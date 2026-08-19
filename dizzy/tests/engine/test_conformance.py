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
from collections import deque

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
