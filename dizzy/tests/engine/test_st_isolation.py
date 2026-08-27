"""Latency and isolation: what a pool lane actually buys, measured.

The executor's correctness is pinned in ``test_st_executor.py``. This file
answers the question the topology work exists to answer — *does an urgent
command get served while something slow is in flight* — and it answers it with
blocking stubs and deadlines, never by reading a log or a duration off a
timing-sensitive sleep.

Two latency problems, deliberately kept apart because they have different
fixes and only one of them needs a lane:

- **QUEUE-POSITION** — the urgent command is behind 200 batch rows. Fixed by
  the claim order ``(priority DESC, id)``.
- **HEAD-OF-LINE** — a procedure is RUNNING, so the queue is not consulted at
  all until it returns. Priority cannot help; a second lane can.

**On the 2s block in the seed.** Nothing here sleeps for two seconds. A pool
procedure that blocks on an ``Event`` the test owns is blocked for as long as
the test wants and costs nothing — it is the stronger stub, not the weaker
one: the block is *unbounded*, so a main lane that waited on it would not
finish late, it would never finish, and the assertion fails on a deadline
rather than on a threshold. The elapsed-time bound (<100ms) is then asserted
over the main lane's own work while that block is held.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest
from _pool_app import COMMAND_CLASSES, EVENT_CLASSES, PoolCrashOnce, PoolPinged
from dizzy.engine.loop import Engine
from dizzy.engine.st import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    DurableCommandQueue,
    LaneExecutor,
)
from dizzy.engine.st.executor import ProcedureResult
from dizzy.engine.store import EventStore
from pydantic import BaseModel

DEADLINE = 5.0
"""Every bounded wait in this file. A broken build fails in seconds."""

INTERACTIVE_BUDGET = 0.1
"""The seed's number: an interactive command dispatched while a pool procedure
is blocked completes in under 100ms on the main lane."""

REPO = Path(__file__).resolve().parents[3]


# ── The fixture app ─────────────────────────────────────────────────────────


class Tick(BaseModel):
    """The main lane's command. ``urgent`` decides its PRIORITY, not its lane."""

    key: str
    urgent: bool = False


class Work(BaseModel):
    """The pool lane's command: what a slow procedure looks like from here."""

    key: str


COMMANDS = {"Tick": Tick, "Work": Work, **COMMAND_CLASSES}


def _lane_of(command) -> str:
    return "heavy" if isinstance(command, Work | PoolCrashOnce) else "main"


def _priority_of(command) -> int:
    return PRIORITY_INTERACTIVE if getattr(command, "urgent", False) else PRIORITY_BATCH


LANES = {"main": ("inline", 1), "heavy": ("pool", 2)}


class Host:
    """One writer: its engine, its store, its queue, and what it folded.

    ``model`` COUNTS each key. Addition is commutative, so reordering cannot
    change it — which is the property under test — while a constant written by
    assignment could not have failed at all: it made ``model`` a function of
    the key set, and the key-set assertion next to it already said that. A
    count also notices a delivery a run did not make. ``folded`` keeps the fold
    ORDER, which is the thing that legitimately differs between runs.
    """

    def __init__(self, root, *, queue_path=None, store_path=None, plain=False):
        root.mkdir(parents=True, exist_ok=True)
        self.folded: list[str] = []
        self.model: dict[str, int] = {}
        self.store = EventStore(path=store_path or root / "events.db", event_classes=EVENT_CLASSES)
        # *plain* is the topology-free host: no lane_of, no priority_of, which
        # is exactly what a wiring with no topology.yaml hands its queue today.
        placement: dict[str, Any] = (
            {} if plain else {"lane_of": _lane_of, "priority_of": _priority_of}
        )
        self.queue = DurableCommandQueue(
            registry=COMMANDS, path=queue_path or root / "queue.db", **placement
        )
        self.engine = Engine(command_queue=self.queue, store=self.store)
        self.engine.register_projection(PoolPinged, self._fold, name="folded")
        self.engine.register_procedure(Tick, self._tick, name="tick")
        self.engine.register_procedure(Work, self._tick, name="work")

    def _fold(self, event: PoolPinged, ingested_at) -> None:
        self.folded.append(event.key)
        self.model[event.key] = self.model.get(event.key, 0) + 1  # commutative

    def _tick(self, command) -> None:
        self.engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    def close(self) -> None:
        self.queue.close()

    def keys(self) -> list[str]:
        return [e.payload["key"] for e in self.store.iterate()]

    def status_of(self, job_id: int) -> str:
        return next(j["status"] for j in self.queue.jobs() if j["id"] == job_id)


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path / "h")
    yield h
    h.close()


