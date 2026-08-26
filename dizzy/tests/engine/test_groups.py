"""The consumer-group worker: a cursor, a doorbell, and what each may cost.

The loop is small; what has to be pinned is everything it is allowed to lose.
Three claims carry the design, and each is a test rather than a paragraph:

* the doorbell carries no work, so dropping EVERY notification costs latency
  and nothing else;
* the cursor trails committed work, so a crash between the two repeats an
  entry and never skips one;
* a cursor is disposable, so deleting one mid-run reaches the same state.

The folds here are deliberately idempotent (last write wins, set insert) —
that is the confluence the whole scheme rests on, and a fold that counted
would be measuring the test's own arithmetic rather than the loop's.
"""

from __future__ import annotations

import threading
import time

import pytest
from dizzy.engine.cursors import START, MemoryCursorStore, SqliteCursorStore
from dizzy.engine.groups import (
    GroupWorker,
    InProcessDoorbell,
    NullDoorbell,
    group_workers,
    shard_for,
)
from dizzy.engine.loop import Engine
from dizzy.engine.store import EventStore
from pydantic import BaseModel


class DefineRecipe(BaseModel):
    recipe_id: str


class StartBatch(BaseModel):
    recipe_id: str


class RecipeDefined(BaseModel):
    recipe_id: str
    servings: int = 1


class BatchOpened(BaseModel):
    recipe_id: str


EVENT_CLASSES = {"recipe_defined": RecipeDefined, "batch_opened": BatchOpened}


class Queue:
    """Stand-in for the shell's command queue — a policy's dispatch lands here."""

    def __init__(self):
        self.items = []
        self.lock = threading.Lock()

    def put(self, command, origin="policy"):
        with self.lock:
            self.items.append(command)


class Model:
    """The read model, and its transaction boundary.

    ``commit`` is what the engine calls between the fold and the policies; the
    worker never calls it, which is the point — the boundary stays the
    engine's even when the entry came off the log rather than off a procedure.
    """

    def __init__(self):
        self.recipes: dict[str, int] = {}
        self.committed: dict[str, int] = {}
        self.order: list[str] = []

    def fold(self, event, ingested_at=None):
        self.recipes[event.recipe_id] = event.servings
        self.order.append(f"fold:{event.recipe_id}")

    def commit(self):
        self.committed = dict(self.recipes)
        self.order.append("commit")


@pytest.fixture
def store(tmp_path):
    return EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)


@pytest.fixture
def engine(store):
    return Engine(command_queue=Queue(), store=store)


def append_recipes(store, count, first=0):
    return [store.append(RecipeDefined(recipe_id=f"r{i}", servings=i)) for i in range(first, count)]


# ── The loop ────────────────────────────────────────────────────────────────


def test_the_loop_folds_the_log_after_its_cursor_and_advances(engine, store):
    model = Model()
    engine.commit = model.commit
    envelopes = append_recipes(store, 5)
    cursors = MemoryCursorStore()
    worker = GroupWorker(
        "reads", engine, projections={RecipeDefined: [("recipe", model.fold)]}, cursors=cursors
    )

    assert worker.position() == START
    assert worker.fold_to_tail() == 5
    assert model.committed == {f"r{i}": i for i in range(5)}
    assert worker.position() == envelopes[-1].position
    # A second pass has nothing after the cursor: the watermark is what makes a
    # boot snapshot+tail, and it has to hold within a run too.
    assert worker.fold_to_tail() == 0


def test_the_group_runs_only_what_it_claims(engine, store):
    model, seen = Model(), []
    store.append(BatchOpened(recipe_id="r0"))
    store.append(RecipeDefined(recipe_id="r1", servings=1))
    store.append(BatchOpened(recipe_id="r2"))
    worker = GroupWorker(
        "recipes",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        policies={RecipeDefined: [("announce", lambda e: seen.append(e.recipe_id))]},
        cursors=MemoryCursorStore(),
    )

    assert worker.fold_to_tail() == 1
    assert seen == ["r1"]
    # The entries it does not claim are still passed: a watermark that stalled
    # on another group's events would replay the whole log every boot.
    assert worker.position() == 3


