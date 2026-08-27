"""Conformance: one wiring, two scheduling models, and what each one GUARANTEES.

The engine and the wiring are shared verbatim; a shell only decides who drains
the command queue and how many drain it at once. This suite pins what that
choice costs, because the two shells DIZZY ships do not make the same promise:

- **sequential** (what ``dizzy.engine.st`` does): one lane, one process, FIFO.
  This is DIZZY's *defined* semantics — one legal interleaving, sequentially
  consistent.
- **uniform queue** (what ``dizzy.engine.mp`` does): every command, external
  and policy-dispatched alike, lands in one queue; workers claim in arbitrary
  order and at-least-once delivery means some commands run twice. Deliberately
  weaker.

The claim under test is NOT that the two are identical — they are not, and the
stream proves it. It is that **the read models converge**, which is what
confluent projections buy and what makes the weaker shell safe to deploy. Both
halves are asserted, so a future change cannot quietly promote mp's guarantee
or demote st's.

The fixture feature lives here rather than in ``examples/`` because DIZZY's CI
must be able to run this without the example's generated workspace on the path.
It is the smallest feature with a real cascade: a batch produces an entity, and
a policy advances whatever batch was blocked waiting for it.
"""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Any, cast

import pytest
from dizzy.engine.loop import Engine
from dizzy.engine.store import EventStore
from pydantic import BaseModel

# ── The fixture feature ─────────────────────────────────────────────────────


class OpenBatch(BaseModel):
    batch_id: str
    requires: str = ""


class AdvanceBatch(BaseModel):
    batch_id: str


class BatchOpened(BaseModel):
    batch_id: str
    requires: str = ""
    status: str


class EntityProduced(BaseModel):
    batch_id: str
    entity: str


EVENT_CLASSES = {"batch_opened": BatchOpened, "entity_produced": EntityProduced}

# Each batch produces the entity named after it, so a chain is easy to declare.
CHAIN = [("starter", ""), ("loaf", "starter"), ("croutons", "loaf")]


class ReadModel:
    """The read models, as plain data.

    Every fold is idempotent and order-insensitive — assignment and set
    insertion, never ``+= 1`` — which is exactly the confluence the uniform
    scheduler relies on. A counter here would break under redelivery, and that
    would be the projection's bug, not the shell's.
    """

    def __init__(self):
        self.batches: dict[str, str] = {}
        self.entities: set[str] = set()
        self.blocked_on: dict[str, str] = {}

    def snapshot(self):
        return (
            dict(sorted(self.batches.items())),
            sorted(self.entities),
            dict(sorted(self.blocked_on.items())),
        )


def build_engine(read_model: ReadModel, queue, store: EventStore) -> Engine:
    """The wiring. Identical under both schedulers — that is the point."""
    engine = Engine(command_queue=queue, store=store)

    def fold_opened(event: BatchOpened, ingested_at) -> None:
        # setdefault, NOT assignment: under reordering this event can arrive
        # after the batch already completed, and a plain assignment would walk
        # the status backwards. Status is a one-way lattice here — that is what
        # makes the fold commutative, and it has to be designed in.
        read_model.batches.setdefault(event.batch_id, event.status)
        if event.requires:
            read_model.blocked_on[event.batch_id] = event.requires

    def fold_produced(event: EntityProduced, ingested_at) -> None:
        read_model.entities.add(event.entity)
        read_model.batches[event.batch_id] = "completed"  # the terminal state

    engine.register_projection(BatchOpened, fold_opened, name="batch_store")
    engine.register_projection(EntityProduced, fold_produced, name="inventory")

    def advance_ready(event: EntityProduced) -> None:
        # Reads the read model, which the engine has already committed.
        for batch_id, requires in sorted(read_model.blocked_on.items()):
            if requires == event.entity and read_model.batches.get(batch_id) != "completed":
                engine.dispatch_command(AdvanceBatch(batch_id=batch_id))

    def advance_if_ready(event: BatchOpened) -> None:
        # The other half of the cascade. Without it, a batch whose requirement
        # already existed when it opened would sit at "ready" forever — and
        # whether that happens depends on claim order, so the two shells would
        # disagree for a reason that has nothing to do with the projections.
        if event.status == "ready":
            engine.dispatch_command(AdvanceBatch(batch_id=event.batch_id))

    engine.register_policy(EntityProduced, advance_ready, name="advance_ready_batches")
    engine.register_policy(BatchOpened, advance_if_ready, name="advance_opened_batches")

    def open_batch(command: OpenBatch) -> None:
        ready = not command.requires or command.requires in read_model.entities
        engine.emit_event(
            BatchOpened(
                batch_id=command.batch_id,
                requires=command.requires,
                status="ready" if ready else "blocked",
            )
        )

    def run_batch(command: AdvanceBatch) -> None:
        if read_model.batches.get(command.batch_id) == "completed":
            return  # already done: the guard that makes redelivery harmless
        engine.emit_event(EntityProduced(batch_id=command.batch_id, entity=command.batch_id))

    engine.register_procedure(OpenBatch, open_batch, name="open_batch")
    engine.register_procedure(AdvanceBatch, run_batch, name="run_batch")
    return engine


