"""The consumer-group worker — one loop, driven by a cursor and a doorbell.

A consumer group is a set of elements sharing one cursor over the event log
(topology.yaml's own unit). This module is the loop that serves one:

    read cursor -> fetch entries after it -> run the elements this group
    claims -> commit their effects -> advance the cursor

which is :meth:`Engine.ingest`'s body with the append taken out of it,
in the same order — so the sequence survives the split verbatim. The fold and
the commit are not re-implemented here: the worker calls
:meth:`Engine.apply_event`, handing it the group's subset of the registered
handlers, precisely so the two intakes cannot drift apart.

**Two intakes, one lane.** A group claiming projections or policies consumes
the LOG and holds a cursor. A group claiming procedures consumes COMMANDS and
holds a lane of the durable queue — no cursor, because a command is not in the
log and there is no position to hold. Most groups are both, and a hybrid
worker's cycle is *fold to tail -> run one command -> fold to tail*. The
leading fold is what carries the ordering guarantee across a group split: a
policy-dispatched command is causally after the append of the event that
triggered it, and positions are local insertion order, so a worker whose fold
covers the command's arrival has necessarily folded that command's triggering
event. The trailing fold restores ``run_command``'s own post-condition —
projections have folded by the time it returns.

The guarantee that survives the split is therefore not quite
``ingest``'s. It is: **the read model commits before the dispatched
command runs.** Inside one group those are the same sentence; across groups
only the second is available.

**Where that guarantee is not yet carried, stated rather than hidden.** The
leading fold covers a command's cause only if the command was already in the
lane when the fold ran. *commands* is today one opaque callable that claims
AND runs, so a claim that BLOCKS waiting for work returns after the fold, and
an event appended during that wait — with the command it dispatched — is not
in the model the command then reads. The window is real and is not closed by
anything here; closing it means splitting the seam into claim and run so the
fold can be taken between them, which is a change to this module's interface
and its callers, not a line inside :meth:`step`. Until then a group that both
tails the log and blocks on a lane holds the weaker property: it folds to tail
at some point before the command, not necessarily after the command's cause.

**Fairness is part of the semantics**, the way the shell already is. This loop
folds first and runs at most one command per cycle: folding is what the
ordering guarantee rests on, so starving it to drain a lane would trade a
correctness-adjacent property for throughput. The cost is the other direction —
a group under heavy replication load is slow to reach its lane — and that is a
deliberate, stated choice rather than an accident of the code.

**The transport is a doorbell only.** It carries "wake up, there is work" and
nothing else. Correctness comes from the cursor, so a dropped notification
costs latency and never costs work — which is what makes the transports
interchangeable, and why this module ships only ``inprocess`` and takes any
other as an argument.

**Not yet wired.** No shell constructs a :class:`GroupWorker`: ``st`` and
``mp`` still drain through :meth:`Engine.ingest`, and this module's per-group
cursor is validated rather than executed. What has since arrived is the LANE
half: :class:`dizzy.engine.st.executor.LaneExecutor` reads the generated
``LANES``/``COMMAND_PLACEMENT`` table and serves a topology's lanes off one
durable queue — and its ``pump()`` is deliberately the shape this module
documents for ``commands=``, so the two compose rather than compete. No
generated wiring builds either of them yet. This module is the mechanism a
topology's REPLICATION half will be run through, and everything below
describes what it does, not a shipped deployment.

**Nothing here names a command, event, or environment field.** The elements a
group claims arrive as arguments — the maps the wiring already built for the
engine — and the group's identity is a string from the topology. That is what
lets one loop serve every feature.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol, runtime_checkable

from dizzy.engine.cursors import START, CursorStore
from dizzy.engine.registry import snake_case
from dizzy.engine.store import reconstruct_event

DEFAULT_POLL_INTERVAL = 0.25
"""How long a worker sleeps when the doorbell says nothing. It is a LATENCY
knob, never a correctness one: set it to an hour and the group still makes
exactly the same progress, an hour later."""


@runtime_checkable
class Doorbell(Protocol):
    """The wake-up seam every transport kind implements.

    Two methods, and deliberately no payload: a doorbell that carried the work
    would be a queue, and then losing a message would lose work. Everything a
    worker needs to know is in the log; the bell only says when to look.
    """

    def ring(self) -> None:
        """Signal that there may be work. Never blocks, never fails."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block until rung or *timeout*; True iff a ring was collected."""
        ...