# ── The doubles ─────────────────────────────────────────────────────────────


class BlockingPool:
    """A 'child' that never returns until the test lets it.

    ``submit`` blocks the LANE thread as well, which is harsher than the real
    thing (a real child blocks in another process entirely). If the main lane
    can be served while this double is in flight, it can be served while a real
    forty-second procedure is.
    """

    def __init__(self, lane: str, workers: int):
        self.lane, self.workers = lane, workers
        self.entered = threading.Event()
        self.let_go = threading.Event()
        self.started: list[str] = []
        self._lock = threading.Lock()

    def submit(self, fn, spec, sys_path, lane, command):
        with self._lock:
            self.started.append(command.key)
        self.entered.set()
        future: Future = Future()
        future.set_running_or_notify_cancel()
        if not self.let_go.wait(DEADLINE):
            future.set_exception(AssertionError("the pool block was never released"))
        else:
            future.set_result(ProcedureResult(events=[PoolPinged(key=command.key, pid=4242)]))
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.let_go.set()


class HeldPool:
    """Holds every job until the test resolves it BY NAME — so completion
    ORDER is chosen by the test rather than raced for."""

    def __init__(self, lane: str, workers: int):
        self.lane, self.workers = lane, workers
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._arrived = threading.Event()

    def submit(self, fn, spec, sys_path, lane, command):
        future: Future = Future()
        future.set_running_or_notify_cancel()
        with self._lock:
            self._futures[command.key] = future
        self._arrived.set()
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def await_job(self, key: str) -> Future:
        end = time.monotonic() + DEADLINE
        while time.monotonic() < end:
            with self._lock:
                if key in self._futures:
                    return self._futures[key]
            self._arrived.wait(0.01)
            self._arrived.clear()
        raise AssertionError(f"job {key!r} was never dispatched")

    def finish(self, key: str) -> None:
        self.await_job(key).set_result(
            ProcedureResult(events=[PoolPinged(key=key, pid=4242)], pid=4242)
        )


def _collect(sink: list, cls):
    def factory(lane: str, workers: int):
        sink.append(cls(lane, workers))
        return sink[-1]

    return factory