def test_the_read_model_commits_before_the_policy_runs(engine, store):
    """``ingest``'s ordering rule, carried across the split verbatim —
    this is what a policy-dispatched command is allowed to assume."""
    model = Model()
    engine.commit = model.commit
    store.append(RecipeDefined(recipe_id="r1", servings=2))
    worker = GroupWorker(
        "reads",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        policies={RecipeDefined: [("announce", lambda e: model.order.append("policy"))]},
        cursors=MemoryCursorStore(),
    )
    worker.fold_to_tail()

    assert model.order == ["fold:r1", "commit", "policy"]


def test_a_policys_dispatch_leaves_through_the_command_queue(engine, store):
    store.append(RecipeDefined(recipe_id="r1"))
    worker = GroupWorker(
        "reacts",
        engine,
        policies={
            RecipeDefined: [
                (
                    "open",
                    lambda e: engine.dispatch_command(
                        StartBatch(**e.model_dump(include={"recipe_id"}))
                    ),
                )
            ]
        },
        cursors=MemoryCursorStore(),
    )
    worker.fold_to_tail()

    assert [type(c).__name__ for c in engine.command_queue.items] == ["StartBatch"]


# ── The doorbell carries nothing ────────────────────────────────────────────


def test_a_dropped_doorbell_costs_latency_and_never_work(engine, store):
    """Every notification dropped, every entry still folded.

    :class:`NullDoorbell` is the transport switched off: ``ring`` does nothing
    and ``wait`` never reports one. If this test can be made to fail by a
    change to the loop, that change has moved correctness into the transport —
    and then a lost NATS message is lost work.
    """
    model = Model()
    engine.commit = model.commit
    cursors = MemoryCursorStore()
    worker = GroupWorker(
        "reads",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        cursors=cursors,
        doorbell=NullDoorbell(),
        poll_interval=0.01,
    )
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), daemon=True)
    thread.start()
    try:
        for round_ in range(3):
            append_recipes(store, 4 * (round_ + 1), first=4 * round_)
            worker.doorbell.ring()  # dropped on the floor, by construction
            deadline = time.monotonic() + 5
            while len(model.committed) < 4 * (round_ + 1) and time.monotonic() < deadline:
                time.sleep(0.005)
            assert len(model.committed) == 4 * (round_ + 1)
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_ring_is_what_buys_the_latency_back(engine, store):
    """The other half of the claim: dropping the bell costs latency, so ringing
    it must save that latency. The poll interval here is longer than the test's
    patience by two orders of magnitude — only the ring can wake this worker,
    and if the loop ever stopped waiting on the bell this would time out."""
    model = Model()
    engine.commit = model.commit
    worker = GroupWorker(
        "reads",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        cursors=MemoryCursorStore(),
        poll_interval=300,
    )
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), daemon=True)
    thread.start()
    try:
        append_recipes(store, 2)
        worker.doorbell.ring()
        deadline = time.monotonic() + 5
        while len(model.committed) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert model.committed == {"r0": 0, "r1": 1}
    finally:
        stop.set()
        worker.doorbell.ring()
        thread.join(timeout=5)


def test_a_bell_nobody_rings_still_spends_the_poll_interval():
    """A wait that returns immediately is not a poll, it is a spin loop: the
    worker would re-scan the log tens of thousands of times a second and burn a
    core while idle. The bell still carries nothing — it only sleeps."""
    bell = NullDoorbell()
    start = time.monotonic()
    assert bell.wait(0.05) is False
    assert time.monotonic() - start >= 0.04
    assert bell.wait(0) is False  # and "do not wait" still does not


def test_the_doorbell_holds_a_ring_that_arrives_between_cycles():
    """A latch, not a bare notify: the ring landing while nobody waits is the
    common case, and losing it would idle a worker for a whole poll interval
    every time. Latency only — which is why it is tested here and not in the
    loop."""
    bell = InProcessDoorbell()
    bell.ring()
    assert bell.wait(timeout=0) is True
    assert bell.wait(timeout=0) is False


def test_a_rung_doorbell_wakes_a_waiting_worker():
    bell = InProcessDoorbell()
    woke = threading.Event()

    def wait():
        if bell.wait(timeout=5):
            woke.set()

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    time.sleep(0.05)
    bell.ring()
    thread.join(timeout=5)
    assert woke.is_set()


# ── Crash safety: commit, THEN advance ──────────────────────────────────────


class CrashingCursors(MemoryCursorStore):
    """A cursor store that dies on the *n*th save — the crash window itself."""

    def __init__(self, die_on: int):
        super().__init__()
        self.die_on = die_on
        self.saves = 0

    def save(self, group: str, position: int) -> int:
        self.saves += 1
        if self.saves == self.die_on:
            raise KeyboardInterrupt("the process died between the commit and the save")
        return super().save(group, position)