class InProcessDoorbell:
    """topology.yaml's default transport: a latch and a condition variable.

    The reason a single-group topology costs a consumer nothing — no broker, no
    daemon, no IPC. A latch rather than a bare notify so a ring landing between
    a worker's last fold and its next wait is not lost; a ring landing while
    several workers wait is collected by exactly one of them, and the others
    fall through to their poll. Both of those are latency decisions. Nothing
    here may become a correctness one.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending = False

    def ring(self) -> None:
        with self._cond:
            self._pending = True
            self._cond.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._cond:
            if not self._pending:
                self._cond.wait(timeout)
            rung, self._pending = self._pending, False
            return rung


class NullDoorbell:
    """A bell nobody rings — the polling worker, and the test of the claim.

    Every guarantee this module makes is supposed to hold with the transport
    switched off entirely. This class is how that is exercised rather than
    asserted, so it ships beside the real one instead of living in the suite.
    """

    def ring(self) -> None:
        """Deliberately nothing."""

    def wait(self, timeout: float | None = None) -> bool:
        """Never rung, but still a WAIT — the poll interval has to be real.

        Returning immediately would make the transport-off worker a spin loop
        burning a core between empty cycles, which is not "polling" and is not
        what :data:`DEFAULT_POLL_INTERVAL` describes. The bell still carries
        nothing: this only spends the interval the caller asked for.
        """
        if timeout:
            time.sleep(timeout)
        return False


def shard_for(event_id: str, shards: int) -> int:
    """Which of *shards* owns the entry with this content hash.

    On the id, not on the position: a position is node-local, so two nodes
    would assign the same fact to different shards. The id is the fact's
    identity everywhere, and it is uniformly distributed by construction.
    """
    return int(event_id[:8], 16) % shards


class GroupWorker:
    """One consumer group's worker: a cursor over the log, and at most a lane.

    *projections* and *policies* are the group's claims, in the shape the
    engine registers them (``{event class: [(name, runner)]}`` — that is
    :meth:`Engine.projection_runners` and :meth:`Engine.policy_runners`, sliced
    to this group by whoever built the topology). Passing them rather than
    reading them off the engine is what makes the LOG intake general: two
    log-only groups in one process are two workers over one engine.

    *commands* is the group's command intake, if it claims procedures: a
    callable that runs at most one unit of work and returns whether it did.
    The durable queue is not imported here — a lane is the host's ``claim ->
    run_command -> ack``, and this loop only needs to know whether it happened.

    *engine* supplies :meth:`Engine.apply_event` (the ordering rule and the
    read-model commit, which the engine owns) and the store. On the LOG intake
    only the elements passed here run; the COMMAND intake is the engine's, and
    that asymmetry is a real constraint on how a host wires a topology:
    ``run_command`` cascades through everything the engine has REGISTERED, not
    through this group's slice, so a group that runs commands needs an engine
    registered with its own claims and nothing else. Wire two command-running
    groups onto one engine and each command runs the other group's projections
    and policies inline — and then again when that group's cursor reaches the
    entry. :meth:`Engine.take_applied` cannot repair it either: it is one
    engine-wide list, so whichever worker asks first consumes the other's
    positions. One engine per command-running group, until the command path
    learns to take a group's slice the way :meth:`Engine.apply_event` does.
    """

    def __init__(
        self,
        group: str,
        engine: Any,
        projections: Mapping[type, list[tuple[str, Any]]] | None = None,
        policies: Mapping[type, list[tuple[str, Any]]] | None = None,
        cursors: CursorStore | None = None,
        commands: Callable[[], bool] | None = None,
        doorbell: Doorbell | None = None,
        store: Any = None,
        event_classes: Mapping[str, type] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        batch: int | None = None,
        shard: int = 0,
        shards: int = 1,
    ):
        self.group = group
        self.engine = engine
        self.store = store if store is not None else engine.store
        self.projections = dict(projections or {})
        self.policies = dict(policies or {})
        self.commands = commands
        self._cursor_store = cursors
        self.doorbell = doorbell if doorbell is not None else InProcessDoorbell()
        self.poll_interval = poll_interval
        self.batch = batch
        """Entries to apply before returning from :meth:`fold_to_tail`, or None
        for "to the tail". A bound on a cycle's length, and on nothing else FOR
        A LOG-ONLY GROUP: the cursor is saved per entry either way, so a short
        batch costs cycles and never work. It is refused alongside *commands*,
        because there it would bound the leading fold — see :meth:`step`."""
        self._event_classes = event_classes
        if batch is not None and commands is not None:
            raise ValueError(
                f"group {group!r} runs commands and cannot also cap its fold at "
                f"batch={batch}: the leading fold of a hybrid cycle is what puts the "
                f"model ahead of the command about to run, and a fold that stops short "
                f"of the tail hands that command a model missing entries it should have "
                f"seen. batch is a latency knob for a log-only group"
            )
        if shards < 1 or not 0 <= shard < shards:
            raise ValueError(f"shard {shard} of {shards} is not a shard of anything")
        if shards > 1 and self.projections:
            raise ValueError(
                f"group {group!r} holds {len(self.projections)} projection(s) and cannot "
                f"be sharded across {shards} workers: shards fold disjoint subsets of the "
                f"log, so each one's model would hold a fraction of the events. Split the "
                f"topology into more groups instead — that is a semantic decision, and it "
                f"is meant to be made in topology.yaml"
            )
        self.shard = shard
        self.shards = shards
        self._stop = threading.Event()

    # ── Identity ────────────────────────────────────────────────────────────

    @property
    def cursor_name(self) -> str:
        """The cursor this worker advances.

        One name per SHARD, never one name shared by N workers: ``save`` is
        monotone, so a shared cursor's leading worker covers a prefix the
        laggard never committed and a crash there loses that work silently.
        Each shard's watermark is honest about exactly what that shard folded.
        """
        return self.group if self.shards == 1 else f"{self.group}#{self.shard}/{self.shards}"

    @property
    def claims(self) -> frozenset[str]:
        """The stream names this group consumes — the log's names, not classes.

        Derived from the registered classes rather than configured, so an
        entry's type is matched without this module being told any of them.
        """
        return frozenset(snake_case(cls.__name__) for cls in (*self.projections, *self.policies))

    @property
    def reads_log(self) -> bool:
        """Whether this group has an event intake at all.

        False for a procedure-only group, and then no cursor is ever loaded or
        saved — the decided answer to "what does a procedure group's cursor
        read?" is that it does not have one.
        """
        return bool(self.projections or self.policies)

    def owns(self, event_id: str) -> bool:
        return self.shards == 1 or shard_for(event_id, self.shards) == self.shard

    @property
    def event_classes(self) -> Mapping[str, type]:
        return self._event_classes if self._event_classes is not None else self.store.event_classes

    # ── The loop ────────────────────────────────────────────────────────────

    def position(self) -> int:
        """Where this worker has got to. :data:`START` for a group with no
        event intake, which is the honest answer and not a missing cursor."""
        return self._cursors.load(self.cursor_name) if self.reads_log else START

    def fold_to_tail(self, skip: Collection[int] = ()) -> int:
        """Apply every entry after the cursor; return how many this group ran.

        *skip* names positions the caller has already applied — a hybrid
        worker's own appends, folded inline by ``run_command``. They are still
        walked, because the cursor must pass them.

        The order per entry is the crash rule: apply, which commits the read
        model and dispatches, and only THEN save. A crash between the two
        re-delivers the entry, which the system is built to absorb; the
        reverse loses it, which nothing can recover.

        A cursor sitting PAST the tail is repaired here rather than obeyed —
        rewound to :data:`START` and re-folded. It is reachable without an
        operator mistake, because cursors deliberately live in a file of their
        own: restore or rebuild the event store while the cursor store survives
        and every group is ahead of a log it has never seen. Obeying it is a
        permanent silent stall (nothing after the cursor, so nothing applies,
        so :meth:`step` reports no work and the worker sleeps while the log
        fills); re-folding costs a replay, which is exactly what disposability
        says a cursor may ever cost. :class:`SqliteCursorStore` cannot make
        this repair — it has never seen the log — and this is the object that
        holds both.
        """
        if not self.reads_log:
            return 0
        cursors = self._cursors
        skip = frozenset(skip)
        claims = self.claims
        applied = 0
        walked = 0
        position = cursors.load(self.cursor_name)
        for envelope in self.store.iterate_from(position):
            walked += 1
            if envelope.position not in skip and envelope.type in claims and self.owns(envelope.id):
                event = reconstruct_event(envelope, self.event_classes)
                self.engine.apply_event(
                    event, envelope.ingested_at, envelope.type, self.projections, self.policies
                )
                applied += 1
            # Saved for a skipped entry too: this group is done with it, by
            # claim or by ownership, and a watermark that stalled on entries
            # nobody here consumes would replay the whole log every boot.
            cursors.save(self.cursor_name, envelope.position)
            if self.batch is not None and applied >= self.batch:
                break
        # Checked only on an empty pass, so the repair costs one extra seek per
        # idle cycle and nothing at all while a group is making progress.
        if walked == 0 and position > START and not self._log_reaches(position):
            cursors.rewind(self.cursor_name, START)
            return self.fold_to_tail(skip)
        return applied

    def _log_reaches(self, position: int) -> bool:
        """Whether the log holds an entry at or after *position*."""
        return next(iter(self.store.iterate_from(position - 1)), None) is not None

    def step(self) -> bool:
        """One cycle of the two intakes. True iff any work was done.

        Fold to tail, then take at most one command, then fold to tail again —
        the trailing fold skipping what the command's own cascade already
        folded inline, so a policy that dispatched during ``run_command`` does
        not dispatch a second time when the cursor walks over its event.
        """
        worked = self.fold_to_tail() > 0
        if self.commands is not None and self.commands():
            self.fold_to_tail(skip=self.engine.take_applied())
            worked = True
        return worked

    def run(self, stop: threading.Event | None = None) -> None:
        """Step until stopped, sleeping on the doorbell between empty cycles."""
        # Both flags, not one: every caller today passes an Event of its own,
        # and a stop() that set only the internal one would be a method that
        # silently does nothing. Read rather than swapped, so a stop() racing
        # the start of the run cannot be lost.
        stopped = self._stop if stop is None else stop
        while not (stopped.is_set() or self._stop.is_set()):
            if not self.step():
                self.doorbell.wait(self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        self.doorbell.ring()  # so a worker asleep on the bell leaves promptly

    # ── Cursor store ────────────────────────────────────────────────────────

    @property
    def _cursors(self) -> CursorStore:
        """The group's cursor store, refused rather than invented when absent.

        A default would be the dangerous convenience here: an in-memory store
        conjured per worker looks like it works and quietly replays the log on
        every boot, and a sqlite one would put a file where no operator asked
        for it.
        """
        if self._cursor_store is None:
            raise ValueError(
                f"group {self.group!r} consumes the log and was given no cursor store; "
                f"pass cursors= (MemoryCursorStore is the right one for a single-shot "
                f"run — every boot replays from zero, which is slow and correct)"
            )
        return self._cursor_store


def group_workers(
    group: str,
    engine: Any,
    *,
    projections: Mapping[type, list[tuple[str, Any]]] | None = None,
    policies: Mapping[type, list[tuple[str, Any]]] | None = None,
    workers: int = 1,
    **kwargs: Any,
) -> tuple[GroupWorker, ...]:
    """Build the workers a group's ``workers: N`` asks for — three answers, not one.

    The knob means something different for each kind of group, and conflating
    them is how it becomes a lie:

    * **Holding a projection.** Refused above N=1. Shards fold disjoint
      subsets, so each model would hold a fraction of the stream;
      :class:`GroupWorker` raises, and the topology validator refuses it
      earlier for the same reason.
    * **Policies only, reading nothing.** N SHARDS, each with a cursor of its
      own — never N workers over one cursor, which is the at-most-once hole a
      monotone ``save`` hides rather than fixes. Worker *k* reads the whole
      stream from its own position, acts on the entries it owns, and saves on
      every entry either way. The price is that changing N invalidates every
      assignment: the only safe reshard is to rewind all N cursors to the
      lowest and replay.
    * **Procedures only.** N workers on one lane, sharing the queue's claim —
      no cursor is involved at all. Genuinely just operations, with one
      prerequisite this module cannot supply: the queue's claim must be safe
      between PROCESSES, not merely between threads.
    """
    if workers < 1:
        raise ValueError(f"group {group!r} asks for {workers} workers")
    # Sharding is the cursor's answer, so it applies exactly when there is a
    # cursor: a procedure-only group has none, and its N workers share a lane
    # instead. A group holding a projection reaches GroupWorker's refusal here
    # rather than getting a silently shared cursor.
    shards = workers if (projections or policies) else 1
    return tuple(
        GroupWorker(
            group,
            engine,
            projections=projections,
            policies=policies,
            # The shard index means something only when there is a cursor to
            # shard. A procedure-only group has none — its N workers share one
            # lane — so every one of them is the whole (unsharded) group.
            shard=k if shards > 1 else 0,
            shards=shards,
            **kwargs,
        )
        for k in range(workers)
    )