def _await(probe, deadline: float = DEADLINE, poll=None):
    """Poll until *probe* is truthy or the deadline passes. Never a bare sleep:
    a broken build fails in seconds instead of hanging the suite."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if poll is not None:
            poll()
        value = probe()
        if value:
            return value
        time.sleep(0.002)
    raise AssertionError("condition was never reached")


# ── 1. HEAD-OF-LINE ─────────────────────────────────────────────────────────


def test_a_blocked_pool_procedure_does_not_delay_an_interactive_command(tmp_path):
    """The property the whole seed exists for.

    A procedure on the pool lane is blocked — indefinitely, which is every
    duration the seed's 2s stands for — and an interactive command dispatched
    DURING that window is claimed, run, folded and acked by the main lane in
    under 100ms. The slow row is still ``running`` when that assertion is
    made, so there is no chance the latency was bought by it finishing early.
    """
    host = Host(tmp_path / "hol")
    pools: list[BlockingPool] = []
    try:
        slow = host.queue.put(Work(key="slow"), origin="external")
        executor = LaneExecutor(
            host.queue, host.engine, lanes=LANES, pool_factory=_collect(pools, BlockingPool)
        )
        with executor:
            pool = _await(lambda: pools[0] if pools else None)
            assert pool.entered.wait(DEADLINE), "the pool lane never dispatched"

            started = time.perf_counter()
            host.queue.put(Tick(key="urgent", urgent=True), origin="external")
            assert executor.pump() is True
            elapsed = time.perf_counter() - started

            assert host.folded == ["urgent"]
            assert host.status_of(slow) == "running"  # still blocked, not finished
            assert elapsed < INTERACTIVE_BUDGET, f"interactive latency {elapsed * 1000:.1f}ms"

            pool.let_go.set()
            assert executor.run_until_idle(DEADLINE)
        assert host.folded == ["urgent", "slow"]
        assert host.status_of(slow) == "done"
    finally:
        host.close()


def test_an_inline_lane_alone_cannot_do_this(tmp_path):
    """The control. Same block, one lane: the main lane is INSIDE the slow
    procedure, so the queue is not consulted and the urgent command waits.
    Without this the test above proves only that nothing was broken."""
    root = tmp_path / "control"
    root.mkdir(parents=True, exist_ok=True)
    folded: list[str] = []
    store = EventStore(path=root / "e.db", event_classes=EVENT_CLASSES)
    command_queue = DurableCommandQueue(
        registry=COMMANDS, path=root / "q.db", priority_of=_priority_of
    )
    engine = Engine(command_queue=command_queue, store=store)
    engine.register_projection(PoolPinged, lambda e, at: folded.append(e.key), name="f")
    entered, let_go = threading.Event(), threading.Event()

    def slow(command) -> None:
        entered.set()
        assert let_go.wait(DEADLINE)
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    engine.register_procedure(Work, slow, name="slow")
    engine.register_procedure(Tick, lambda c: engine.emit_event(PoolPinged(key=c.key, pid=0)), "t")
    try:
        command_queue.put(Work(key="slow"), origin="external")
        with LaneExecutor(command_queue, engine, poll_interval=0.001) as executor:
            worker = threading.Thread(target=executor.run_forever, daemon=True)
            worker.start()
            assert entered.wait(DEADLINE)
            command_queue.put(Tick(key="urgent", urgent=True), origin="external")
            # The single lane is inside the procedure: nothing can be folded,
            # and this is asserted by holding the block, not by waiting it out.
            deadline = time.monotonic() + 0.05
            while time.monotonic() < deadline:
                assert folded == []
                # Yield: this is the one wait in this file that watches a
                # thread which needs the CPU to prove it is blocked. A bare
                # spin here pegs a core against the executor thread it is
                # observing; the assertion window is unchanged.
                time.sleep(0.002)
            let_go.set()
            _await(lambda: folded == ["slow", "urgent"], poll=None)
            executor.stop()
            worker.join(timeout=DEADLINE)
    finally:
        command_queue.close()


# ── 2. QUEUE-POSITION ───────────────────────────────────────────────────────


def test_two_hundred_batch_rows_do_not_stand_in_front_of_one_interactive(host):
    """Arriving two-hundred-and-first does not mean running two-hundred-and-
    first. Asserted on the executor's OWN next claim, not on the queue in
    isolation: the main lane's very first unit of work is the urgent command."""
    for i in range(200):
        host.queue.put(Tick(key=f"b{i}"), origin="external")
    host.queue.put(Tick(key="urgent", urgent=True), origin="external")

    with LaneExecutor(host.queue, host.engine, lanes={"main": ("inline", 1)}) as executor:
        started = time.perf_counter()
        assert executor.pump() is True
        elapsed = time.perf_counter() - started
    assert host.folded == ["urgent"]
    assert elapsed < INTERACTIVE_BUDGET, f"interactive latency {elapsed * 1000:.1f}ms"
    assert host.queue.counts()["queued"] == 200  # deferred, and still all there


# ── 3. BOTH TOGETHER ────────────────────────────────────────────────────────


def test_interactive_latency_survives_a_backlog_and_a_blocked_pool_together(tmp_path):
    """The two problems at once, which is the only configuration a deployment
    actually has: 200 batch rows queued on the main lane AND a pool procedure
    blocked mid-flight. The bound is the main lane's OWN work — the unit it is
    already running — and nothing else: the urgent command is the next thing
    the main lane claims, and at most one batch row runs between its arrival
    and its completion.
    """
    host = Host(tmp_path / "both")
    pools: list[BlockingPool] = []
    try:
        host.queue.put(Work(key="slow"), origin="external")
        for i in range(200):
            host.queue.put(Tick(key=f"b{i}"), origin="external")
        executor = LaneExecutor(
            host.queue, host.engine, lanes=LANES, pool_factory=_collect(pools, BlockingPool)
        )
        with executor:
            pool = _await(lambda: pools[0] if pools else None)
            assert pool.entered.wait(DEADLINE)
            # The main lane is already at work on the backlog.
            for _ in range(3):
                assert executor.pump() is True
            assert len(host.folded) == 3

            before = len(host.folded)
            started = time.perf_counter()
            host.queue.put(Tick(key="urgent", urgent=True), origin="external")
            assert executor.pump() is True
            elapsed = time.perf_counter() - started

            assert host.folded[-1] == "urgent"
            assert len(host.folded) - before == 1  # nothing batch ran in between
            assert elapsed < INTERACTIVE_BUDGET, f"interactive latency {elapsed * 1000:.1f}ms"
            assert host.queue.counts()["running"] == 1  # the pool row, still blocked
            pool.let_go.set()
    finally:
        host.close()