def test_a_crash_between_the_commit_and_the_save_repeats_work_and_never_loses_it(engine, store):
    model, folded = Model(), []
    engine.commit = model.commit
    append_recipes(store, 4)
    cursors = CrashingCursors(die_on=3)

    def fold(event, ingested_at=None):
        folded.append(event.recipe_id)
        model.fold(event, ingested_at)

    def worker(cursor_store):
        return GroupWorker(
            "reads", engine, projections={RecipeDefined: [("recipe", fold)]}, cursors=cursor_store
        )

    with pytest.raises(KeyboardInterrupt):
        worker(cursors).fold_to_tail()

    # The third entry was folded and committed; its save is what did not happen.
    assert folded == ["r0", "r1", "r2"]
    assert cursors.load("reads") == 2

    survivor = MemoryCursorStore(cursors.groups().items())
    worker(survivor).fold_to_tail()

    # r2 is folded a second time — at-least-once, in the only direction that is
    # recoverable — and nothing is skipped.
    assert folded == ["r0", "r1", "r2", "r2", "r3"]
    assert model.committed == {f"r{i}": i for i in range(4)}


def test_the_cursor_never_runs_ahead_of_the_effect(engine, store):
    """The other half of the same rule: an element that raises must leave the
    cursor where it was, or the entry is lost with no record that it was."""
    append_recipes(store, 3)
    cursors = MemoryCursorStore()
    attempts = []

    def fold(event, ingested_at=None):
        attempts.append(event.recipe_id)
        if event.recipe_id == "r1" and attempts.count("r1") == 1:
            raise RuntimeError("the element raised")

    worker = GroupWorker(
        "reads", engine, projections={RecipeDefined: [("recipe", fold)]}, cursors=cursors
    )
    with pytest.raises(RuntimeError):
        worker.fold_to_tail()
    assert cursors.load("reads") == 1

    assert worker.fold_to_tail() == 2
    assert attempts == ["r0", "r1", "r1", "r2"]


# ── Cursors are disposable ──────────────────────────────────────────────────


def test_deleting_the_cursor_mid_run_reaches_the_same_state(engine, store, tmp_path):
    """The disposability property, now through the loop rather than the store.

    Half the log folded, the cursor thrown away, the rest of the run made from
    zero: the state must be the one the undisturbed run reaches. Anything that
    made a cursor load-bearing — a fold that counts, a policy that assumes it
    is seeing an entry for the first time — fails here.
    """
    append_recipes(store, 6)

    def run(lose_cursor: bool) -> dict:
        model = Model()
        engine.commit = model.commit
        cursors = SqliteCursorStore(tmp_path / f"cursors-{lose_cursor}.db")
        worker = GroupWorker(
            "reads",
            engine,
            projections={RecipeDefined: [("recipe", model.fold)]},
            cursors=cursors,
            batch=3,
        )
        worker.fold_to_tail()
        if lose_cursor:
            cursors.rewind("reads", START)
        while worker.fold_to_tail():
            pass
        cursors.close()
        return model.committed

    kept = run(lose_cursor=False)
    lost = run(lose_cursor=True)
    assert lost == kept == {f"r{i}": i for i in range(6)}


def test_an_unknown_group_starts_at_the_beginning_rather_than_failing(engine, store):
    """A missing cursor is a normal answer, not a fault — the case a boot with
    a fresh sqlite file hits, and the one a worker must not treat as an error."""
    append_recipes(store, 2)
    model = Model()
    worker = GroupWorker(
        "never-seen",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        cursors=MemoryCursorStore(),
    )
    assert worker.position() == START
    assert worker.fold_to_tail() == 2


def test_a_group_that_reads_the_log_refuses_to_invent_a_cursor_store(engine):
    worker = GroupWorker(
        "reads", engine, projections={RecipeDefined: [("recipe", lambda e, at: None)]}
    )
    with pytest.raises(ValueError, match="no cursor store"):
        worker.fold_to_tail()


# ── Two intakes: the hybrid cycle ───────────────────────────────────────────