SCRIPT = [OpenBatch(batch_id=b, requires=r) for b, r in reversed(CHAIN)]
"""The external commands, identical for both runs.

Batches open in reverse dependency order, so under the sequential shell the
later two open *blocked* and are advanced by the policy once their requirement
appears. Every command after these three is dispatched by a policy — the
cascade is the feature, not the script."""


# ── The two schedulers ──────────────────────────────────────────────────────


class Queue:
    def __init__(self):
        self.items = deque()

    def put(self, command, origin="policy"):
        self.items.append(command)

    def qsize(self):
        return len(self.items)


def run_sequential(tmp_path) -> tuple[ReadModel, EventStore]:
    """st: one lane, FIFO, to quiescence after each external command."""
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "seq.db", event_classes=EVENT_CLASSES)
    queue = Queue()
    engine = build_engine(read_model, queue, store)
    for command in SCRIPT:
        engine.run_command(command)
        while queue.qsize():
            engine.run_command(queue.items.popleft())
    return read_model, store


def run_uniform(tmp_path, seed: int, duplicate: bool = True) -> tuple[ReadModel, EventStore]:
    """mp: one queue for everything, arbitrary claim order, at-least-once.

    Workers are simulated in-process — the real process boundary is covered by
    ``test_mp_shell.py``. What matters here is the SEMANTICS: which command
    runs next is not determined, and some run twice.
    """
    rng = random.Random(seed)
    read_model = ReadModel()
    store = EventStore(path=tmp_path / f"uni{seed}.db", event_classes=EVENT_CLASSES)
    queue = Queue()
    engine = build_engine(read_model, queue, store)
    for command in SCRIPT:
        queue.put(command)
    while queue.qsize():
        index = rng.randrange(len(queue.items))  # any claimable command, not the head
        queue.items.rotate(-index)
        command = queue.items.popleft()
        engine.run_command(command)
        if duplicate and rng.random() < 0.5:
            engine.run_command(command)  # at-least-once delivery
    return read_model, store


# ── What each shell claims ──────────────────────────────────────────────────


def test_the_sequential_shell_produces_the_defined_result(tmp_path):
    """The reference. One legal interleaving, so this is simply THE answer."""
    read_model, store = run_sequential(tmp_path)
    assert read_model.batches == {
        "starter": "completed",
        "loaf": "completed",
        "croutons": "completed",
    }
    assert read_model.entities == {"starter", "loaf", "croutons"}


def test_the_sequential_shell_is_deterministic(tmp_path):
    """Sequential consistency means running it again cannot differ — including
    the ORDER and CONTENT of the stream, not just the read models.

    Not the ids, though: ``ingested_at`` is stamped at append and travels
    inside the hashed payload, so the same fact appended a second later is a
    different record. That is deliberate — the append time is part of the fact
    — which is why equivalence between two runs is judged on payloads.
    """
    first, first_store = run_sequential(tmp_path / "a")
    second, second_store = run_sequential(tmp_path / "b")
    assert first.snapshot() == second.snapshot()
    assert [(e.type, e.payload) for e in first_store.iterate()] == [
        (e.type, e.payload) for e in second_store.iterate()
    ]


@pytest.mark.parametrize("seed", range(8))
def test_the_uniform_shell_converges_to_the_same_read_models(tmp_path, seed):
    """The load-bearing claim: reorder the commands and redeliver some, and the
    read models still land where the defined semantics say they should."""
    reference, _ = run_sequential(tmp_path / "ref")
    scrambled, _ = run_uniform(tmp_path, seed)
    assert scrambled.snapshot() == reference.snapshot()


@pytest.mark.parametrize("seed", range(4))
def test_the_uniform_shell_does_not_promise_an_identical_stream(tmp_path, seed):
    """The honest half. Redelivery appends real facts, so the two shells do NOT
    agree on the event stream — only on what the stream folds to. A change that
    made this pass would mean mp had silently become sequential."""
    _, reference = run_sequential(tmp_path / "ref")
    _, scrambled = run_uniform(tmp_path, seed)
    assert len(scrambled) >= len(reference)


