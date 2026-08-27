"""The ``st`` lane executor: N lanes, one writer, and what the pool lane costs.

Almost every property this module has is PARENT-side logic — the ack rule, the
reorder buffer, the in-flight bound, the broken-pool retry, the unit boundary —
and an in-process double for ``pool_factory`` exercises all of it at zero spawn
cost. Three tests at the bottom spawn for real, because pickling, resolving a
HostApp in a child and actual process isolation cannot be faked.

Nothing here sleeps as a synchronisation primitive: every wait is on an Event,
a Future, or a bounded deadline loop.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest
from _pool_app import (
    COMMAND_CLASSES,
    PoolBoom,
    PoolOpenProbe,
    PoolPing,
    PoolPinged,
    PoolRendezvous,
    PoolSleep,
    PoolStoreProbe,
    report_queue_recovery,
)
from dizzy.engine.loop import Engine
from dizzy.engine.ports import in_pool_child, mark_pool_child
from dizzy.engine.st import DurableCommandQueue, LaneExecutor
from dizzy.engine.st.executor import (
    POOL_MAX_ATTEMPTS,
    ProcedureResult,
    _RefusingQueue,
    run_procedure_job,
    spawn_pool,
)
from dizzy.engine.store import EventStore
from pydantic import BaseModel

DEADLINE = 5.0
"""Every bounded wait in this file. A broken build fails in seconds; it never
hangs the suite."""


class InlineTick(BaseModel):
    key: str


ALL_COMMANDS = {**COMMAND_CLASSES, "InlineTick": InlineTick}
EVENT_CLASSES = {"pool_pinged": PoolPinged}


# ── The fixture host ────────────────────────────────────────────────────────


class Host:
    """One writer's engine, its store, its queue, and what it folded."""

    def __init__(self, tmp_path, lane_of=None, poison: set[str] | None = None):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.folded: list[str] = []
        self.poison = poison or set()
        self.store = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
        self.queue = DurableCommandQueue(
            registry=ALL_COMMANDS, path=tmp_path / "queue.db", lane_of=lane_of
        )
        self.engine = Engine(command_queue=self.queue, store=self.store)
        self.engine.register_projection(PoolPinged, self._fold, name="folded")
        self.engine.register_procedure(InlineTick, self._tick, name="tick")
        self.engine.register_procedure(PoolPing, self._tick, name="ping")

    def _fold(self, event: PoolPinged, ingested_at) -> None:
        if event.key in self.poison:
            raise RuntimeError(f"projection refuses {event.key}")
        self.folded.append(event.key)

    def _tick(self, command) -> None:
        self.engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    def close(self) -> None:
        self.queue.close()

    def statuses(self) -> dict[str, str]:
        return {j["label"] or str(j["id"]): j["status"] for j in self.queue.jobs()}

    def status_of(self, job_id: int) -> str:
        return next(j["status"] for j in self.queue.jobs() if j["id"] == job_id)


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path / "h")
    yield h
    h.close()


# ── The doubles ─────────────────────────────────────────────────────────────


def _result(command) -> ProcedureResult:
    return ProcedureResult(events=[PoolPinged(key=command.key, pid=4242)], pid=4242)


class ImmediateExecutor:
    """Runs the job on submit, in the calling thread.

    IMMEDIATE rather than threaded, deliberately: a real pool runs one task at
    a time per worker, which is what makes the child's runtime singleton safe.
    A ThreadPoolExecutor double would share one engine's event deque across
    threads and race — so the double must not be one.
    """

    def __init__(self, lane: str, workers: int, handler=_result):
        self.lane, self.workers, self.handler = lane, workers, handler
        self.submitted: list[object] = []

    def submit(self, fn, spec, sys_path, lane, command):
        self.submitted.append(command)
        future: Future = Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(self.handler(command))
        except BaseException as exc:  # noqa: BLE001 - mirrors a pool's own behaviour
            future.set_exception(exc)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class DeferredExecutor:
    """Holds every submitted job until the test resolves it BY NAME.

    This is how completion order, dispatch order and the in-flight bound get
    asserted on ORDER rather than on elapsed time.
    """

    def __init__(self, lane: str, workers: int):
        self.lane, self.workers = lane, workers
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._arrived = threading.Event()
        self.shutdowns = 0

    def submit(self, fn, spec, sys_path, lane, command):
        future: Future = Future()
        future.set_running_or_notify_cancel()
        with self._lock:
            self._futures[command.key] = future
        self._arrived.set()
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdowns += 1

    # -- test-side control -------------------------------------------------

    def await_job(self, key: str, deadline: float = DEADLINE) -> Future:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            with self._lock:
                if key in self._futures:
                    return self._futures[key]
            self._arrived.wait(0.01)
            self._arrived.clear()
        raise AssertionError(f"job {key!r} was never dispatched")

    def finish(self, key: str, deadline: float = DEADLINE) -> None:
        self.await_job(key, deadline).set_result(_result_for(key))

    def break_pool(self, key: str, deadline: float = DEADLINE) -> None:
        self.await_job(key, deadline).set_exception(BrokenProcessPool("worker died"))

    def dispatched(self) -> set[str]:
        with self._lock:
            return set(self._futures)


def _result_for(key: str) -> ProcedureResult:
    return ProcedureResult(events=[PoolPinged(key=key, pid=4242)], pid=4242)