def hybrid(engine, store, cursors, model, dispatched):
    """A group claiming a procedure, a projection and a policy — the common
    shape, per rule 6. The engine is registered with exactly this group's
    elements, as a topology-generated wiring would register it."""
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id=c.recipe_id, servings=1))
    )
    engine.register_projection(RecipeDefined, model.fold, name="recipe")
    engine.register_policy(RecipeDefined, lambda e: dispatched.append(e.recipe_id), name="announce")
    pending = [DefineRecipe(recipe_id="r1")]

    def commands() -> bool:
        if not pending:
            return False
        engine.run_command(pending.pop(0))
        return True

    return GroupWorker(
        "kitchen",
        engine,
        projections=engine.projection_runners(),
        policies=engine.policy_runners(),
        cursors=cursors,
        commands=commands,
    )


def test_a_hybrid_worker_does_not_run_its_own_commands_effects_twice(engine, store):
    """The command's cascade is folded inline by ``run_command``; the trailing
    fold walks the very same rows. Applying them again would re-dispatch every
    policy on every command — the tolerated repeat is a crash, not the happy
    path."""
    model, dispatched = Model(), []
    engine.commit = model.commit
    cursors = MemoryCursorStore()
    worker = hybrid(engine, store, cursors, model, dispatched)

    assert worker.step() is True
    assert dispatched == ["r1"]
    assert model.committed == {"r1": 1}
    # ...and the cursor has still passed the entry, so a later cycle does not
    # find it either.
    assert cursors.load("kitchen") == 1
    assert worker.step() is False
    assert dispatched == ["r1"]


def test_a_hybrid_worker_folds_to_tail_before_it_runs_a_command(engine, store):
    """The leading fold is what carries the guarantee across a group split: a
    command claimed from the lane must see the model already carrying the
    entries that caused it."""
    model, dispatched, seen = Model(), [], []
    engine.commit = model.commit
    worker = hybrid(engine, store, MemoryCursorStore(), model, dispatched)
    engine.register_procedure(
        StartBatch, lambda c: seen.append(dict(model.committed)), name="start_batch"
    )
    # Another group's fact, appended straight to the log.
    store.append(RecipeDefined(recipe_id="r0", servings=9))
    worker.commands = lambda: (engine.run_command(StartBatch(recipe_id="r0")), True)[1]

    worker.step()
    assert seen == [{"r0": 9}]


def test_a_procedure_only_group_holds_no_cursor(engine, store):
    """The decided answer to "what does a procedure group's cursor read?" is
    that it does not have one: commands are not in the log, so there is no
    position to hold. Given no cursor store, this group must still run."""
    ran = []
    engine.register_procedure(DefineRecipe, lambda c: ran.append(c.recipe_id))
    pending = [DefineRecipe(recipe_id="r1")]

    def commands() -> bool:
        if not pending:
            return False
        engine.run_command(pending.pop(0))
        return True

    worker = GroupWorker("procedures", engine, commands=commands)
    assert worker.reads_log is False
    assert worker.step() is True
    assert worker.step() is False
    assert ran == ["r1"]
    assert worker.position() == START


# ── workers: N, three answers ───────────────────────────────────────────────


def test_a_group_holding_a_projection_refuses_more_than_one_worker(engine):
    with pytest.raises(ValueError, match="cannot be sharded"):
        group_workers(
            "reads",
            engine,
            projections={RecipeDefined: [("recipe", lambda e, at: None)]},
            workers=2,
            cursors=MemoryCursorStore(),
        )


def test_sharded_policy_workers_each_hold_their_own_cursor(engine):
    workers = group_workers(
        "reacts",
        engine,
        policies={RecipeDefined: [("announce", lambda e: None)]},
        workers=3,
        cursors=MemoryCursorStore(),
    )
    assert [w.cursor_name for w in workers] == ["reacts#0/3", "reacts#1/3", "reacts#2/3"]
    # One name per shard, never one name shared: `save` is monotone, so a
    # shared cursor's leading worker would cover a prefix the laggard never
    # committed and the crash there would lose that work silently.
    assert len({w.cursor_name for w in workers}) == 3