def test_without_redelivery_the_uniform_shell_still_reorders(tmp_path):
    """Isolating the two weakenings: even with exactly-once delivery, claim
    order alone is enough to make mp non-sequential — and convergence has to
    survive that on its own."""
    reference, _ = run_sequential(tmp_path / "ref")
    for seed in range(8):
        scrambled, _ = run_uniform(tmp_path / f"s{seed}", seed, duplicate=False)
        assert scrambled.snapshot() == reference.snapshot()


def test_refolding_the_stream_changes_nothing_that_is_confluent(tmp_path):
    """The property mp, rebuild and replication all lean on, stated directly.

    Rebuild refolds the whole stream; fold-on-replicate folds a peer's facts
    that may already have been folded here. Both mean a projection sees the
    same event more than once, so "confluent" is not a nicety — it is the
    precondition for every one of those features. Fold the stream a second time
    and the read model must not move.
    """
    reference, store = run_sequential(tmp_path / "ref")
    read_model = ReadModel()
    queue = Queue()
    engine = build_engine(read_model, queue, store)

    def refold() -> None:
        for envelope in store.iterate():
            event = store.reconstruct_event(envelope)
            for _name, runner in engine.projection_runners().get(type(event), []):
                runner(event, envelope.ingested_at)

    refold()
    once = read_model.snapshot()
    assert once == reference.snapshot()
    refold()
    assert read_model.snapshot() == once  # folding twice == folding once


def test_a_counter_shows_what_non_confluence_would_cost(tmp_path):
    """The boundary of the claim: mp is safe because the PROJECTIONS are
    confluent, not because the shell repairs anything. A counter — the classic
    non-idempotent fold — double-counts the moment the stream is refolded, and
    no amount of scheduling discipline saves it."""
    _, store = run_sequential(tmp_path / "ref")
    counts: dict[str, int] = {}

    def fold(event, ingested_at) -> None:
        counts[event.entity] = counts.get(event.entity, 0) + 1

    for _ in range(2):
        for envelope in store.iterate():
            event = store.reconstruct_event(envelope)
            if isinstance(event, EntityProduced):
                fold(event, envelope.ingested_at)

    assert counts == {"starter": 2, "loaf": 2, "croutons": 2}  # should have been 1s


def test_the_engines_registration_is_what_rebuild_refolds(tmp_path):
    """One wiring drives all three triggers.

    A host registers its projections once, with the engine; ``rebuild`` and
    fold-on-replicate then take that same map. If they had to be wired
    separately, the two could drift — and a rebuild that folds a DIFFERENT set
    of projections than the live engine is a rebuild that silently produces a
    different read model.
    """
    from dizzy.engine.rebuild import rebuild

    reference, store = run_sequential(tmp_path / "ref")

    class Metadata:
        def drop_all(self, bind):
            pass

        def create_all(self, bind):
            pass

    class Session:
        def get_bind(self):
            return None

        def commit(self):
            pass

    read_model = ReadModel()
    engine = build_engine(read_model, Queue(), store)
    folded = rebuild(store, Session(), engine.projection_runners(), [Metadata()])
    assert folded == len(store)
    assert read_model.snapshot() == reference.snapshot()


# ── The split lane: the procedure runs elsewhere, the log keeps one writer ───


class NoWriteStore:
    """The store as a pooled child may hold it: readable, and fatal to append.

    A procedure that reached for the log here would be a process boundary the
    design does not allow, so the test refuses to discover that as a race.
    """

    def __init__(self, store: EventStore):
        self._store = store

    def as_store(self) -> EventStore:
        """A child holds something store-SHAPED; the annotation is the lie the
        process boundary makes true."""
        return cast(EventStore, self)

    def append(self, event):
        raise AssertionError("run_procedure touched the store")

    def __getattr__(self, name):
        return getattr(self._store, name)

    def __len__(self):
        return len(self._store)


def run_split(tmp_path) -> tuple[ReadModel, EventStore]:
    """The same FIFO schedule as ``run_sequential``, with each procedure run by
    an engine that cannot write and its events ingested by the one that can."""
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "split.db", event_classes=EVENT_CLASSES)
    queue = Queue()
    main = build_engine(read_model, queue, store)
    background = build_engine(read_model, Queue(), NoWriteStore(store).as_store())

    def step(command) -> None:
        main.ingest(background.run_procedure(command, lane="background"))

    for command in SCRIPT:
        step(command)
        while queue.qsize():
            step(queue.items.popleft())
    assert background.command_queue.qsize() == 0  # only ingest dispatches
    return read_model, store