POOL_LANES = {"main": ("inline", 1), "heavy": ("pool", 2)}


def _executor(host, factory, **kwargs) -> LaneExecutor:
    return LaneExecutor(host.queue, host.engine, lanes=POOL_LANES, pool_factory=factory, **kwargs)


def _lane_of(command) -> str:
    return "heavy" if isinstance(command, PoolPing | PoolBoom) else "main"


@pytest.fixture
def two_lane(tmp_path):
    h = Host(tmp_path / "two", lane_of=_lane_of)
    yield h
    h.close()


# ── No topology: the single-lane host, unchanged ────────────────────────────


def test_with_no_lanes_the_executor_is_the_loop_hosts_write_today(host):
    """The default placement is one inline lane. Same claim -> run -> ack, and
    the read model is the defined sequential result."""
    ids = [host.queue.put(InlineTick(key=k), origin="external") for k in ("a", "b", "c")]
    with LaneExecutor(host.queue, host.engine) as executor:
        assert executor.pool_lanes == ()
        assert executor.run_until_idle(DEADLINE)
    assert host.folded == ["a", "b", "c"]
    assert [host.status_of(i) for i in ids] == ["done"] * 3
    assert len(host.store) == 3


def test_without_a_pool_lane_nothing_is_ever_spawned(host, monkeypatch):
    """The seed's hard requirement: a topology-free run must not pay for a pool
    it will never use. Not 'a cheap pool' — no pool, and no lane thread."""
    import dizzy.engine.st.executor as mod

    def refuse(lane, workers):
        raise AssertionError("a topology-free executor built a process pool")

    monkeypatch.setattr(mod, "spawn_pool", refuse)
    before = threading.active_count()
    host.queue.put(InlineTick(key="a"), origin="external")
    with LaneExecutor(host.queue, host.engine) as executor:
        assert threading.active_count() == before
        assert executor.run_until_idle(DEADLINE)
    assert host.folded == ["a"]


def test_pump_reports_whether_it_did_anything(host):
    """The contract pump keeps: one unit of work, and whether it did any."""
    executor = LaneExecutor(host.queue, host.engine)
    assert executor.pump() is False
    host.queue.put(InlineTick(key="a"), origin="external")
    assert executor.pump() is True
    assert executor.pump() is False


def test_one_engine_takes_one_executor(host):
    """take_applied is engine-wide and destructive: two executors over one
    engine would eat each other's positions. Refused at construction rather
    than discovered by a double dispatch."""
    LaneExecutor(host.queue, host.engine)
    with pytest.raises(RuntimeError, match="already has a LaneExecutor"):
        LaneExecutor(host.queue, host.engine)


# ── The pool lane, driven by the immediate double ───────────────────────────


def test_a_pool_lanes_events_are_ingested_by_the_main_lane(two_lane):
    """The child returns events; the MAIN lane appends them. One writer."""
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(two_lane, ImmediateExecutor) as executor:
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.folded == ["p"]
    assert two_lane.status_of(job) == "done"
    assert [e.type for e in two_lane.store.iterate()] == ["pool_pinged"]
    # The event carries the CHILD's pid, unchanged by the trip home.
    assert [e.payload["pid"] for e in two_lane.store.iterate()] == [4242]


def test_the_row_stays_running_until_the_main_lane_has_ingested(two_lane):
    """The ack rule, which is the whole crash story: a claimed row is not
    acked by the lane that claimed it, and stays claimable-again on a crash
    until the writer has actually written."""
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    pools: list[DeferredExecutor] = []

    def factory(lane, workers):
        pools.append(DeferredExecutor(lane, workers))
        return pools[-1]

    executor = _executor(two_lane, factory)
    with executor:
        pools_pool = _await(lambda: pools[0] if pools else None)
        pools_pool.await_job("p")
        # Dispatched, running in the 'child', not acked and nothing appended.
        assert two_lane.status_of(job) == "running"
        assert len(two_lane.store) == 0
        pools_pool.finish("p")
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.status_of(job) == "done"
    assert len(two_lane.store) == 1


def test_a_pool_lane_does_not_hold_the_queue_while_it_runs(two_lane):
    """HEAD-OF-LINE, the property the whole seed exists for: a long procedure
    is dispatched to the pool and the inline lane keeps claiming and running.
    Asserted on the state of the queue, not on elapsed time."""
    slow = two_lane.queue.put(PoolPing(key="slow"), origin="external")
    two_lane.queue.put(InlineTick(key="quick"), origin="external")
    pools: list[DeferredExecutor] = []

    def factory(lane, workers):
        pools.append(DeferredExecutor(lane, workers))
        return pools[-1]

    executor = _executor(two_lane, factory)
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        pool.await_job("slow")
        # The slow job is in flight. Pump the main lane: it must make progress.
        assert _await(executor.pump)
        assert two_lane.folded == ["quick"]
        assert two_lane.status_of(slow) == "running"
        pool.finish("slow")
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.folded == ["quick", "slow"]