def test_sharded_policy_workers_split_the_stream_exactly_once(engine, store):
    """Every entry acted on by exactly one shard, and every shard's watermark
    at the tail — a skipped entry is DONE, by ownership, so its position is
    saved as surely as an acted-on one."""
    seen: dict[int, list[str]] = {0: [], 1: [], 2: []}
    envelopes = append_recipes(store, 40)
    cursors = MemoryCursorStore()
    workers = group_workers(
        "reacts",
        engine,
        policies={RecipeDefined: [("announce", lambda e: None)]},
        workers=3,
        cursors=cursors,
    )
    for k, worker in enumerate(workers):
        worker.policies = {
            RecipeDefined: [("announce", lambda e, k=k: seen[k].append(e.recipe_id))]
        }
        worker.fold_to_tail()

    everything = [e.payload["recipe_id"] for e in store.iterate_from(START)]
    assert sorted(sum(seen.values(), [])) == sorted(everything)
    assert all(seen[k] for k in seen), "hash-mod ownership left a shard idle"
    for worker in workers:
        assert cursors.load(worker.cursor_name) == envelopes[-1].position


def test_ownership_follows_the_content_hash_not_the_position(engine, store):
    """On the id, because a position is node-local: two nodes inserting the
    same facts in different orders must still agree on who owns what."""
    # Fixed ids, because the function is pure and the property is about the
    # hash: content hashes carry ingested_at, so ids differ every run and an
    # assertion that 20 live ones cover 4 shards fails roughly once in 80.
    assert shard_for("00000000" + "ff" * 28, 4) == 0
    assert shard_for("00000001" + "ff" * 28, 4) == 1
    assert shard_for("deadbeef" + "00" * 28, 4) == shard_for("deadbeef" + "ff" * 28, 4)
    for envelope in append_recipes(store, 20):
        assert 0 <= shard_for(envelope.id, 4) < 4


def test_resharding_is_a_replay_from_the_lowest_cursor(engine, store):
    """Changing N invalidates every assignment, so the only safe reshard is to
    rewind every cursor to the LOWEST and replay. Named here because it is the
    price of the sharded answer, and a test is the only place it stays true:
    the shard that was ahead re-delivers the entries it had already handled,
    and no entry any shard had not passed is skipped.
    """
    append_recipes(store, 30)
    cursors = MemoryCursorStore()
    two = group_workers(
        "reacts",
        engine,
        policies={RecipeDefined: [("a", lambda e: None)]},
        workers=2,
        cursors=cursors,
    )
    for k, worker in enumerate(two):
        worker.policies = {RecipeDefined: [("a", lambda e: None)]}
        if k == 1:  # one shard deliberately left behind the other
            worker.batch = 3
        worker.fold_to_tail()
    positions = [cursors.load(w.cursor_name) for w in two]
    assert positions[0] > positions[1], "the fixture meant to leave one shard behind"

    lowest = min(positions)
    after: list[str] = []
    for worker in group_workers(
        "reacts",
        engine,
        policies={RecipeDefined: [("a", lambda e: None)]},
        workers=3,
        cursors=cursors,
    ):
        cursors.rewind(worker.cursor_name, lowest)
        worker.policies = {RecipeDefined: [("a", lambda e: after.append(e.recipe_id))]}
        worker.fold_to_tail()

    tail = [e.payload["recipe_id"] for e in store.iterate_from(lowest)]
    # Every entry past the lowest watermark delivered exactly once under the
    # new assignment — including those the leading shard had already handled.
    # Safe only because these policies read nothing and their commands dedupe
    # on deterministic ids.
    assert sorted(after) == sorted(tail)