def test_splitting_the_procedure_off_the_lane_changes_nothing(tmp_path):
    """run_procedure + ingest is run_command, even across two engines: the
    stream and the read models are the defined sequential result."""
    split, split_store = run_split(tmp_path / "a")
    sequential, sequential_store = run_sequential(tmp_path / "b")
    assert split.snapshot() == sequential.snapshot()
    assert [(e.type, e.payload) for e in split_store.iterate()] == [
        (e.type, e.payload) for e in sequential_store.iterate()
    ]


def test_the_lane_a_procedure_ran_on_reaches_the_bus(tmp_path):
    """Telemetry has to be able to say 'ran in pool'; the engine carries the
    marker through without knowing what lanes exist."""
    records = []
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "lane.db", event_classes=EVENT_CLASSES)
    background = build_engine(read_model, Queue(), NoWriteStore(store).as_store())
    background.bus = records.append
    background.run_procedure(OpenBatch(batch_id="starter"), lane="background")
    assert [(r["kind"], r.get("lane")) for r in records] == [("procedure", "background")]
    assert len(store) == 0  # nothing was appended by the half that cannot


# ── Priority: a third claim order, and what it does NOT weaken ──────────────
#
# The durable queue can serve interactive commands ahead of queued batch ones.
# That is a claim-ORDER change on the COMMAND queue alone: the engine's event
# queue stays FIFO, because event order is log order and log order is the fold.
# What follows pins that priority buys latency without spending st's guarantee.

COMMAND_CLASSES = {"OpenBatch": OpenBatch, "AdvanceBatch": AdvanceBatch}

# Opened in dependency order, so each batch is ready the moment it opens and
# the policy's AdvanceBatch has queued OpenBatch work to overtake. The reversed
# SCRIPT cannot show the reordering: nothing is dispatched until the last of
# the three has already been claimed.
READY_SCRIPT = [OpenBatch(batch_id=b, requires=r) for b, r in CHAIN]


def _priority_of(command) -> int:
    """Non-trivial, and a pure function of command TYPE — which is exactly why
    the reordering it causes is still deterministic."""
    from dizzy.engine.st import PRIORITY_BATCH, PRIORITY_INTERACTIVE

    return PRIORITY_INTERACTIVE if isinstance(command, AdvanceBatch) else PRIORITY_BATCH


def run_durable(tmp_path, name: str, priority_of=None) -> tuple[ReadModel, EventStore, list[str]]:
    """st over the REAL durable queue, drained by one worker to exhaustion.

    Same wiring and same script as everything else here; ``priority_of`` is the
    only variable. Returns the order commands were actually claimed in, so a
    test can show the reordering happened rather than assume it.
    """
    from dizzy.engine.st import DurableCommandQueue

    tmp_path.mkdir(parents=True, exist_ok=True)
    read_model = ReadModel()
    store = EventStore(path=tmp_path / f"{name}.db", event_classes=EVENT_CLASSES)
    kwargs = {"priority_of": priority_of} if priority_of else {}
    queue = DurableCommandQueue(
        registry=COMMAND_CLASSES, path=tmp_path / f"{name}-queue.db", **kwargs
    )
    engine = build_engine(read_model, queue, store)
    claimed: list[str] = []
    try:
        for command in READY_SCRIPT:
            queue.put(command, origin="external")
        while True:
            job = queue.claim(timeout=0.05)
            if job is None:
                break
            job_id, command = job
            claimed.append(f"{type(command).__name__}:{command.batch_id}")
            engine.run_command(command)
            queue.mark_done(job_id)
    finally:
        queue.close()
    return read_model, store, claimed


def test_an_interactive_command_jumps_a_queue_of_batch_work(tmp_path):
    """(a) The latency claim, in isolation: arriving twenty-first does not mean
    running twenty-first. Queue-POSITION latency only — a command already
    executing still holds the lane, and that is a lane problem, not an order
    one."""
    from dizzy.engine.st import DurableCommandQueue

    queue = DurableCommandQueue(
        registry=COMMAND_CLASSES, path=tmp_path / "jump.db", priority_of=_priority_of
    )
    try:
        for i in range(20):
            queue.put(OpenBatch(batch_id=f"b{i}"))
        queue.put(AdvanceBatch(batch_id="urgent"))
        job = queue.claim(timeout=1)
        assert job is not None and job[1] == AdvanceBatch(batch_id="urgent")
    finally:
        queue.close()