def test_pool_results_fold_in_completion_order_by_default(two_lane):
    """mp's guarantee: convergent, not sequentially consistent. Whichever
    finishes first is ingested first, whatever order it was claimed in."""
    for key in ("a", "b"):
        two_lane.queue.put(PoolPing(key=key), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        pool.await_job("a")
        pool.await_job("b")
        pool.finish("b")
        pool.finish("a")
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.folded == ["b", "a"]


def test_the_reorder_buffer_restores_this_lanes_dispatch_order(two_lane):
    """``ordered=True`` holds a result until every earlier one on the SAME lane
    has been handed over. It restores that lane's dispatch order and nothing
    more — not the log's, which races the main lane by construction."""
    for key in ("a", "b"):
        two_lane.queue.put(PoolPing(key=key), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w), ordered=True)
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        pool.await_job("a")
        pool.await_job("b")
        pool.finish("b")
        # 'b' completed first and is HELD: nothing may be ingested yet.
        for _ in range(3):
            executor.pump()
        assert two_lane.folded == []
        pool.finish("a")
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.folded == ["a", "b"]


def test_in_flight_rows_are_bounded_by_the_lanes_width(two_lane):
    """``backlog=0`` means the queue-visible ``running`` set equals the
    actually-running set: every claimed-but-un-ingested row replays on a crash,
    so this is a correctness dial, not a throughput one."""
    for key in ("a", "b", "c", "d"):
        two_lane.queue.put(PoolPing(key=key), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        pool.await_job("a")
        pool.await_job("b")
        # workers=2, backlog=0 — a third row cannot be claimed until one lands.
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            assert len(pool.dispatched()) <= 2
        assert two_lane.queue.counts()["running"] == 2
        # Land the first two; the lane may then, and only then, claim the rest.
        pool.finish("a")
        pool.finish("b")
        pool.finish("c")
        pool.finish("d")
        assert executor.run_until_idle(DEADLINE)
    assert sorted(two_lane.folded) == ["a", "b", "c", "d"]


def test_extra_backlog_claims_further_ahead(two_lane):
    """The dial moves, and what it buys is stated where it is set."""
    for key in ("a", "b", "c"):
        two_lane.queue.put(PoolPing(key=key), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w), backlog=1)
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        for key in ("a", "b", "c"):
            pool.await_job(key)
        assert two_lane.queue.counts()["running"] == 3
        for key in ("a", "b", "c"):
            pool.finish(key)
        assert executor.run_until_idle(DEADLINE)
    assert sorted(two_lane.folded) == ["a", "b", "c"]


# ── Failure paths ───────────────────────────────────────────────────────────


def test_a_procedure_that_raised_errors_the_row_and_appends_nothing(two_lane):
    """Deterministic failure: the child caught it, discarded its stranded
    emits, and returned a string. No automatic replay — the operator retries."""

    def failing(command):
        return ProcedureResult(error="ValueError: boom", traceback="...", pid=4242)

    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(two_lane, lambda lane, w: ImmediateExecutor(lane, w, failing)) as ex:
        assert ex.run_until_idle(DEADLINE)
    assert two_lane.status_of(job) == "error"
    assert len(two_lane.store) == 0
    assert two_lane.folded == []


def test_a_failed_ingest_errors_the_unit_and_drops_the_tail(tmp_path):
    """``ingest`` is at-most-once per list: an element that raises part-way
    leaves the prefix in the log, so the unit fails rather than being ingested
    again — and the un-appended tail is discarded, not inherited."""
    host = Host(tmp_path / "bad", lane_of=_lane_of, poison={"p"})
    try:
        job = host.queue.put(PoolPing(key="p"), origin="external")
        executor = _executor(host, ImmediateExecutor)
        with executor:
            assert executor.run_until_idle(DEADLINE)
        assert host.status_of(job) == "error"
        assert host.folded == []
        # The event WAS appended — append comes first, by design — and the
        # engine is left clean for the next unit.
        assert len(host.store) == 1
        assert not host.engine._events
        # The engine is clean for the next unit — nothing was inherited.
        host.queue.put(InlineTick(key="ok"), origin="external")
        with executor:
            assert executor.run_until_idle(DEADLINE)
        assert host.folded == ["ok"]
    finally:
        host.close()


def test_a_failed_unit_rolls_the_read_model_session_back(two_lane):
    class Session:
        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    session = Session()

    def failing(command):
        return ProcedureResult(error="ValueError: boom", pid=4242)

    two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(
        two_lane, lambda lane, w: ImmediateExecutor(lane, w, failing), session=session
    ) as ex:
        assert ex.run_until_idle(DEADLINE)
    assert session.rollbacks == 1


def test_a_dead_worker_is_replayed_until_the_rows_attempt_budget_is_spent(two_lane):
    """A child killed by the OS is infrastructure, not a decision: nothing was
    appended, so the row is replayed. The budget is the ROW's own ``attempts``
    column — durable, so a restart cannot reset it — and a poison pill is
    bounded rather than looped on."""
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    with executor:
        for attempt in range(POOL_MAX_ATTEMPTS):
            pool = _await(lambda n=attempt: pools[n] if len(pools) > n else None)
            pool.break_pool("p")
            expected = "queued" if attempt + 1 < POOL_MAX_ATTEMPTS else "error"
            _await(lambda e=expected: two_lane.status_of(job) == e or None, poll=executor.pump)
    assert two_lane.status_of(job) == "error"
    assert len(pools) == POOL_MAX_ATTEMPTS  # a fresh pool for each replay
    row = next(j for j in two_lane.queue.jobs() if j["id"] == job)
    assert row["attempts"] == POOL_MAX_ATTEMPTS
    assert len(two_lane.store) == 0