# ── 4. THE DEFAULT TOPOLOGY IS A NO-OP ──────────────────────────────────────


def _hand_written_loop(command_queue, engine) -> None:
    """The loop every st host writes today, verbatim: claim, run, ack."""
    while True:
        job = command_queue.claim(timeout=0)
        if job is None:
            return
        job_id, command = job
        engine.run_command(command)
        command_queue.mark_done(job_id)


def _time_run(root, n: int, drive) -> tuple[float, list[str]]:
    host = Host(root, plain=True)
    try:
        for i in range(n):
            host.queue.put(Tick(key=f"k{i}"), origin="external")
        started = time.perf_counter()
        drive(host)
        return time.perf_counter() - started, host.folded
    finally:
        host.close()


N_COMMANDS = 200
"""Enough that per-command cost dominates the fixed setup, small enough that
this test is tenths of a second. The claim is a RATIO, so N cancels out — but
the absolute slack below does not, and at N=60 that slack was ~80% of the whole
baseline, which made the effective bound nearer 3x than 2x. At N=200 the
per-command signal dominates it again."""


def test_the_default_topology_costs_what_the_hand_written_loop_costs(tmp_path):
    """'If adopting this slows a fast in-process suite, the design is wrong.'

    With no topology there is one inline lane, and the executor is then the
    same claim/run/ack loop with one function call around it. Best-of-three
    each way, because a single timing on a shared machine measures the machine.
    The bound is deliberately loose (2x plus 10ms of slack) — it is there to
    catch a spawned pool, a sleep, or a poll-per-command creeping into the
    default path, not to police a few microseconds. The slack is small against
    the baseline at this N, so 2x is what the bound actually is.
    """

    def by_hand(host):
        _hand_written_loop(host.queue, host.engine)

    def by_executor(host):
        with LaneExecutor(host.queue, host.engine) as executor:
            assert executor.pool_lanes == ()
            assert executor.run_until_idle(DEADLINE)

    threads_before = threading.active_count()
    hand, hand_folded = min(
        (_time_run(tmp_path / f"hand{i}", N_COMMANDS, by_hand) for i in range(3)),
        key=lambda pair: pair[0],
    )
    lane, lane_folded = min(
        (_time_run(tmp_path / f"lane{i}", N_COMMANDS, by_executor) for i in range(3)),
        key=lambda pair: pair[0],
    )
    assert lane_folded == hand_folded  # same work, same order, same result
    assert threading.active_count() == threads_before  # nothing was left running
    assert lane <= hand * 2 + 0.01, (
        f"the default lane cost {lane * 1000:.1f}ms against {hand * 1000:.1f}ms by hand"
    )


# ── 5. CONVERGENCE ──────────────────────────────────────────────────────────


def _run_two_pool_procedures(root, finish_order, ordered: bool) -> Host:
    host = Host(root)
    pools: list[HeldPool] = []
    executor = LaneExecutor(
        host.queue,
        host.engine,
        lanes=LANES,
        pool_factory=_collect(pools, HeldPool),
        ordered=ordered,
    )
    with executor:
        for key in ("a", "b"):
            host.queue.put(Work(key=key), origin="external")
        pool = _await(lambda: pools[0] if pools else None)
        for key in ("a", "b"):
            pool.await_job(key)
        for key in finish_order:
            pool.finish(key)
        assert executor.run_until_idle(DEADLINE)
    return host