def test_priority_does_not_cost_the_sequential_shell_its_determinism(tmp_path):
    """(b) st promises DETERMINISM, not FIFO — the two coincide only while
    nothing else decides the order. Priority is a pure function of command
    type, so the claim order remains a function of the script alone and two
    runs cannot diverge: same claim order, same read models, same stream."""
    first, first_store, first_order = run_durable(tmp_path / "a", "p", _priority_of)
    second, second_store, second_order = run_durable(tmp_path / "b", "p", _priority_of)
    assert first_order == second_order
    assert first.snapshot() == second.snapshot()
    assert [(e.type, e.payload) for e in first_store.iterate()] == [
        (e.type, e.payload) for e in second_store.iterate()
    ]


def test_priority_defers_batch_work_without_skipping_it(tmp_path):
    """(c) The starvation bound that actually matters: over a finite run,
    deferring is not dropping. The prioritized run claims commands in a
    genuinely different order from the FIFO one and still reaches the same
    quiescent state, having run every command exactly once."""
    fifo, _, fifo_order = run_durable(tmp_path / "fifo", "f")
    prioritized, _, prio_order = run_durable(tmp_path / "prio", "p", _priority_of)

    assert prio_order != fifo_order  # the reordering is real
    assert sorted(prio_order) == sorted(fifo_order)  # and nothing was skipped
    assert prioritized.snapshot() == fifo.snapshot()
    # Concretely: an AdvanceBatch overtook batch work still sitting in the queue.
    first_advance = next(i for i, c in enumerate(prio_order) if c.startswith("AdvanceBatch"))
    assert first_advance < max(i for i, c in enumerate(prio_order) if c.startswith("OpenBatch"))


# ── What the split costs on the READ side ───────────────────────────────────
#
# ``run_split`` above hands both engines ONE read model, which is what a host
# splitting the halves inside a process has. A pooled child holds its own, and
# the write path says nothing about that: `run_procedure` touches no STORE, but
# a procedure that queries still reads a model. The two tests below pin which
# half of that is the split's property and which is a constraint on the host.


def _sequential(tmp_path, script) -> tuple[ReadModel, EventStore]:
    """``run_sequential``'s loop over any script — the reference answer."""
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "seq.db", event_classes=EVENT_CLASSES)
    queue = Queue()
    engine = build_engine(read_model, queue, store)
    for command in script:
        engine.run_command(command)
        while queue.qsize():
            engine.run_command(queue.items.popleft())
    return read_model, store


def _split(tmp_path, script, own_model: bool) -> tuple[ReadModel, EventStore]:
    """``run_split``'s loop, with the procedure engine's read model a choice:
    the lane's own (a host splitting in-process) or one of its own (a pooled
    child that was handed nothing)."""
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "split.db", event_classes=EVENT_CLASSES)
    queue = Queue()
    main = build_engine(read_model, queue, store)
    background = build_engine(
        ReadModel() if own_model else read_model, Queue(), NoWriteStore(store).as_store()
    )

    def step(command) -> None:
        main.ingest(background.run_procedure(command, lane="background"))

    for command in script:
        step(command)
        while queue.qsize():
            step(queue.items.popleft())
    return read_model, store


def test_the_split_survives_a_script_that_exercises_the_read_side(tmp_path):
    """READY_SCRIPT opens each batch after its requirement exists, so every
    procedure's emit turns on what the model already carries — the reversed
    SCRIPT never puts that question. Given the committed model, the split is
    still the sequential answer, stream included."""
    split, split_store = _split(tmp_path / "a", READY_SCRIPT, own_model=False)
    sequential, sequential_store = _sequential(tmp_path / "b", READY_SCRIPT)
    assert split.snapshot() == sequential.snapshot()
    assert [(e.type, e.payload) for e in split_store.iterate()] == [
        (e.type, e.payload) for e in sequential_store.iterate()
    ]


def test_a_procedure_run_against_a_model_of_its_own_decides_on_stale_state(tmp_path):
    """The constraint, stated as a test rather than left to be discovered: a
    host that moves the procedure half elsewhere owes it a model carrying what
    `ingest` has committed. Given a model of its own, nothing here reorders —
    the procedure simply reads a model no fold ever reached, opens every batch
    `blocked`, and the cascade stalls. The engine cannot detect that, which is
    why it is named here."""
    stale, _ = _split(tmp_path / "a", READY_SCRIPT, own_model=True)
    sequential, _ = _sequential(tmp_path / "b", READY_SCRIPT)
    assert stale.snapshot() != sequential.snapshot()
    batches, entities, _ = stale.snapshot()
    assert sorted(entities) == ["starter"]  # the chain never advanced past the first
    assert batches["loaf"] == "blocked"