def test_a_bystander_of_someone_elses_dead_child_still_gets_to_run(two_lane):
    """``BrokenProcessPool`` poisons every in-flight future on the lane, not
    just the victim's — and the parent cannot tell them apart. A row that was
    merely a bystander must therefore not be errored for good on somebody
    else's crash: it never ran, so 'repeated and absorbed' would have become
    'lost'. It is replayed, and the replay lands."""
    victim = two_lane.queue.put(PoolPing(key="victim"), origin="external")
    bystander = two_lane.queue.put(PoolPing(key="bystander"), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        pool.await_job("victim")
        pool.await_job("bystander")
        # One child dies; the pool breaks BOTH futures, which is what a real
        # ProcessPoolExecutor does and what a hand-broken future must copy.
        pool.break_pool("victim")
        pool.break_pool("bystander")
        _await(lambda: two_lane.status_of(bystander) == "queued" or None, poll=executor.pump)
        replay = _await(lambda: pools[1] if len(pools) > 1 else None, poll=executor.pump)
        # The second attempt is dispatched to the rebuilt pool and succeeds.
        for key in ("victim", "bystander"):
            replay.finish(key)
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.status_of(bystander) == "done"
    assert two_lane.status_of(victim) == "done"
    assert sorted(two_lane.folded) == ["bystander", "victim"]


def test_a_broken_pools_shutdown_never_runs_on_the_callback_thread(two_lane):
    """The deadlock a hand-broken Future cannot show, asserted in milliseconds.

    ``ProcessPoolExecutor`` sets ``BrokenProcessPool`` on every pending future
    while HOLDING its own ``_shutdown_lock``, and ``shutdown()`` takes that
    same lock — so discarding the pool inline, from the done-callback, wedges
    the pool's manager thread forever: the callback never returns, the job is
    never delivered, and the lane is dead for the life of the process. Here the
    double's ``shutdown`` blocks until the test releases it, and the job must
    still come home while it is blocked.
    """

    entered, let_go = threading.Event(), threading.Event()

    class BlockingShutdown(DeferredExecutor):
        """One shared release, so a pool built by the retry after the test has
        released cannot block ``stop()`` on an Event nobody will ever set."""

        def shutdown(self, wait=True, cancel_futures=False):
            entered.set()
            let_go.wait(DEADLINE)
            super().shutdown(wait, cancel_futures)

    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    pools: list[BlockingShutdown] = []
    executor = _executor(two_lane, lambda lane, w: _remember_cls(pools, BlockingShutdown, lane, w))
    executor.start()
    try:
        pool = _await(lambda: pools[0] if pools else None)
        started = time.perf_counter()
        pool.break_pool("p")  # resolving the future runs the callback HERE
        blocked_for = time.perf_counter() - started
        assert entered.wait(DEADLINE), "the discarded pool was never shut down"
        # The callback handed the shutdown off instead of waiting on it. In a
        # real pool that wait is on a lock the caller already holds, so this
        # bound is the difference between a rebuilt lane and a wedged one.
        assert blocked_for < 1.0, f"the completion callback blocked {blocked_for:.2f}s on shutdown"
        # Shutdown is still blocked, and the completion arrives anyway.
        _await(lambda: two_lane.status_of(job) == "queued" or None, poll=executor.pump)
    finally:
        let_go.set()  # released BEFORE stop(), which waits on the live pool
        executor.stop()


def test_a_job_that_cannot_be_dispatched_leaves_running(two_lane):
    """An unpicklable command, or a pool that will not start. The row must
    leave ``running`` with a message naming the lane rather than hanging."""

    def refuse(lane, workers):
        raise RuntimeError("no pool for you")

    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    executor = _executor(two_lane, refuse)
    with executor:
        assert executor.run_until_idle(DEADLINE)
    row = next(j for j in two_lane.queue.jobs() if j["id"] == job)
    assert row["status"] == "error"
    assert "heavy" in row["error"]


def test_a_callback_that_blows_up_still_reports_the_job(two_lane, monkeypatch):
    """``concurrent.futures`` swallows a done-callback's exceptions — a silent
    callback is how this design would lose a job forever."""
    import dizzy.engine.st.executor as mod

    def explode(self, *a, **k):
        raise RuntimeError("callback is broken")

    monkeypatch.setattr(mod.LaneExecutor, "_completion_for", explode)
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(two_lane, ImmediateExecutor) as ex:
        assert ex.run_until_idle(DEADLINE)
    row = next(j for j in two_lane.queue.jobs() if j["id"] == job)
    assert row["status"] == "error"
    assert "callback" in row["error"]


# ── The unit boundary ───────────────────────────────────────────────────────


def test_every_unit_hands_over_its_applied_positions(two_lane):
    """``run_command`` clears the positions on entry, so a completion's
    positions left undrained are cleared and LOST — and a hybrid group worker
    then re-runs policies that already dispatched."""
    units: list[tuple[int, ...]] = []
    two_lane.queue.put(PoolPing(key="p"), origin="external")
    two_lane.queue.put(InlineTick(key="i"), origin="external")
    with _executor(two_lane, ImmediateExecutor, on_unit_done=units.append) as ex:
        assert ex.run_until_idle(DEADLINE)
    assert len(units) == 2
    assert all(len(u) == 1 for u in units)
    assert two_lane.engine.take_applied() == ()


def test_owns_units_false_leaves_the_positions_for_the_group_worker(two_lane):
    """Driven by a caller that marks its own unit boundaries: positions
    accumulate across one pass and the worker drains one merged skip-list.
    Merging positions is safe; losing them is not."""
    two_lane.queue.put(PoolPing(key="p"), origin="external")
    two_lane.queue.put(InlineTick(key="i"), origin="external")
    executor = _executor(two_lane, ImmediateExecutor, owns_units=False)
    with executor:
        assert executor.run_until_idle(DEADLINE)
    assert len(two_lane.engine.take_applied()) == 2


def test_the_bus_says_which_lane_a_procedure_ran_on(two_lane):
    """The child's engine has no bus, so the lane stamp is lost over there.
    The parent emits it from the result instead — telemetry still says where."""
    records: list[dict] = []
    two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(two_lane, ImmediateExecutor, bus=records.append) as ex:
        assert ex.run_until_idle(DEADLINE)
    assert [(r["lane"], r["pid"], r["outcome"]) for r in records] == [("heavy", 4242, "ok")]


# ── The child half, in this process ─────────────────────────────────────────


def test_a_pool_child_may_not_dispatch():
    """A policy runs on the main lane, after ingest, in the writer's process."""
    with pytest.raises(RuntimeError, match="may not dispatch"):
        _RefusingQueue().put(object())
    assert _RefusingQueue().qsize() == 0


def test_a_bad_host_app_spec_comes_back_as_a_result_not_a_broken_pool():
    """Built lazily inside the task rather than in ``initializer=``: an
    exception in an initializer breaks the whole pool, while this names the
    spec an operator has to fix."""
    result = run_procedure_job("no.such.module:app", [], "heavy", PoolPing(key="p"))
    assert result.events == []
    assert result.error is not None and "no.such.module:app" in result.error
    assert result.pid == os.getpid()


# ── The fence: an ack may only land on the attempt that earned it ───────────


def test_a_superseded_attempt_cannot_ack_the_live_row(two_lane):
    """The crash window a pool lane makes wide.

    A 40-second procedure is in flight; the host restarts. The new process's
    ``DurableCommandQueue`` recovers that row to ``queued`` and it is claimed
    again. If the OLD attempt's completion could still ack, it would erase the
    replay that is the only thing left standing between this command and never
    running at all — the queue would say ``error``, and nothing, anywhere, ever
    ran it. The ack is fenced on the attempt, so the stale one is dropped.
    """
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    with executor:
        pool = _await(lambda: pools[0] if pools else None)
        stale = pool.await_job("p")  # attempt 1's future, held for later
        assert two_lane.status_of(job) == "running"
        # The 'restart': recovery re-queues the in-flight row, and the lane
        # claims it again — a second attempt, live, while the first is stale.
        assert two_lane.queue._recover() == 1
        _await(lambda: pool.await_job("p") is not stale or None)
        assert next(j for j in two_lane.queue.jobs() if j["id"] == job)["attempts"] == 2
        # Now the STALE attempt comes home and fails.
        stale.set_exception(ValueError("transient"))
        for _ in range(5):
            executor.pump()
        # The live attempt still owns the row: it was not errored out from
        # under itself, and the command still runs.
        assert two_lane.status_of(job) == "running"
        pools[-1].finish("p")
        assert executor.run_until_idle(DEADLINE)
    assert two_lane.status_of(job) == "done"
    assert two_lane.folded == ["p"]


def test_an_unfenced_queue_still_works(host):
    """The fence is optional: a queue with no ``claim_fenced`` acks the way it
    always did, which is what keeps a host's own queue object working."""

    class PlainQueue:
        def __init__(self, inner):
            self._inner = inner
            self.acked: list[int] = []

        def claim(self, timeout=None, lane="default"):
            return self._inner.claim(timeout=timeout, lane=lane)

        def mark_done(self, job_id):
            self.acked.append(job_id)
            self._inner.mark_done(job_id)

        def mark_error(self, job_id, error):
            self._inner.mark_error(job_id, error)

        def counts(self):
            return self._inner.counts()

    plain = PlainQueue(host.queue)
    job = host.queue.put(InlineTick(key="a"), origin="external")
    with LaneExecutor(plain, host.engine) as executor:
        assert executor.run_until_idle(DEADLINE)
    assert plain.acked == [job]
    assert host.folded == ["a"]


# ── What may enter the log ──────────────────────────────────────────────────


class Alien(BaseModel):
    """An event class the host's store does not declare — a child running a
    NEWER build of the feature than the parent, which is the shape this
    actually takes in a deploy."""

    key: str


def test_an_event_the_store_cannot_read_back_never_reaches_the_log(two_lane):
    """``EventStore.append`` never consults its class map, so an undeclared
    event lands happily and only fails later, at rebuild time, with the
    command's intent row long gone. An un-foldable stream is the one thing the
    store cannot recover from, so the completion is refused at the door."""

    def alien(command):
        return ProcedureResult(events=[Alien(key=command.key)], pid=4242)

    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    with _executor(two_lane, lambda lane, w: ImmediateExecutor(lane, w, alien)) as ex:
        assert ex.run_until_idle(DEADLINE)
    row = next(j for j in two_lane.queue.jobs() if j["id"] == job)
    assert row["status"] == "error"
    assert "Alien" in row["error"]
    assert len(two_lane.store) == 0  # nothing unreadable was written
    # And the store is still foldable: every envelope reconstructs.
    assert [two_lane.store.reconstruct(e) for e in two_lane.store.iterate()] == []


# ── stop(): what it drains, what it bounds, what it releases ────────────────


def test_stop_ingests_the_results_that_already_came_home(two_lane):
    """A graceful stop is not a crash: work the children already finished is
    on the inbox, this thread is the writer, and dropping it would leave the
    row ``running`` with nobody in this process left to recover it."""
    job = two_lane.queue.put(PoolPing(key="p"), origin="external")
    pools: list[DeferredExecutor] = []
    executor = _executor(two_lane, lambda lane, w: _remember(pools, lane, w))
    executor.start()
    pool = _await(lambda: pools[0] if pools else None)
    pool.finish("p")
    _await(lambda: not executor.idle())  # home, un-ingested: the window
    assert len(two_lane.store) == 0
    executor.stop()
    assert two_lane.folded == ["p"]
    assert two_lane.status_of(job) == "done"
    assert executor.idle()


def test_a_stopped_executor_gives_its_engine_back(host):
    """The invariant is one LIVE owner, not one owner ever: a host reloading
    its topology stops the executor and builds another over the same engine."""
    first = LaneExecutor(host.queue, host.engine)
    first.start()
    first.stop()
    second = LaneExecutor(host.queue, host.engine)  # accepted, not refused
    with pytest.raises(RuntimeError, match="already has a LaneExecutor"):
        LaneExecutor(host.queue, host.engine)
    second.stop()


# ── The boundary itself: the tests that really spawn ────────────────────────

SPEC = "_pool_app:host_app"
FALLBACK_SPEC = "_pool_app:fallback_app"


def test_the_pool_context_is_spawn_and_nothing_else():
    """Spawn is not a preference: a forked child inherits this process's sqlite
    connections and telemetry threads. The platform default is fork on Linux,
    so the whole suite passes with the context quietly changed — which is
    exactly why the context is asserted here directly and cheaply. Nothing is
    spawned: a ProcessPoolExecutor starts no worker until the first submit."""
    pool = spawn_pool("heavy", 2)
    try:
        # getattr, because `_mp_context` is a ProcessPoolExecutor detail and
        # the declared type is the abstract Executor — which is also the point:
        # the context is a fact about the object this factory returns.
        context = getattr(pool, "_mp_context", None)
        assert context is not None and context.get_start_method() == "spawn"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_a_lane_that_declares_two_workers_runs_two_processes(tmp_path):
    """``workers: N`` from LANES is a width, and nothing else asserted it: every
    other pool test uses an in-process double, and the real-spawn ones never
    counted pids. The two commands here BARRIER on each other in the children,
    so a lane clamped to one worker cannot finish at all — the width is proved
    by the run completing, and the pids then say it out loud."""
    host = Host(tmp_path / "width", lane_of=lambda c: "heavy")
    room = tmp_path / "barrier"
    try:
        for key in ("x", "y"):
            host.queue.put(
                PoolRendezvous(key=key, barrier_dir=str(room), expect=2, timeout=25.0),
                origin="external",
            )
        executor = LaneExecutor(
            host.queue,
            host.engine,
            lanes={"main": ("inline", 1), "heavy": ("pool", 2)},
            host_app_spec=SPEC,
        )
        with executor:
            assert executor.run_until_idle(60.0)
        pids = {e.payload["pid"] for e in host.store.iterate()}
        assert len(pids) == 2, f"a two-worker lane ran in {len(pids)} process(es)"
        assert os.getpid() not in pids
    finally:
        host.close()


def test_stop_returns_inside_its_timeout_with_a_child_still_running(tmp_path):
    """``shutdown(wait=True)`` is unbounded and ``cancel_futures`` cannot reach
    a task already running, so stopping a lane mid-procedure used to wait for
    the whole procedure — with the forty-second one a pool lane exists for,
    stop() simply hung. The child here sleeps for thirty seconds; stop() is
    given a fraction of a second and must take the worker away."""
    host = Host(tmp_path / "stopping", lane_of=lambda c: "heavy")
    marker = tmp_path / "inside.marker"
    try:
        host.queue.put(PoolSleep(key="slow", marker=str(marker), seconds=30.0), origin="external")
        executor = LaneExecutor(
            host.queue,
            host.engine,
            lanes={"main": ("inline", 1), "heavy": ("pool", 1)},
            host_app_spec=SPEC,
        )
        executor.start()
        _await(lambda: marker.exists(), deadline=60.0)  # the child is INSIDE it
        child = int(marker.read_text())
        started = time.perf_counter()
        executor.stop(timeout=0.5)
        elapsed = time.perf_counter() - started
        assert elapsed < 10.0, f"stop() blocked {elapsed:.1f}s on a 30s procedure"
        # And the child is gone, not leaked to outlive the suite.
        gone = _await(lambda: not _alive(child) or None, deadline=10.0)
        assert gone
    finally:
        host.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not ours, but it exists
        return True
    return True


def test_a_spawned_child_does_not_recover_the_hosts_queue(tmp_path):
    """The duplicate-fact bug at the spawn boundary.

    A spawned child re-imports the host's ``__main__`` as ``__mp_main__``, and
    a host that builds its queue at module scope — the ordinary shape — thereby
    ran crash recovery INSIDE the child, re-queueing the very row that child
    was spawned to run. The parent then ran it a second time and the log held
    the fact twice: 'the truth', with a duplicate in it. Recovery is a host
    action, and a child must open the file and change nothing.
    """
    path = tmp_path / "host-queue.db"
    queue = DurableCommandQueue(registry=ALL_COMMANDS, path=path)
    try:
        job = queue.put(PoolPing(key="in-flight"), origin="external")
        assert queue.claim(timeout=1) is not None  # the row is 'running'
        ctx = multiprocessing.get_context("spawn")
        sink = ctx.Queue()
        child = ctx.Process(target=report_queue_recovery, args=(str(path), sink))
        child.start()
        try:
            recovered, rows = sink.get(timeout=60.0)
        finally:
            child.join(timeout=10.0)
            if child.is_alive():  # pragma: no cover - a child that will not exit
                child.kill()
        assert recovered == 0, "a spawned child re-queued the host's in-flight row"
        assert rows == [(job, "running")]
        assert queue.jobs()[0]["status"] == "running"
    finally:
        queue.close()


@pytest.mark.parametrize("spec", [SPEC, FALLBACK_SPEC])
def test_a_spawned_child_runs_the_procedure_and_the_events_come_home(tmp_path, spec):
    """The whole boundary in one test: spawn (never fork), resolve the HostApp
    from the spec the parent passed, run the procedure in ANOTHER process, and
    pickle the generated event instances back. Both host shapes — one that
    declares ``build_procedure_runtime`` and one that predates it."""
    host = Host(tmp_path / f"spawn-{spec[-4:]}", lane_of=_lane_of)
    try:
        jobs = [host.queue.put(PoolPing(key=k), origin="external") for k in ("a", "b")]
        executor = LaneExecutor(host.queue, host.engine, lanes=POOL_LANES, host_app_spec=spec)
        with executor:
            assert executor.run_until_idle(30.0)
        assert sorted(host.folded) == ["a", "b"]
        assert [host.status_of(j) for j in jobs] == ["done", "done"]
        pids = {e.payload["pid"] for e in host.store.iterate()}
        assert pids and os.getpid() not in pids  # real process isolation
    finally:
        host.close()


# ── What the child may do to the host's log ─────────────────────────────────


STORE_FALLBACK_SPEC = "_pool_app:store_fallback_app"


def test_the_pool_child_marker_is_never_set_in_the_host(tmp_path):
    """The marker means "this process is a spawned pool child", and a host that
    called the child entry point in-process — which every ``pool_factory``
    double here does — must not be able to mark itself. A read-only store in
    the writer's own process would take the log away from the one thing
    allowed to write it, so the guard is on ``parent_process()`` and the
    variable alone is not enough."""
    mark_pool_child()  # in the host: a no-op
    assert not in_pool_child()
    os.environ["DIZZY_POOL_CHILD"] = "1"  # and forging it changes nothing
    try:
        assert not in_pool_child()
        assert not EventStore(path=tmp_path / "host.db", read_only=None).dag.read_only
    finally:
        os.environ.pop("DIZZY_POOL_CHILD", None)


def test_a_read_only_store_reads_the_log_and_cannot_touch_it(tmp_path):
    """What the child's store becomes: every read the procedures might make,
    and no way to write — not even the schema DDL, which is a write
    transaction on the host's file taken merely by CONSTRUCTING a store."""
    path = tmp_path / "log.db"
    writer = EventStore(path=path, event_classes=EVENT_CLASSES)
    writer.append(PoolPinged(key="a", pid=1))

    reader = EventStore(path=path, event_classes=EVENT_CLASSES, read_only=True)
    assert reader.dag.read_only
    assert [e.payload["key"] for e in reader.iterate()] == ["a"]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.append(PoolPinged(key="b", pid=2))
    # And the writer is unharmed: one fact, and it can still add another.
    writer.append(PoolPinged(key="c", pid=3))
    assert [e.payload["key"] for e in writer.iterate()] == ["a", "c"]


def test_a_fallback_child_opens_the_hosts_log_read_only(tmp_path, monkeypatch):
    """The residue of the fallback, pinned.

    A host with no ``build_procedure_runtime`` — every generated wiring today —
    hands the child its FULL runtime, and that store's path is inherited across
    the spawn: the child really does open the parent's log. What it must not be
    able to do is write it, or run the DAG schema's DDL against it on the way
    in. The child reports what it found from inside the procedure, because the
    process that found it is gone by the time the parent reads the answer."""
    host = Host(tmp_path / "ro", lane_of=lambda c: "heavy")
    marker = tmp_path / "probe.txt"
    monkeypatch.setenv("DIZZY_STORE_PATH", str(tmp_path / "ro" / "events.db"))
    try:
        job = host.queue.put(PoolStoreProbe(key="probe", marker=str(marker)), origin="external")
        executor = LaneExecutor(
            host.queue,
            host.engine,
            lanes=POOL_LANES,
            host_app_spec=STORE_FALLBACK_SPEC,
        )
        with executor:
            assert executor.run_until_idle(30.0)
        assert host.status_of(job) == "done", [j["error"] for j in host.queue.jobs()]
        answer = marker.read_text()
        assert answer == "read_only=True REFUSED OperationalError", answer
        # One fact in the log, appended by the one writer: the child's attempt
        # left nothing behind. And it really was another process.
        facts = list(host.store.iterate())
        assert [e.payload["key"] for e in facts] == ["probe"]
        assert facts[0].payload["pid"] != os.getpid()
    finally:
        host.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("_pool_app:generated_app", "store=no open=[]"),
        ("_pool_app:pre_generated_app", "store=yes open=['events.db']"),
    ],
    ids=["emitted-procedure-runtime", "the-shape-before-it"],
)
def test_a_generated_child_never_opens_the_hosts_log_at_all(tmp_path, monkeypatch, spec, expected):
    """The cure, and the calibration that makes it mean something.

    ``generated_app`` is the shape the wiring generator now emits: both
    builders, ``build_procedure_runtime`` populated, and its engine's store is
    None. ``pre_generated_app`` is the same module with that field left off —
    the shape every generated wiring had before — and the child then builds the
    full runtime whose store path it inherited.

    Both run the SAME procedure, which reports its own ``/proc/self/fd``. The
    second case is what turns the first from "the probe found nothing" into
    "there is nothing to find": read-only was the mitigation, an empty fd table
    is the cure."""
    host = Host(tmp_path / "opened", lane_of=lambda c: "heavy")
    marker = tmp_path / "open.txt"
    monkeypatch.setenv("DIZZY_STORE_PATH", str(tmp_path / "opened" / "events.db"))
    try:
        job = host.queue.put(PoolOpenProbe(key="probe", marker=str(marker)), origin="external")
        executor = LaneExecutor(host.queue, host.engine, lanes=POOL_LANES, host_app_spec=spec)
        with executor:
            assert executor.run_until_idle(30.0)
        assert host.status_of(job) == "done", [j["error"] for j in host.queue.jobs()]
        assert marker.read_text() == expected
        # Either way the fact came home through the one writer, from elsewhere.
        facts = list(host.store.iterate())
        assert [e.payload["key"] for e in facts] == ["probe"]
        assert facts[0].payload["pid"] != os.getpid()
    finally:
        host.close()