def test_two_pool_procedures_converge_in_either_completion_order(tmp_path):
    """The guarantee a pool lane makes: CONVERGENT, not sequentially
    consistent. Two procedures dispatched together and completing in opposite
    orders fold in opposite orders — and the read model is identical, which is
    what confluence buys and why the weaker lane is safe to deploy."""
    first = _run_two_pool_procedures(tmp_path / "ab", ("a", "b"), ordered=False)
    second = _run_two_pool_procedures(tmp_path / "ba", ("b", "a"), ordered=False)
    try:
        assert first.folded == ["a", "b"]
        assert second.folded == ["b", "a"]  # the reordering is real
        # Not implied by the key-set assertion below: the counts would differ
        # if either run had folded anything twice or not at all.
        assert first.model == second.model == {"a": 1, "b": 1}
        assert first.keys() != second.keys()  # the STORES differ: no determinism claimed
        assert sorted(first.keys()) == sorted(second.keys())
    finally:
        first.close()
        second.close()


def test_with_the_reorder_buffer_either_completion_order_gives_the_same_store(tmp_path):
    """``ordered=True`` restores this lane's DISPATCH order, so the same two
    procedures completing in opposite orders now produce byte-identical
    streams as well as identical read models — determinism bought back, for
    the shape the buffer covers: one pool lane's own results."""
    first = _run_two_pool_procedures(tmp_path / "oab", ("a", "b"), ordered=True)
    second = _run_two_pool_procedures(tmp_path / "oba", ("b", "a"), ordered=True)
    try:
        assert first.model == second.model == {"a": 1, "b": 1}
        assert first.folded == second.folded == ["a", "b"]
        assert [(e.type, e.payload) for e in first.store.iterate()] == [
            (e.type, e.payload) for e in second.store.iterate()
        ]
    finally:
        first.close()
        second.close()


# ── 6. CRASH, IN BOTH WINDOWS ───────────────────────────────────────────────


def test_a_child_killed_mid_procedure_replays_and_leaves_one_effect(tmp_path):
    """WINDOW ONE, with a real kill: the child calls ``os._exit`` inside the
    procedure, so the OS takes the worker away with no result and no traceback.
    Nothing was appended — the log has one writer and it is alive — so the row
    is retried, the replay succeeds, and the store holds the effect ONCE.

    This is the only test here that spawns; it is also the only one that can
    prove the ``BrokenProcessPool`` path against a genuinely dead process
    rather than against a future broken by hand.
    """
    host = Host(tmp_path / "crash")
    marker = tmp_path / "crashed.marker"
    try:
        job = host.queue.put(PoolCrashOnce(key="c", marker=str(marker)), origin="external")
        executor = LaneExecutor(
            host.queue,
            host.engine,
            lanes={"main": ("inline", 1), "heavy": ("pool", 1)},
            host_app_spec="_pool_app:host_app",
        )
        with executor:
            assert executor.run_until_idle(60.0)
        assert marker.exists()  # the first attempt really did die in the child
        assert host.status_of(job) == "done"  # the retry really did land
        assert host.keys() == ["c"]  # exactly one effect, not two
        assert host.folded == ["c"]
    finally:
        host.close()


def test_a_parent_that_dies_between_the_result_and_the_ingest_replays(tmp_path):
    """WINDOW TWO: the events are home, the main lane has NOT ingested them,
    and the process dies. The row was never acked, so the queue's own recovery
    re-queues it on restart and the command runs again — and because nothing
    was appended in the lost window, the restarted process's store holds the
    effect once, exactly as an uninterrupted run does.
    """
    root = tmp_path / "parent"
    root.mkdir(parents=True)
    queue_path, store_path = root / "q.db", root / "e.db"

    crashed = Host(root / "a", queue_path=queue_path, store_path=store_path)
    pools: list[HeldPool] = []
    executor = LaneExecutor(
        crashed.queue, crashed.engine, lanes=LANES, pool_factory=_collect(pools, HeldPool)
    )
    executor.start()
    crashed.queue.put(Work(key="w"), origin="external")
    pool = _await(lambda: pools[0] if pools else None)
    pool.finish("w")
    # The result is home and waiting on the inbox. The main lane never pumps:
    # this is the window, and it is entered by not acting, which is exactly
    # what a process that dies here does.
    _await(lambda: not executor.idle())
    assert len(crashed.store) == 0
    # ``drain=False`` because a process that dies ingests nothing. A graceful
    # stop() DOES drain this inbox — that is tested next door — and using one
    # here would be simulating a clean shutdown, not a crash.
    executor.stop(drain=False)
    crashed.close()  # the 'crash': no ack, no ingest, nothing appended

    restarted = Host(root / "b", queue_path=queue_path, store_path=store_path)
    replays: list[HeldPool] = []
    try:
        assert restarted.queue.recovered == 1  # the un-acked row came back
        with LaneExecutor(
            restarted.queue,
            restarted.engine,
            lanes=LANES,
            pool_factory=_collect(replays, HeldPool),
        ) as executor:
            pool = _await(lambda: replays[0] if replays else None)
            pool.finish("w")  # the replay runs the procedure a second time
            assert executor.run_until_idle(DEADLINE)
        assert restarted.folded == ["w"]
        assert restarted.keys() == ["w"]  # one effect in the store, not two
    finally:
        restarted.close()