# ── A fourth scheduling model: st with POOL lanes ───────────────────────────
#
# ``dizzy.engine.st.executor.LaneExecutor`` is st with more lanes, not a third
# shell — but a POOL lane changes what st promises, and that has to be said out
# loud rather than discovered. An inline lane is sequentially consistent. A pool
# lane runs its procedure CONCURRENTLY with the main lane and hands the events
# back to be ingested, so the log order is a race and only the read models are
# claimed. That is mp's guarantee arriving inside st, and these tests pin both
# halves: convergent, and NOT sequentially consistent.
#
# The pools here are in-process doubles. Everything asserted below is
# parent-side — the ack rule, the fold order, the reorder buffer — and spawning
# real children to assert it would buy nothing but seconds. The boundary itself
# (spawn, pickling, HostApp resolution) is covered in ``test_st_executor.py``.

POOL_LANES = {"main": ("inline", 1), "background": ("pool", 2)}


class _InProcessPool:
    """Run the procedure on submit, in the calling thread, on a SECOND engine.

    Immediate rather than threaded, deliberately: a real pool runs one task at
    a time per worker, and that is what makes the child's per-process runtime
    safe. A threaded double would share one engine's event queue and race.
    """

    def __init__(self, engine, results: list | None = None):
        from dizzy.engine.st.executor import ProcedureResult

        self._engine = engine
        self._result = ProcedureResult
        self.results = results if results is not None else []

    def _run(self, lane, command):
        try:
            events = self._engine.run_procedure(command, lane=lane)
        except Exception as exc:  # noqa: BLE001 - the child returns, never raises
            self._engine.discard_pending_events()
            return self._result(error=f"{type(exc).__name__}: {exc}", pid=-1)
        return self._result(events=list(events), pid=-1)

    def submit(self, fn, spec, sys_path, lane, command):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_running_or_notify_cancel()
        future.set_result(self._run(lane, command))
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class _ReversingPool(_InProcessPool):
    """Compute on submit, but DELIVER newest-first, in pairs.

    A deterministic reversal of the completion order with no sleeping and no
    timing: results are held until two are pending (the lane is two workers
    wide) and then released in reverse. A lone straggler is released by
    :meth:`release` once the test can see the queue has nothing left to claim,
    so the drain is a function of queue state rather than of wall clock.
    """

    def __init__(self, engine):
        super().__init__(engine)
        self._pending: list = []

    def submit(self, fn, spec, sys_path, lane, command):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_running_or_notify_cancel()
        self._pending.append((future, self._run(lane, command)))
        return future

    def release(self, counts: dict) -> bool:
        """Deliver, newest-first, once every claimed row is held here.

        Gated on the QUEUE rather than on a clock: ``running`` counts exactly
        the rows a lane has claimed and the writer has not yet acked, so
        ``running == len(pending)`` means every row this lane holds has reached
        the double and nothing is in flight behind a claim it has not seen yet.
        Not gated on ``queued`` being empty as well: a two-worker lane with a
        third row still queued would then never release, and the run would
        deadlock rather than reorder.
        """
        if not self._pending or len(self._pending) != counts.get("running"):
            return False
        held, self._pending = self._pending, []
        for future, result in reversed(held):
            future.set_result(result)
        return True


def _lane_of(command) -> str:
    return "background"  # every command is pooled: one lane, maximum exposure


def run_pooled(tmp_path, name: str, pool_cls, ordered: bool) -> tuple[ReadModel, EventStore]:
    """st driven by ``LaneExecutor``, with every command on one POOL lane.

    Two engines: the writer's, and the one standing in for a child. The
    child's cannot append — ``NoWriteStore`` refuses — and its dispatches go
    nowhere, which is the invariant the executor exists to keep.

    Two deliberate departures from a real deployment, said out loud rather
    than implied:

    - the child is handed the parent's LIVE ``ReadModel``. A spawned child
      cannot have that; what it has is a model somebody keeps current, which
      is exactly the debt ``Engine.run_procedure`` names for a pooling host.
      Sharing the object here is the strongest version of that assumption, and
      the run converges with an isolated one too.
    - both fixture procedures QUERY a read model, and the shipped topology
      validator refuses ``executor: pool`` for exactly that (rule 3 in
      ``topology_loader``). They are pooled anyway, to maximise the exposure
      this file is trying to measure — a legal topology would not place them
      here, and nothing below should be read as saying it could.
    """
    from dizzy.engine.st import DurableCommandQueue, LaneExecutor

    tmp_path.mkdir(parents=True, exist_ok=True)
    read_model = ReadModel()
    store = EventStore(path=tmp_path / f"{name}.db", event_classes=EVENT_CLASSES)
    command_queue = DurableCommandQueue(
        registry=COMMAND_CLASSES, path=tmp_path / f"{name}-queue.db", lane_of=_lane_of
    )
    main = build_engine(read_model, command_queue, store)
    child = build_engine(read_model, Queue(), NoWriteStore(store).as_store())
    pool = pool_cls(child)
    try:
        for command in SCRIPT:
            command_queue.put(command, origin="external")
        executor = LaneExecutor(
            command_queue,
            main,
            lanes=POOL_LANES,
            pool_factory=lambda lane, workers: pool,
            ordered=ordered,
        )
        with executor:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                worked = executor.pump()
                if executor.idle():
                    break
                # Every claimed row is held in the double: let them go, in
                # whatever order the double has decided on.
                release = getattr(pool, "release", None)
                if release is not None and release(command_queue.counts()):
                    worked = True
                if not worked:
                    time.sleep(0.001)
        assert executor.idle(), "the pooled run never reached quiescence"
    finally:
        command_queue.close()
    return read_model, store