def test_a_procedure_that_raises_in_a_child_strands_nothing(tmp_path):
    """Two properties at once: the failure comes back as a STRING (arbitrary
    app exceptions are frequently unpicklable), and the emits of the procedure
    that raised are discarded in the child — so the NEXT job on that worker
    does not inherit them."""
    host = Host(tmp_path / "spawn-boom", lane_of=_lane_of)
    try:
        bad = host.queue.put(PoolBoom(key="boom"), origin="external")
        executor = LaneExecutor(
            host.queue,
            host.engine,
            lanes={"main": ("inline", 1), "heavy": ("pool", 1)},
            host_app_spec=SPEC,
        )
        with executor:
            assert executor.run_until_idle(30.0)
            assert host.status_of(bad) == "error"
            good = host.queue.put(PoolPing(key="after"), origin="external")
            assert executor.run_until_idle(30.0)
        assert host.status_of(good) == "done"
        # Exactly one event: 'boom' emitted before raising and was discarded.
        assert [e.payload["key"] for e in host.store.iterate()] == ["after"]
    finally:
        host.close()


# ── Waiting, always with a deadline ─────────────────────────────────────────


def _remember(pools: list, lane: str, workers: int) -> DeferredExecutor:
    pools.append(DeferredExecutor(lane, workers))
    return pools[-1]