# ── 7. NO ELEMENT KNOWS ITS TOPOLOGY ────────────────────────────────────────

TOPOLOGY_WORDS = re.compile(r"\b(lane|lanes|pool|pools|priority|topology|executor)\b", re.I)
"""The vocabulary itself — used on the SCHEMAS, where any occurrence is a leak."""

TOPOLOGY_LEAK = re.compile(
    r"""(?:
          \b(?:lane|lanes|pool|pools|executor|topology)\s*[:=]     # a parameter or a field
        | [.\[]\s*['"]?(?:lane|pool|priority|executor|topology)\b # an attribute or a key
        | \b(?:import|from)\b[^\n]*\btopology\b                 # reaching for the module
        | \b(?:priority|lane|pool)\s*[:=]\s*['"]                  # a placement literal
      )""",
    re.I | re.X,
)
"""What a topology LEAKING into an element actually looks like.

Deliberately narrower than the vocabulary above: an element or a generator is
app code that may legitimately carry a domain field called ``priority`` or an
entity called ``pool``, and failing this engine test on the noun would be a red
build in a file the engine does not own, for a reason that is not this claim.
What is refused is the shapes that could only be placement — a ``lane=``
argument, a ``.pool`` attribute, an import of the topology module."""

ELEMENT_SOURCES = sorted(
    path
    for kind in ("procedure", "projection", "policy", "query")
    for path in REPO.glob(f"examples/*/lib/*/{kind}/*/src/*.py")
)

ELEMENT_GENERATORS = sorted(
    REPO / "dizzy/src/dizzy/generators" / f"{name}.py"
    for name in ("procedures", "projections", "policies", "queries", "context_extras")
)


def test_no_authored_element_names_a_lane_a_pool_or_a_priority():
    """Placement is a DEPLOYMENT decision, and an element that names its lane
    has taken it. The check is static and it is on both halves of the seam:
    the authored element implementations that ship in this repo, and the
    generators that write the code those implementations sit inside — a lane
    reaching an element would arrive through a generated signature first.
    """
    assert ELEMENT_SOURCES, "no authored element implementations were found to check"
    # The canary first: the pattern fires on the shapes it is looking for, so a
    # green result below is a fact about the files and not about a dead regex.
    for leak in ("engine.run(lane='heavy')", "self.pool.submit(x)", "from wiring import topology"):
        assert TOPOLOGY_LEAK.search(leak), leak
    assert not TOPOLOGY_LEAK.search("priority = self.entry.priority_score")

    offenders = {
        str(path.relative_to(REPO)): sorted(set(TOPOLOGY_LEAK.findall(path.read_text())))
        for path in ELEMENT_SOURCES + ELEMENT_GENERATORS
        if TOPOLOGY_LEAK.search(path.read_text())
    }
    assert offenders == {}


def test_the_feature_file_has_no_vocabulary_for_placement():
    """The other half of the same claim, one level up: a feature-file cannot
    say it even if an author wants to. Placement lives in topology.yaml, and
    the schemas are where that separation is either kept or lost."""
    feat = (REPO / "dizzy/src/dizzy/def/feat.yaml").read_text()
    assert not TOPOLOGY_WORDS.search(feat)
    # The canary: the same regex DOES fire on the file placement belongs in,
    # so a green result above is a fact about the feature-file, not a fact
    # about a pattern that never matches anything.
    topology = (REPO / "dizzy/src/dizzy/def/topology.yaml").read_text()
    assert {w.lower() for w in TOPOLOGY_WORDS.findall(topology)} >= {"executor", "priority"}