def test_a_pool_lane_converges_on_the_defined_read_models(tmp_path):
    """The claim st-with-pool-lanes actually makes. Same wiring, same script,
    the procedures run somewhere else and their events come home to the one
    writer — and the read models are the sequential shell's, exactly."""
    pooled, _ = run_pooled(tmp_path / "pooled", "p", _InProcessPool, ordered=False)
    sequential, _ = run_sequential(tmp_path / "seq")
    assert pooled.snapshot() == sequential.snapshot()


def test_a_pool_lane_folding_in_completion_order_reorders_the_stream(tmp_path):
    """And the claim it does NOT make. Completion order is the default because
    a lane whose whole purpose is that a slow procedure must not hold the queue
    cannot also promise to fold in claim order. The stream differs from the
    sequential shell's; the read models do not."""
    pooled, pooled_store = run_pooled(tmp_path / "rev", "r", _ReversingPool, ordered=False)
    sequential, sequential_store = run_sequential(tmp_path / "seq")
    pooled_stream = [(e.type, e.payload) for e in pooled_store.iterate()]
    sequential_stream = [(e.type, e.payload) for e in sequential_store.iterate()]
    assert pooled_stream != sequential_stream  # the reordering is real
    assert sorted(map(str, pooled_stream)) == sorted(map(str, sequential_stream))
    assert pooled.snapshot() == sequential.snapshot()  # and it costs nothing


def test_the_reorder_buffer_buys_back_the_stream_one_lane_at_a_time(tmp_path):
    """``ordered=True`` folds one pool lane's results in the order that lane
    CLAIMED them. Here every command is on that one lane and the run drains to
    quiescence, which is the only shape in which that adds up to the sequential
    stream — and stating the shape is the point.

    It is NOT determinism in general. A pool procedure runs concurrently with
    the main lane, so whether its events land before or after the inline
    command the main lane happens to be running is a race, every time. A test
    that wants a deterministic log with an inline lane in play wants
    ``executor: inline``, which already exists."""
    ordered, ordered_store = run_pooled(tmp_path / "ord", "o", _ReversingPool, ordered=True)
    sequential, sequential_store = run_sequential(tmp_path / "seq")
    assert ordered.snapshot() == sequential.snapshot()
    assert [(e.type, e.payload) for e in ordered_store.iterate()] == [
        (e.type, e.payload) for e in sequential_store.iterate()
    ]


def test_an_inline_lane_keeps_the_sequential_guarantee_intact(tmp_path):
    """The executor is st with MORE lanes, not different ones: drive it with no
    pool lane at all and the shell's own promise is untouched — same stream,
    same models, and nothing spawned."""
    from dizzy.engine.st import DurableCommandQueue, LaneExecutor

    (tmp_path / "inline").mkdir(parents=True, exist_ok=True)
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "inline" / "i.db", event_classes=EVENT_CLASSES)
    command_queue = DurableCommandQueue(registry=COMMAND_CLASSES, path=tmp_path / "inline" / "q.db")
    engine = build_engine(read_model, command_queue, store)
    try:
        for command in SCRIPT:
            command_queue.put(command, origin="external")
        with LaneExecutor(command_queue, engine) as executor:
            assert executor.pool_lanes == ()
            assert executor.run_until_idle(10.0)
    finally:
        command_queue.close()
    sequential, sequential_store = run_sequential(tmp_path / "seq")
    assert read_model.snapshot() == sequential.snapshot()
    assert [(e.type, e.payload) for e in store.iterate()] == [
        (e.type, e.payload) for e in sequential_store.iterate()
    ]


