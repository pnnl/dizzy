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

**The two loops, and where the second one lives.** ``run_command`` runs the
procedure, whose emits land in the local FIFO event queue; ``_drain_events``
drains that queue to empty. A policy's dispatched command never runs inline —
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

    def _timed(self, kind: str, name: str, trigger: str, fn: Callable[[], None]) -> None:
        """Run one element, emitting a start-end record to the bus (if any) and
        an OTel span (a no-op under the null provider).

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
                }
            )

    # ── The loop ────────────────────────────────────────────────────────────

    def run_command(self, command: Any) -> None:
        """Run one command to quiescence: its procedure, then every event that
        cascades from it. Returns when the local event queue is empty; any
        command a policy dispatched is by then on the external queue."""
        entry = self._procedures.get(type(command))
        if entry is None:
            raise KeyError(
                f"no procedure registered for {type(command).__name__} — the "
                f"wiring did not register it"
            )
        name, runner = entry
        self._timed("procedure", name, type(command).__name__, lambda: runner(command))
        self._drain_events()

    def _drain_events(self) -> None:
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
            # Data loop: projections fold the event, seeing the envelope's
            # ingested_at as their second argument.
            for name, projection in self._projections.get(type(event), []):
                self._timed(
                    "projection",
                    name,
                    envelope.type,
                    # Every free name is bound as a default: the lambda runs
                    # inside this iteration, but a late-binding closure over a
                    # loop variable is a trap waiting for the first caller who
                    # defers it.
                    lambda p=projection, e=event, at=envelope.ingested_at: p(e, at),
                )
            if self.commit is not None:
                self.commit()  # the event is folded, atomically, NOW
            if self.observer is not None:
                self.observer(envelope.type, event)
            # Reactivity loop: policies dispatch commands onto the external queue.
            self.current_event = event
            try:
                for name, policy in self._policies.get(type(event), []):
                    self._timed("policy", name, envelope.type, lambda p=policy, e=event: p(e))
            finally:
                self.current_event = None