def test_two_workers_on_one_lane_claim_each_command_once_in_order(tmp_path):
    """The procedure-only answer: N workers, one lane, no cursor. What the
    queue's claim promises is that the rows are handed out in id order and
    each one to a single worker — IN THIS PROCESS. `claim` is a SELECT then an
    UPDATE under a `threading.Condition`, so two workers in one process are
    serialized here and two processes on one file are not; that is the stated
    prerequisite of the answer, and this test is the half of it that can be
    made true from here.
    """
    from dizzy.engine.st.command_queue import DurableCommandQueue

    queue = DurableCommandQueue(
        {"DefineRecipe": DefineRecipe}, path=tmp_path / "commands.db", lane_of=lambda c: "kitchen"
    )
    try:
        for i in range(24):
            queue.put(DefineRecipe(recipe_id=f"r{i}"))

        claimed: list[str] = []
        lock = threading.Lock()

        def drain():
            while True:
                job = queue.claim(timeout=0.2, lane="kitchen")
                if job is None:
                    return
                job_id, command = job
                with lock:
                    claimed.append(command.recipe_id)
                queue.mark_done(job_id)

        threads = [threading.Thread(target=drain, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert claimed == [f"r{i}" for i in range(24)]
    finally:
        queue.close()


def test_stop_stops_the_run_the_caller_started(engine):
    """`run` takes the caller's Event, so `stop()` has to set THAT one — a stop
    method that quietly sets a different flag is worse than none."""
    worker = GroupWorker("procedures", engine, commands=lambda: False, poll_interval=300)
    thread = threading.Thread(target=worker.run, args=(threading.Event(),), daemon=True)
    thread.start()
    worker.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_procedure_only_group_puts_every_worker_on_one_lane(engine):
    """The third answer, which the constructor has to be able to build: no
    cursor is involved, so there is no shard either and all N workers are the
    whole group."""
    workers = group_workers("kitchen", engine, workers=3, commands=lambda: False)
    assert [w.cursor_name for w in workers] == ["kitchen"] * 3
    assert not any(w.reads_log for w in workers)


def test_a_cursor_past_the_end_of_the_log_re_folds_instead_of_stalling(engine, store):
    """Cursors live in a file of their own, so restoring the event store while
    the cursor store survives leaves every group ahead of a log it has never
    seen. Obeying that cursor is a permanent silent stall; the only safe repair
    is the replay disposability already pays for."""
    model = Model()
    engine.commit = model.commit
    append_recipes(store, 3)
    cursors = MemoryCursorStore([("reads", 999)])
    worker = GroupWorker(
        "reads",
        engine,
        projections={RecipeDefined: [("recipe", model.fold)]},
        cursors=cursors,
    )
    assert worker.fold_to_tail() == 3
    assert model.committed == {"r0": 0, "r1": 1, "r2": 2}
    assert cursors.load("reads") == 3
    # ...and a cursor merely AT the tail is left exactly where it is.
    assert worker.fold_to_tail() == 0
    assert cursors.load("reads") == 3


def test_a_group_that_runs_commands_refuses_a_capped_fold(engine):
    """batch bounds a cycle, and for a hybrid worker the leading fold is not a
    cycle to bound: stopping it short of the tail hands the command a model
    missing entries it should have seen."""
    with pytest.raises(ValueError, match="cannot also cap its fold"):
        GroupWorker(
            "kitchen",
            engine,
            projections={RecipeDefined: [("recipe", lambda e, at: None)]},
            cursors=MemoryCursorStore(),
            commands=lambda: False,
            batch=3,
        )


def test_two_groups_over_one_engine_advance_independently(engine, store):
    """Two groups is a SEMANTIC decision, and this is what it buys: separate
    cursors over one log, each free to be behind the other."""
    model, reacted = Model(), []
    engine.commit = model.commit
    cursors = MemoryCursorStore()
    folds = GroupWorker(
        "folds", engine, projections={RecipeDefined: [("recipe", model.fold)]}, cursors=cursors
    )
    reacts = GroupWorker(
        "reacts",
        engine,
        policies={RecipeDefined: [("announce", lambda e: reacted.append(e.recipe_id))]},
        cursors=cursors,
    )
    append_recipes(store, 5)
    folds.fold_to_tail()
    assert cursors.load("folds") == 5
    assert cursors.load("reacts") == START

    reacts.fold_to_tail()
    assert reacted == [f"r{i}" for i in range(5)]


def test_a_hybrid_worker_that_ingests_twice_in_one_cycle_still_dispatches_once(engine, store):
    """The pooled shape: a unit of work runs its procedures somewhere else and
    ingests each batch as it comes back.

    That is several ingests inside ONE cycle, and the trailing fold skips what
    the cycle applied — so every batch's positions have to survive until the
    worker asks for them. Losing the earlier ones re-folds and re-dispatches
    that batch, which is the very thing the skip-list exists to prevent.
    """
    model, dispatched = Model(), []
    engine.commit = model.commit
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id=c.recipe_id, servings=1))
    )
    engine.register_projection(RecipeDefined, model.fold, name="recipe")
    engine.register_policy(RecipeDefined, lambda e: dispatched.append(e.recipe_id), name="announce")

    def commands() -> bool:
        for recipe_id in ("r1", "r2"):
            engine.ingest(engine.run_procedure(DefineRecipe(recipe_id=recipe_id)))
        return True

    worker = GroupWorker(
        "kitchen",
        engine,
        projections=engine.projection_runners(),
        policies=engine.policy_runners(),
        cursors=MemoryCursorStore(),
        commands=commands,
    )

    assert worker.step() is True
    assert dispatched == ["r1", "r2"]
