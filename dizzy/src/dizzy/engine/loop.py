"""The control loop — a generic two-queue dispatcher for a DIZZY feature.

This is the engine the scheduling shells drive. It owns the event store: every
emitted event is appended FIRST (getting its envelope, whose ``ingested_at`` is
the stream-append timestamp), and only then folded by projections and handed to
policies.

  * COMMAND queue (external, the shell's) — policies dispatch commands onto it;
    the shell drains it, one command per unit of work.
  * EVENT queue (local, this unit) — drained inside :meth:`Engine.run_command`:
    each event is appended, folded by its projections (the data loop), then
    handed to its policies (the reactivity loop), which dispatch further
    commands.

``run_command`` is the composition of two public halves — ``run_procedure``,
which touches no store and so may run in another process, and ``ingest``,
which is the log's only writer.

**The two loops, and where the second one lives.** ``run_command`` runs the
procedure, whose emits land in the local FIFO event queue; ``ingest`` appends
and applies that queue to empty. A policy's dispatched command never runs inline —
it goes to the OUTER command queue and becomes the next unit of work. So the
defined "drain the event queue, then drain the command queue, repeat" semantics
are real here, but only the first phase is owned by this class: **the shell is
part of the semantics.** That matters to anything modelling them, and it is why
the two shells can claim different guarantees while sharing this engine
verbatim — ``st`` (one lane) is sequentially consistent, ``mp`` (N workers
under at-least-once delivery) deliberately is not.

**Ordering rule.** Projections fold and the read model commits BEFORE policies
dispatch. The commit is what makes fold-then-enqueue real across processes: a
policy-dispatched command may be claimed by another worker within
milliseconds, and that worker sees only committed state. The engine owns this
boundary; projections never commit.

That sentence is exact WITHIN A CONSUMER GROUP, which is the whole of the
system until a topology splits one. Across groups it weakens, by design and
not by accident: a group holding only policies has a cursor of its own, so it
reaches an entry before — or long after — the group holding the projections
folds it, and the fold it sees committed is that group's, not the log's tail.
:mod:`dizzy.engine.groups` states which half survives the split and names the
window in which even that half is not yet carried.

**What keeps it feature-agnostic.** Handlers are keyed by the runtime type of
the command/event — the generated pydantic class — so dispatch is a dict
lookup and no name appears in this file. Registration mirrors the feat
topology, and is the wiring's job to perform.

Telemetry (host-level observation, never events): given a ``bus`` callable,
the engine emits a start/end record per element execution and one per event
appended. Elements know nothing about it; this is the engine observing itself.
Tracing goes through the injected OTel provider, which defaults to the no-op —
an engine must run with tracing switched off.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from dizzy.engine.ports import NullOtel

CommandRunner = Callable[[Any], None]
ProjectionRunner = Callable[[Any, datetime | None], None]  # (event, ingested_at)
PolicyRunner = Callable[[Any], None]
EventObserver = Callable[[str, Any], None]
BusEmit = Callable[[dict], None]


class Engine:
    """One process's control loop over a registered topology."""

    def __init__(
        self,
        command_queue: Any,
        store: Any,
        observer: EventObserver | None = None,
        bus: BusEmit | None = None,
        commit: Callable[[], None] | None = None,
        otel: Any = None,
    ):
        self.command_queue = command_queue
        """External (shell-owned) command queue — where dispatches go."""
        self.store = store
        """Append-only event store: the truth."""
        self.observer = observer
        self.bus = bus
        self.commit = commit
        """Called once per event, after its projections fold and before its
        policies dispatch. ``None`` means the app has no read-model
        transaction to close (see the ordering rule above)."""
        self.otel = otel if otel is not None else NullOtel()
        self._events: deque = deque()
        self.applied_positions: list[int] = []
        """Log positions this engine appended and applied during the unit of
        work it is running, read back by :meth:`take_applied`. Reset by
        :meth:`run_command` and drained by :meth:`take_applied`, and by nothing
        else: a unit may :meth:`ingest` more than once (a host driving the two
        halves itself does exactly that), and every batch's positions must
        survive until the worker asks for them. A host that drives the halves
        owns the unit boundary, which it marks by calling
        :meth:`take_applied`."""
        self.current_event = None
        """The event whose policies are currently running, for wiring that
        needs to correlate a dispatch back to its cause."""
        self._procedures: dict[type, tuple[str, CommandRunner]] = {}
        self._projections: dict[type, list[tuple[str, ProjectionRunner]]] = defaultdict(list)
        self._policies: dict[type, list[tuple[str, PolicyRunner]]] = defaultdict(list)
        self._duration_hist: Any = None

    # ── Registration (the wiring calls these; mirrors the feat topology) ─────

    def register_procedure(
        self, command_type: type, runner: CommandRunner, name: str | None = None
    ) -> None:
        self._procedures[command_type] = (name or command_type.__name__, runner)

    def register_projection(
        self, event_type: type, runner: ProjectionRunner, name: str | None = None
    ) -> None:
        self._projections[event_type].append((name or event_type.__name__, runner))

    def register_policy(
        self, event_type: type, runner: PolicyRunner, name: str | None = None
    ) -> None:
        self._policies[event_type].append((name or event_type.__name__, runner))

    def projection_runners(self) -> dict[type, list[tuple[str, ProjectionRunner]]]:
        """The event class -> ``[(name, runner)]`` map, as registered.

        This is exactly what :func:`dizzy.engine.rebuild.rebuild` and
        :func:`dizzy.engine.replicate.fold_envelopes` take, so a host that has
        already wired an engine does not wire the data loop a second time to
        refold or to replicate — one registration, three triggers.
        """
        return dict(self._projections)

    def policy_runners(self) -> dict[type, list[tuple[str, PolicyRunner]]]:
        """The event class -> ``[(name, runner)]`` map for the reactivity loop.

        Mirrors :meth:`projection_runners` for the same reason: a consumer that
        reads the log itself — :class:`dizzy.engine.groups.GroupWorker` — needs
        the registered handlers without re-deriving them from the wiring.
        """
        return dict(self._policies)

    # ── Emit closures handed to elements ────────────────────────────────────

    def emit_event(self, event: Any) -> None:
        """A procedure emitted an event -> onto the local event queue."""
        self._events.append(event)

    def dispatch_command(self, command: Any) -> None:
        """A policy dispatched a command -> onto the external queue."""
        self.command_queue.put(command)

    def discard_pending_events(self) -> None:
        """Drop events emitted but not yet drained.

        A shell calls this after a failed command so un-appended events cannot
        leak into the NEXT command on a long-lived process.
        """
        self._events.clear()

    # ── Self-observation ────────────────────────────────────────────────────

    def _bus_emit(self, record: dict) -> None:
        """Emit a bus record, stamped with the active trace id (if any) so a
        log line can be linked back to its trace."""
        if self.bus is None:
            return
        trace_id = self.otel.trace_id_hex()
        if trace_id:
            record["trace_id"] = trace_id
        self.bus(record)

    def _record_duration(self, kind: str, name: str, outcome: str, ms: float) -> None:
        """Histogram of element run durations (no-op without an OTel meter)."""
        if self._duration_hist is None:
            meter = getattr(self.otel, "meter", None)
            if meter is None:
                return
            self._duration_hist = meter().create_histogram(
                "dizzy.element.duration",
                unit="ms",
                description="element (procedure/projection/policy) run duration",
            )
        self._duration_hist.record(
            ms, {"dizzy.kind": kind, "dizzy.name": name, "dizzy.outcome": outcome}
        )

    def _timed(
        self,
        kind: str,
        name: str,
        trigger: str,
        fn: Callable[[], None],
        extra: dict | None = None,
    ) -> None:
        """Run one element, emitting a start-end record to the bus (if any) and
        an OTel span (a no-op under the null provider).

        *extra* is merged into both records — it is how a caller running an
        element somewhere other than the main lane says so, without this file
        knowing what lanes there are.

        Exceptions propagate after being recorded — the engine observes, it
        does not swallow.
        """
        if self.bus is None:
            return fn()
        with self.otel.tracer().start_as_current_span(
            f"{kind} {name}",
            record_exception=True,
            attributes={"dizzy.kind": kind, "dizzy.name": name, "dizzy.trigger": trigger},
        ):
            t0 = time.monotonic()
            try:
                fn()
            except Exception as exc:
                dur = round((time.monotonic() - t0) * 1000, 1)
                self._record_duration(kind, name, "error", dur)
                self._bus_emit(
                    {
                        "kind": kind,
                        "name": name,
                        "trigger": trigger,
                        "duration_ms": dur,
                        "outcome": "error",
                        "detail": f"{type(exc).__name__}: {exc}",
                        **(extra or {}),
                    }
                )
                raise
            dur = round((time.monotonic() - t0) * 1000, 1)
            self._record_duration(kind, name, "ok", dur)
            self._bus_emit(
                {
                    "kind": kind,
                    "name": name,
                    "trigger": trigger,
                    "duration_ms": dur,
                    "outcome": "ok",
                    "detail": "",
                    **(extra or {}),
                }
            )

    # ── The loop ────────────────────────────────────────────────────────────

    def run_command(self, command: Any) -> None:
        """Run one command to quiescence: its procedure, then every event that
        cascades from it. Returns when the local event queue is empty; any
        command a policy dispatched is by then on the external queue.

        The composition of the two halves below, which is all it is: they are
        split so a host can put the halves in different processes.
        """
        # The unit of work is the reset boundary for the applied positions, and
        # this method is the only place that boundary is known: a half on its
        # own cannot tell whether it opens a unit or continues one.
        self.applied_positions.clear()
        self.ingest(self.run_procedure(command))

    def run_procedure(self, command: Any, lane: str | None = None) -> list[Any]:
        """Run one command's procedure and return the events it emitted, in
        order, WITHOUT appending them.

        This half touches no store, which is what lets it run somewhere else:
        a pooled child gets a command in and hands events back, and the log
        keeps exactly one writer — whoever calls :meth:`ingest`. *lane* is
        stamped onto the procedure's bus record so telemetry can say where it
        ran; the engine neither interprets it nor knows the lane names.

        A raising procedure leaves what it emitted on the queue, as before —
        the shell drops it with :meth:`discard_pending_events` so a half-run
        command cannot hand its events to the next one.

        Only the WRITE side is free of the store. A procedure that queries a
        read model still reads one, and running it elsewhere hands it whatever
        model that process holds — so a host that moves this half off the lane
        owes the procedure a model carrying everything :meth:`ingest` has
        already committed. Nothing here can check that, and a host that gets it
        wrong sees a procedure decide on stale state, not a reordering.
        """
        entry = self._procedures.get(type(command))
        if entry is None:
            raise KeyError(
                f"no procedure registered for {type(command).__name__} — the "
                f"wiring did not register it"
            )
        name, runner = entry
        self._timed(
            "procedure",
            name,
            type(command).__name__,
            lambda: runner(command),
            extra=None if lane is None else {"lane": lane},
        )
        emitted = list(self._events)
        self._events.clear()
        return emitted

    def ingest(self, events: Any) -> None:
        """Append each event, then apply it: fold, commit, observe, dispatch.

        The stream's only writer. Events reaching here need not have been
        emitted in this process — a procedure that ran in a pool hands its
        events back and they are ingested on the main lane, byte for byte the
        write path a locally-run procedure takes.

        A unit of work may call this more than once, so it does NOT reset
        :attr:`applied_positions` — the positions accumulate until
        :meth:`take_applied` drains them, and a host driving the halves itself
        must call that once per unit or a worker's skip-list will span two.

        At-most-once per list, and NOT retryable as a whole: an element that
        raises part-way leaves the prefix already in the log — appended first,
        by design — so a host must fail the unit rather than ingest the same
        list again. The un-appended tail stays on the engine's own queue, where
        :meth:`discard_pending_events` can drop it.
        """
        # Incoming events join the engine's queue rather than a local one, so a
        # failure mid-drain leaves the remainder somewhere the shell can still
        # discard it, exactly as it did when this loop owned the emits.
        self._events.extendleft(reversed(list(events)))
        while self._events:
            event = self._events.popleft()
            # The store is the truth: append FIRST, get the envelope.
            envelope = self.store.append(event)
            if self.bus is not None:
                self._bus_emit(
                    {
                        "kind": "event",
                        "name": envelope.type,
                        "trigger": envelope.id[:12],
                        "duration_ms": None,
                        "outcome": "appended",
                        "detail": "",
                    }
                )
            self.apply_event(event, envelope.ingested_at, envelope.type)
            # Where the entry landed, for a worker that also tails the log:
            # it has already been folded here and must not be folded again
            # when the cursor reaches it. See Engine.take_applied.
            self.applied_positions.append(envelope.position)
            # An element that emitted while handling this entry appended to the
            # same queue, so it joins this drain and quiescence still means
            # quiescence.

    def apply_event(
        self,
        event: Any,
        ingested_at: datetime | None = None,
        type_name: str | None = None,
        projections: dict[type, list[tuple[str, ProjectionRunner]]] | None = None,
        policies: dict[type, list[tuple[str, PolicyRunner]]] | None = None,
    ) -> None:
        """Fold one event, commit, observe, then run its policies.

        The ordering rule in one place, so the two things that consume an event
        — :meth:`ingest`, for an entry this process just appended, and a
        group worker, for one it read back out of the log — cannot drift apart.
        It deliberately does NOT append: an entry reaching this method is
        already a fact, whoever wrote it.

        *projections* and *policies* override the registered maps, which is how
        a worker runs one consumer group's subset of the topology through the
        engine's order without a second engine. *type_name* is the stream's
        name for the event, used only for telemetry; it falls back to the
        class name so a caller holding no envelope need not invent one.
        """
        projections = self._projections if projections is None else projections
        policies = self._policies if policies is None else policies
        label = type_name or type(event).__name__
        # Data loop: projections fold the event, seeing ingested_at as their
        # second argument.
        for name, projection in projections.get(type(event), []):
            self._timed(
                "projection",
                name,
                label,
                # Every free name is bound as a default: the lambda runs
                # inside this iteration, but a late-binding closure over a
                # loop variable is a trap waiting for the first caller who
                # defers it.
                lambda p=projection, e=event, at=ingested_at: p(e, at),
            )
        if self.commit is not None:
            self.commit()  # the event is folded, atomically, NOW
        if self.observer is not None:
            self.observer(label, event)
        # Reactivity loop: policies dispatch commands onto the external queue.
        self.current_event = event
        try:
            for name, policy in policies.get(type(event), []):
                self._timed("policy", name, label, lambda p=policy, e=event: p(e))
        finally:
            self.current_event = None

    def take_applied(self) -> tuple[int, ...]:
        """The log positions the unit of work just finished appended and applied.

        Every :meth:`ingest` since the last reset, not merely the last one: a
        unit that ingests several batches — the shape a pooled procedure gives
        a host — must hand the worker ONE skip-list covering all of them.

        A hybrid group both runs commands here and tails the log, and those two
        intakes see the same entries: the fold happened inline, and the cursor
        then walks over the very same rows. Handing the positions to the worker
        is what stops the second pass from re-running policies that already
        dispatched — the tolerated case is a crash, not every command.

        Engine-wide and destructive, so it serves exactly ONE worker: two
        workers over one engine would consume each other's positions. That is
        the same constraint the inline cascade already imposes — an engine
        registered with one group's elements, per
        :class:`dizzy.engine.groups.GroupWorker` — restated at the seam where
        it would otherwise be discovered by a double dispatch.
        """
        applied = tuple(self.applied_positions)
        self.applied_positions.clear()
        return applied