def _remember_cls(pools: list, cls, lane: str, workers: int):
    pools.append(cls(lane, workers))
    return pools[-1]


def _await(probe, deadline: float = DEADLINE, poll=None):
    """Poll *probe* until it returns something truthy, or fail on the deadline.

    A deadline loop, never a sleep-and-hope: a broken build fails in seconds.
    """
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if poll is not None:
            poll()
        value = probe()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("condition was never reached")


def test_a_module_scope_host_queue_is_not_recovered_by_the_spawn_reimport(tmp_path):
    """The duplicate-fact bug, at the phase the in-child helper cannot reach.

    ``report_queue_recovery`` builds its queue inside the child's TASK, which
    runs after ``_bootstrap`` has set ``_parent_process`` — the one case that
    always worked. The ordinary host builds at module scope, and a spawned
    child re-imports that module BEFORE ``_bootstrap``, where
    ``parent_process()`` is still None and the child reads as a host. This runs
    a real ``__main__`` in that shape and asserts the child's re-import
    recovered nothing.
    """
    db = tmp_path / "host-queue.db"
    out = tmp_path / "probe.txt"

    script = Path(__file__).with_name("_module_scope_host.py")
    env = {
        **os.environ,
        "DIZZY_PROBE_DB": str(db),
        "DIZZY_PROBE_OUT": str(out),
        "PYTHONPATH": str(script.parent),
    }
    done = subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr

    reports = set(out.read_text(encoding="utf-8").splitlines())
    assert "__main__ recovered=0 pool_child=False" in reports, (
        f"the host opened a fresh queue and had nothing to recover; got {sorted(reports)}"
    )
    assert "__mp_main__ recovered=0 pool_child=True" in reports, (
        "a spawned child's re-import re-queued the host's in-flight row, or opened "
        f"the log read-write; got {sorted(reports)}"
    )

    # The row the host left running must still be running: a child that
    # recovered it would have flipped it back to 'queued', and the host would
    # then have run the same command a second time. Read the file directly —
    # opening a DurableCommandQueue here would recover the row itself, this
    # test process being a host, and destroy the evidence.
    with sqlite3.connect(db) as probe:
        assert [row[0] for row in probe.execute("SELECT status FROM jobs")] == ["running"]