# ── The guarantee table, asserted rather than described ─────────────────────
#
# Three lane classes ship, and each one makes a DIFFERENT promise. Above, each
# is pinned by its own test; what follows states the three side by side, so the
# table itself is a thing that fails when a promise moves. The floor is the
# same everywhere — the read models converge — and the STREAM is what
# discriminates: a class claiming sequential consistency must reproduce the
# sequential shell's log exactly, and the convergent class must not be held to
# it, because a pool procedure racing the main lane cannot promise it.

LANE_GUARANTEES = {
    "inline": "sequential",
    "pool": "convergent",
    "pool+reorder": "sequential",
}


def run_inline_lane(tmp_path) -> tuple[ReadModel, EventStore]:
    """The executor with no topology at all: one inline lane, nothing spawned."""
    from dizzy.engine.st import DurableCommandQueue, LaneExecutor

    tmp_path.mkdir(parents=True, exist_ok=True)
    read_model = ReadModel()
    store = EventStore(path=tmp_path / "inline.db", event_classes=EVENT_CLASSES)
    command_queue = DurableCommandQueue(registry=COMMAND_CLASSES, path=tmp_path / "inline-q.db")
    engine = build_engine(read_model, command_queue, store)
    try:
        for command in SCRIPT:
            command_queue.put(command, origin="external")
        with LaneExecutor(command_queue, engine) as executor:
            assert executor.pool_lanes == ()
            assert executor.run_until_idle(10.0)
    finally:
        command_queue.close()
    return read_model, store


def _stream(store: EventStore) -> list[tuple[str, dict]]:
    return [(e.type, e.payload) for e in store.iterate()]


@pytest.mark.parametrize("lane_class", sorted(LANE_GUARANTEES))
def test_each_lane_class_keeps_exactly_the_guarantee_it_claims(tmp_path, lane_class):
    """One table, one assertion per row.

    ``sequential`` is asserted as stream equality with the defined result;
    ``convergent`` is asserted as read-model equality AND stream INEQUALITY,
    because a weaker promise that happens to coincide is not evidence — a
    convergent lane that silently became sequential would be a design change,
    and this fails when it happens in either direction.
    """
    if lane_class == "inline":
        actual, actual_store = run_inline_lane(tmp_path / "inline")
    else:
        actual, actual_store = run_pooled(
            tmp_path / lane_class.replace("+", "-"),
            "g",
            _ReversingPool,
            ordered=lane_class.endswith("reorder"),
        )
    defined, defined_store = run_sequential(tmp_path / "defined")

    assert actual.snapshot() == defined.snapshot()  # the floor, everywhere
    if LANE_GUARANTEES[lane_class] == "sequential":
        assert _stream(actual_store) == _stream(defined_store)
    else:
        assert _stream(actual_store) != _stream(defined_store)
        assert sorted(map(str, _stream(actual_store))) == sorted(map(str, _stream(defined_store)))


def _mixed_lane_of(command) -> str:
    # The shape a real topology has: the slow, query-free procedure is pooled
    # and everything else stays on the main lane.
    return "background" if isinstance(command, OpenBatch) else "main"


def test_a_mixed_topology_converges_with_both_lane_kinds_live(tmp_path):
    """Both lane kinds at once, which is the only configuration a deployment
    actually ships: pooled procedures completing on a lane thread while the
    main lane claims, runs and ingests on its own. Nothing here can be timed
    into a fixed order — that is the point — and the read models are still the
    sequential shell's, exactly."""
    from dizzy.engine.st import DurableCommandQueue, LaneExecutor

    root = tmp_path / "mixed"
    root.mkdir(parents=True, exist_ok=True)
    read_model = ReadModel()
    store = EventStore(path=root / "m.db", event_classes=EVENT_CLASSES)
    command_queue = DurableCommandQueue(
        registry=COMMAND_CLASSES, path=root / "m-q.db", lane_of=_mixed_lane_of
    )
    main = build_engine(read_model, command_queue, store)
    child = build_engine(read_model, Queue(), NoWriteStore(store).as_store())
    try:
        for command in SCRIPT:
            command_queue.put(command, origin="external")

        def factory(lane: str, workers: int) -> Any:
            return _InProcessPool(child)

        executor = LaneExecutor(command_queue, main, lanes=POOL_LANES, pool_factory=factory)
        with executor:
            assert executor.run_until_idle(10.0)
    finally:
        command_queue.close()
    defined, _ = run_sequential(tmp_path / "defined")
    assert read_model.snapshot() == defined.snapshot()
    assert all(status == "completed" for status in read_model.batches.values())
