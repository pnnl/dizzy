"""The ``st`` lane executor — N lanes over one durable queue, one writer.

This is ``st`` with more lanes, not a third shell. The seam it closes is the
one :mod:`dizzy.engine.st` names in its own docstring: the worker loop that
drives ``claim -> run -> ack`` used to be the host's, so every host wrote it
again and no host could serve two lanes. :class:`LaneExecutor` promotes that
loop into the shell and parameterises it by the placement the generator
already emits (``LANES``: lane -> ``(executor, workers)``).

**The invariant, stated once.** The log has exactly ONE writer: the thread
that owns *engine*. Only that thread calls ``ingest``, ``run_procedure``,
``apply_event``, ``discard_pending_events`` or ``take_applied``. A pool lane's
thread touches the command queue and its process pool and nothing else; a pool
CHILD dispatches nothing (its command queue refuses) and appends nothing.

That last half is a property of the RUNTIME the child builds, not of anything
this module can enforce: a host that declares
:attr:`~dizzy.engine.ports.HostApp.build_procedure_runtime` gives the child an
engine with no store at all, and a host that does not gets whatever
``build_runtime`` opens — today, for the generated wiring, the parent's own
event-store file. It still OPENS it, and that is the residue: what it cannot
do is write it. A child stamps itself (:func:`~dizzy.engine.ports.in_pool_child`)
before the app builds anything, and an ``EventStore`` built in a marked
process opens the file read-only — no schema DDL against the host's log, no
writer connection held for the worker's lifetime. A host pooling anything
serious still owes itself the procedure-only builder, which opens nothing at
all; the generator does not emit one yet.

**The two lane kinds.**

- ``inline`` — served by the calling thread, in :meth:`LaneExecutor.pump`.
  Claim, run the procedure, ingest, ack. This is what a single-lane host does
  today, unchanged.
- ``pool`` — one lane THREAD in this process (claim, dispatch, collect) plus
  one spawned ``ProcessPoolExecutor``. It exists for HEAD-OF-LINE latency: a
  40-second procedure no longer stops the queue from being consulted. The pool
  is built on that thread's first claim, not at :meth:`start`, so a run that
  never reaches a pool lane spawns nothing at all.

**The ack rule.** ``claim`` flips ``queued -> running`` and nothing else moves
the row until the MAIN lane has ingested. ``mark_done``/``mark_error`` are
called only from the main lane, after ``ingest`` returned or raised, and they
are FENCED by the attempt number the claim owns: a completion belonging to an
attempt some recovery has already superseded is dropped rather than allowed to
ack the live row. A pool lane thread never acks. So a crash — of the child or
of the parent — leaves the row ``running``, and the queue's own recovery
re-queues it: at-least-once, exactly what ``st`` already promised for a crash
mid-``run_command``.

**What at-least-once costs here, stated plainly.** A replay APPENDS THE FACT
AGAIN. ``ingested_at`` is inside the hashed payload, so a byte-identical fact
appended a second time lands under a new content address and nothing dedups
it: the parent that dies between ``ingest`` (append + fold) and the ack has
written the event once and will write it again on the replay, and every
projection folds it twice. That is the bargain — repeated, never lost — and it
is why a projection must be idempotent on its own key. It is not "absorbed" by
the store.

**What this does NOT promise.** A pool procedure runs CONCURRENTLY with the
main lane, so whether its events land before or after the inline command the
main lane happens to be running is a race, every time. ``ordered=True``
restores the DISPATCH order of one pool lane's own results — which, with
confluent projections, pins the final read models and pins that lane's own
relative order. It does not make the log deterministic, and it is not the
sequential consistency inline ``st`` claims. A test that wants a deterministic
LOG wants ``executor: inline``, which already exists.

Stdlib only: ``concurrent.futures`` + ``multiprocessing`` + ``threading``.
Nothing here names a command, an event, or an environment field.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback as _traceback
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import BrokenExecutor, Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from dizzy.engine.ports import HostApp, ShellServices, mark_pool_child

DEFAULT_POLL_INTERVAL = 0.05
"""How long the main lane sleeps on an empty pass. Pool results bypass it —
a completion rings the nudge — so this is the floor on a NEWLY ENQUEUED inline
command only."""

POOL_MAX_ATTEMPTS = 3
"""How many times one row may be dispatched to a pool before a worker death
leaves it errored for good.

The bound is on the ROW's own ``attempts`` column, read back at completion
time, so it is durable across a restart and cannot be defeated by a process
that dies. It is not a per-process set of ids, which was both unbounded in
memory and wrong: ``BrokenProcessPool`` poisons every in-flight future on the
lane, so a row that was merely a BYSTANDER to another job's child death burned
its one retry without its procedure ever having run, and a second unrelated
death errored it permanently. The parent cannot tell victim from bystander —
the pool reports both identically — so the budget is per row and generous
enough that one unrelated death is not fatal, while a genuine poison pill
still stops after three."""

DEFAULT_LANES: dict[str, tuple[str, int]] = {"default": ("inline", 1)}
"""What a host with no topology gets: one inline lane, served by the caller.
Byte for byte the behaviour of the single-lane loop hosts write today, and it
spawns nothing."""


# ── The child side: what crosses the pipe, and what runs over there ─────────


@dataclass(frozen=True)
class ProcedureResult:
    """What a pool child hands back. Importable from both sides, picklable.

    *events* are the generated pydantic instances exactly as ``run_procedure``
    returned them. They pickle by class reference (the generated classes are
    module-level) and the parent ingests them byte for byte the way a locally
    run procedure's events are ingested — which is precisely what
    :meth:`Engine.ingest` promises. Rebuilding them from payloads instead
    would re-run validation on data a procedure legitimately produced, and
    would need an envelope that does not exist yet.

    *error* is a STRING, never an exception object: app exceptions are
    frequently unpicklable, and an unpicklable exception in a worker degrades
    into a confusing pool-level failure. A string plus a formatted traceback is
    what the queue stores anyway.
    """

    events: list[Any] = field(default_factory=list)
    error: str | None = None
    traceback: str | None = None
    pid: int = 0
    duration_ms: float = 0.0


class _RefusingQueue:
    """The command queue a pool child gets. Nothing over there may dispatch:
    a policy runs on the main lane, after ingest, in the writer's process."""

    def put(self, command: Any, origin: str = "policy") -> Any:
        raise RuntimeError(
            "a pool child may not dispatch commands — policies run on the main "
            "lane after ingest, not in the process that ran the procedure"
        )

    def qsize(self) -> int:
        return 0


def _noop_publish(record: Any) -> None:
    return None


def _noop_observer(name: str, event: Any) -> None:
    return None


_RUNTIME: Any = None
"""The child's procedure runtime, built once per worker PROCESS.

Safe because ``ProcessPoolExecutor`` runs one task at a time per worker, which
is also why an in-process double for ``pool_factory`` must be immediate rather
than threaded — see that parameter's docstring."""


def _child_runtime(spec: str | None, sys_path: Sequence[str], lane: str) -> Any:
    global _RUNTIME
    if _RUNTIME is None:
        # Say what this process is BEFORE the app builds anything: a host that
        # declares no procedure-only runtime falls back to its full one, and
        # the store that builder opens must not be able to write the log.
        # Guarded inside mark_pool_child() so an in-process pool double, which
        # runs this on the HOST's thread, marks nothing.
        mark_pool_child()
        # `multiprocessing.spawn` already ships the parent's sys.path today, so
        # this is redundant — three lines that cost nothing and remove a
        # dependency on a CPython implementation detail. Nothing requires the
        # workspace on $PYTHONPATH.
        for entry in reversed([p for p in sys_path if p not in sys.path]):
            sys.path.insert(0, entry)
        app = HostApp.resolve(spec)
        _RUNTIME = app.procedure_runtime(
            ShellServices(
                command_queue=_RefusingQueue(),
                publish=_noop_publish,
                observer=_noop_observer,
                pool=lane,
            )
        )
    return _RUNTIME


def run_procedure_job(
    spec: str | None, sys_path: Sequence[str], lane: str, command: Any
) -> ProcedureResult:
    """The pool child's entry point: run ONE procedure, return its events.

    A module-level function so pickle-by-reference resolves it in the child by
    importing :mod:`dizzy`, never the host's ``__main__``.

    The runtime is built lazily HERE rather than in an ``initializer=``,
    because an exception in an initializer breaks the whole pool, whereas a
    boot failure here comes back as a ``ProcedureResult`` naming the spec —
    which is something an operator can act on.
    """
    started = time.monotonic()

    def elapsed() -> float:
        return round((time.monotonic() - started) * 1000, 3)

    try:
        runtime = _child_runtime(spec, sys_path, lane)
    except BaseException as exc:  # noqa: BLE001 - reported, never raised across the pipe
        return ProcedureResult(
            error=f"host app {spec!r}: {type(exc).__name__}: {exc}",
            traceback=_traceback.format_exc(),
            pid=os.getpid(),
            duration_ms=elapsed(),
        )
    try:
        runtime.refresh()
        events = list(runtime.engine.run_procedure(command, lane=lane))
    except BaseException as exc:  # noqa: BLE001 - the failure IS the return value
        # The emits of a procedure that raised are stranded on THIS engine's
        # queue, and this child serves the next job. Drop them.
        try:
            runtime.engine.discard_pending_events()
        except Exception:  # pragma: no cover - a discard that raises is inert
            pass
        return ProcedureResult(
            error=f"{type(exc).__name__}: {exc}",
            traceback=_traceback.format_exc(),
            pid=os.getpid(),
            duration_ms=elapsed(),
        )
    return ProcedureResult(events=events, pid=os.getpid(), duration_ms=elapsed())


def _shutdown_quietly(pool: Executor) -> None:
    """Shut a discarded pool down, never raising: it is already unusable."""
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # pragma: no cover - a discarded pool's shutdown is inert
        pass


def _shutdown_bounded(pool: Executor, budget: float) -> None:
    """Shut *pool* down inside *budget* seconds, whatever its children are doing.

    ``shutdown(wait=True)`` is unbounded by construction, and ``cancel_futures``
    reaches only the tasks that have not started — so a host stopping a lane
    that is mid-procedure waited for that procedure, however long it is. Here
    the executor is told to stop taking work, the workers are given what is
    left of the budget to finish on their own, and the survivors are
    terminated. ``_processes`` is a ``ProcessPoolExecutor`` implementation
    detail and is treated as one: absent (an in-process double, another
    Executor kind) means there is nothing to bound and the shutdown stands.
    """
    # Read the worker set BEFORE the shutdown: the pool's own manager thread
    # clears it as it winds down, and a bound over an empty list bounds nothing.
    processes = list((getattr(pool, "_processes", None) or {}).values())
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except TypeError:  # pragma: no cover - an Executor predating cancel_futures
        pool.shutdown(wait=False)
    except Exception:  # pragma: no cover - a shutdown that raises is inert
        return
    end = time.monotonic() + max(0.0, budget)
    for proc in processes:
        try:
            proc.join(timeout=max(0.0, end - time.monotonic()))
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=0.5)
            if proc.is_alive():  # pragma: no cover - a child ignoring SIGTERM
                proc.kill()
                proc.join(timeout=0.5)
        except Exception:  # pragma: no cover - a process already reaped
            pass


def spawn_pool(lane: str, workers: int) -> Executor:
    """The default ``pool_factory``: a SPAWN-context process pool.

    Spawn is not a preference. A forked child inherits this process's sqlite
    connections and its telemetry threads, and will corrupt or hang. The
    context is pinned explicitly rather than inherited, because the platform
    default is fork on Linux and ``forkserver`` on newer CPython.
    """
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


# ── The parent side ─────────────────────────────────────────────────────────


@dataclass
class Completion:
    """One finished unit of pool work, on its way to the main lane."""

    job_id: int
    lane: str
    seq: int
    command_type: str
    attempt: int | None = None
    """The row's attempt number at claim time — the fencing token. ``None``
    for a queue that cannot supply one, which acks unfenced as before."""
    events: list[Any] = field(default_factory=list)
    error: str | None = None
    pid: int = 0
    duration_ms: float = 0.0
    requeue: bool = False
    """Set when the WORKER died rather than the procedure failing: the main
    lane marks the row errored (so it leaves ``running``) and then retries it,
    while the row's own attempts are under :data:`POOL_MAX_ATTEMPTS`."""


class _LaneState:
    """One pool lane's parent-side state. Every field is under ``lock``."""

    def __init__(self, name: str, workers: int, backlog: int):
        self.name = name
        self.workers = workers
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(max(1, workers + backlog))
        self.pool: Executor | None = None
        self.generation = 0
        self.next_seq = 0
        self.next_out = 0
        self.held: dict[int, Completion] = {}
        self.inflight = 0
        self.thread: threading.Thread | None = None


class LaneExecutor:
    """Drive N lanes off one durable command queue, with one writer.

    *lanes* is the generated ``LANES`` table verbatim — ``{name: (executor,
    workers)}`` — and is the ONLY app-shaped thing this class takes. It is
    read, never interpreted: the executor knows the two strings ``inline`` and
    ``pool`` and no lane name at all.

    *engine* is the one writer's engine, and this executor must be its only
    owner: :meth:`Engine.take_applied` is engine-wide and destructive, so two
    executors over one engine would consume each other's positions. That is
    asserted in the constructor rather than discovered by a double dispatch.

    :meth:`pump` is deliberately the exact shape
    :class:`dizzy.engine.groups.GroupWorker` documents for its ``commands=``
    argument — "runs at most one unit of work and returns whether it did" — so
    this composes with the group worker instead of competing with it. Pass
    ``owns_units=False`` when embedding it that way; see that parameter.
    """

    def __init__(
        self,
        command_queue: Any,
        engine: Any,
        lanes: Mapping[str, tuple[str, int]] | None = None,
        host_app_spec: str | None = None,
        session: Any = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        ordered: bool = False,
        backlog: int = 0,
        pool_factory: Callable[[str, int], Executor] | None = None,
        bus: Callable[[dict], None] | None = None,
        on_unit_done: Callable[[tuple[int, ...]], None] | None = None,
        owns_units: bool = True,
    ):
        #: *host_app_spec* is passed to the child EXPLICITLY, falling back to
        #: ``$DIZZY_HOST_APP`` only if it is None. Environment does cross a
        #: spawn, but passing it means a test needs no ``monkeypatch.setenv``
        #: to reach the child, and two executors in one process can serve two
        #: apps.
        #:
        #: *ordered* opts into the per-lane reorder buffer. See the module
        #: docstring for what it does and does not buy.
        #:
        #: *backlog* is extra in-flight rows per pool lane beyond ``workers``.
        #: It defaults to 0 so the queue-visible ``running`` set stays equal to
        #: the actually-running set — every claimed-but-un-ingested row is a
        #: row that replays on a crash, which makes this a correctness dial
        #: and not a throughput one.
        #:
        #: *pool_factory* is ``(lane, workers) -> Executor``, defaulting to
        #: :func:`spawn_pool`. **The executor it returns must run at most one
        #: task at a time per worker**: the child's runtime is a per-process
        #: singleton and its engine holds one event queue, so a threaded double
        #: would race. An in-process double must therefore be IMMEDIATE
        #: (run-on-submit) or build a fresh runtime per call.
        #:
        #: *owns_units* decides who marks the unit boundary. True (the default)
        #: means this executor calls ``take_applied()`` after EVERY unit —
        #: which it must, because a completion's positions left undrained are
        #: cleared and lost by the next ``run_command``-shaped unit. False is
        #: for ``GroupWorker(commands=executor.pump)``: positions then
        #: accumulate across the completions and the inline command of one
        #: pass, and the worker drains one merged skip-list. Merging positions
        #: is safe; losing them is not.
        self._queue = command_queue
        self._engine = engine
        self._lanes = dict(lanes) if lanes else dict(DEFAULT_LANES)
        self._spec = host_app_spec
        self._session = session
        self._poll_interval = poll_interval
        self._ordered = ordered
        self._backlog = max(0, int(backlog))
        self._pool_factory = pool_factory or spawn_pool
        self._bus = bus
        self._on_unit_done = on_unit_done
        self._owns_units = owns_units

        self._claim_engine()

        self.inline_lanes = tuple(
            name for name, (kind, _w) in self._lanes.items() if kind != "pool"
        )
        self._pool_lanes: dict[str, _LaneState] = {
            name: _LaneState(name, max(1, workers), self._backlog)
            for name, (kind, workers) in self._lanes.items()
            if kind == "pool"
        }
        #: Cap on completions drained per pass, so a flood of pool results
        #: cannot starve the inline lane.
        self._drain_cap = max(1, sum(st.workers for st in self._pool_lanes.values()))
        self._inbox: queue.SimpleQueue[Completion] = queue.SimpleQueue()
        self._nudge = threading.Event()
        self._stop = threading.Event()
        self._started = False

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @property
    def pool_lanes(self) -> tuple[str, ...]:
        return tuple(self._pool_lanes)

    def _claim_engine(self) -> None:
        """Take single ownership of *engine*, for as long as this is running.

        The invariant is one LIVE owner, not one owner ever: :meth:`stop`
        releases the claim, so a host reconfiguring its topology can build a
        second executor over the same engine once the first has stopped.
        """
        owner = getattr(self._engine, "_dizzy_lane_executor", None)
        if owner is not None and owner is not self:
            raise RuntimeError(
                "this engine already has a LaneExecutor: take_applied() is "
                "engine-wide and destructive, so two of them would consume each "
                "other's applied positions — give the second lane set its own engine"
            )
        self._engine._dizzy_lane_executor = self

    def _release_engine(self) -> None:
        if getattr(self._engine, "_dizzy_lane_executor", None) is self:
            self._engine._dizzy_lane_executor = None

    def start(self) -> LaneExecutor:
        """Start one thread per POOL lane. With no pool lanes this starts
        nothing, spawns nothing, and is what a single-lane host has today."""
        if self._started:
            return self
        self._claim_engine()  # re-taken after a stop released it
        self._started = True
        self._stop.clear()
        for state in self._pool_lanes.values():
            state.thread = threading.Thread(
                target=self._serve_pool_lane,
                args=(state,),
                name=f"dizzy-lane-{state.name}",
                daemon=True,
            )
            state.thread.start()
        return self

    def stop(self, timeout: float = 5.0, drain: bool = True) -> None:
        """Stop the lane threads, shut the pools down, drain what came home.

        *timeout* bounds the WHOLE of it, and that is the point: it used to
        bound only the thread joins, while the pool shutdown waited — without a
        deadline, and ``cancel_futures`` cannot cancel a task already running —
        for the longest in-flight procedure. With a 40-second procedure, which
        is the case a pool lane exists for, the honest answer to "drain, drop
        or hang?" was hang. Now the workers are given what is left of the
        budget and then taken away.

        Completions already home are then INGESTED rather than discarded: this
        thread is the writer, the work is already paid for, and dropping them
        left rows stuck ``running`` in a queue object nothing in this process
        would ever recover. ``drain=False`` skips that, and exists for one
        thing — a test standing in for a process that DIES in this window,
        which of course ingests nothing. A host has no use for it: the rows it
        leaves ``running`` come back only through a restart's recovery.
        Idempotent.
        """
        end = time.monotonic() + max(0.0, timeout)
        self._stop.set()
        self._nudge.set()
        for state in self._pool_lanes.values():
            thread, state.thread = state.thread, None
            if thread is not None:
                thread.join(timeout=max(0.0, end - time.monotonic()))
        for state in self._pool_lanes.values():
            with state.lock:
                pool, state.pool = state.pool, None
            if pool is not None:
                _shutdown_bounded(pool, max(0.0, end - time.monotonic()))
        if drain:
            self.drain()
        self._started = False
        self._release_engine()

    def drain(self, limit: int = 10_000) -> int:
        """Ingest every completion already on the inbox, on THIS thread.

        The calling thread is the writer, so this is the same write path
        :meth:`pump` takes. *limit* is a runaway guard, not a policy.
        """
        drained = 0
        while drained < limit:
            try:
                completion = self._inbox.get_nowait()
            except queue.Empty:
                return drained
            self._ingest_completion(completion)
            drained += 1
        return drained

    def __enter__(self) -> LaneExecutor:
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False

    # ── The main lane ───────────────────────────────────────────────────────

    def pump(self) -> bool:
        """Run at most one unit of work on THIS thread; True iff it did.

        A unit is either one pool completion ingested, or one inline command
        claimed and run. Completions are drained first, up to
        :attr:`_drain_cap`, so results do not queue up behind the inline lane
        indefinitely — but the cap keeps them from starving it either.
        """
        self._nudge.clear()
        worked = False
        for _ in range(self._drain_cap):
            try:
                completion = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._ingest_completion(completion)
            worked = True
        for lane in self.inline_lanes:
            job = self._claim(lane, timeout=0)
            if job is None:
                continue
            self._run_inline(lane, *job)
            return True
        return worked

    def run_forever(self) -> None:
        """Pump until :meth:`stop`. Sleeps on the nudge between empty passes."""
        self.start()
        while not self._stop.is_set():
            if not self.pump():
                self._nudge.wait(self._poll_interval)

    def run_until_idle(self, deadline: float = 5.0) -> bool:
        """Pump until nothing is queued, in flight, or waiting to be ingested.

        True if quiescence was reached, False if *deadline* seconds elapsed
        first — a wall-clock cap so a test drains rather than sleeps and hopes.
        """
        self.start()
        end = time.monotonic() + deadline
        while True:
            worked = self.pump()
            if not worked:
                if self.idle():
                    return True
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._nudge.wait(min(self._poll_interval, remaining))
            elif time.monotonic() >= end:
                return False

    def idle(self) -> bool:
        """Nothing queued, nothing dispatched, nothing awaiting ingest.

        Read off the QUEUE where the queue can say so: a row is ``running``
        from the instant a lane claims it until the main lane acks, and those
        two transitions are exactly the boundaries of "in flight". That leaves
        no window in which a unit of work is invisible — which a counter kept
        on this side does have, because a lane thread claims a row before it
        can increment anything.
        """
        if not self._inbox.empty():
            return False
        for state in self._pool_lanes.values():
            with state.lock:
                if state.inflight or state.held:
                    return False
        counts = getattr(self._queue, "counts", None)
        if counts is not None:
            tally = counts()
            return not (tally.get("queued") or tally.get("running"))
        return not self._queue.qsize()

    # ── Claim and ack, both fenced where the queue can be ───────────────────

    def _claim(self, lane: str, timeout: float) -> tuple[int, int | None, Any] | None:
        """Claim one row, with its attempt number when the queue keeps one.

        ``claim_fenced`` is optional so this composes with the plain
        ``claim``-shaped queues a host or a test may pass; without it the acks
        below are unfenced, which is exactly the behaviour that shipped before.
        """
        fenced = getattr(self._queue, "claim_fenced", None)
        if fenced is not None:
            return fenced(timeout=timeout, lane=lane)
        job = self._queue.claim(timeout=timeout, lane=lane)
        if job is None:
            return None
        job_id, command = job
        return job_id, None, command

    def _ack(self, job_id: int, attempt: int | None, error: str | None) -> bool:
        """Ack one row, fenced by *attempt*. False iff it was superseded.

        A superseded ack is DROPPED, not forced: the row it names has already
        been re-queued by a recovery and re-claimed by somebody else, and
        writing to it would erase a replay that is the only thing left standing
        between this command and never running at all.
        """
        if error is None:
            done = self._queue.mark_done
            landed = done(job_id, attempt) if attempt is not None else done(job_id)
        else:
            failed = self._queue.mark_error
            landed = (
                failed(job_id, error, attempt) if attempt is not None else failed(job_id, error)
            )
        # A queue whose ack returns None (anything queue.Queue-shaped, or a
        # host's own) is taken at its word: it landed.
        return landed is not False

    # ── One unit of work, either shape ──────────────────────────────────────

    def _rollback(self) -> None:
        if self._session is not None:
            try:
                self._session.rollback()
            except Exception:  # pragma: no cover - a rollback that raises is inert
                pass

    def _finish_unit(self) -> None:
        # Unconditionally, whether or not on_unit_done is set: positions left
        # on the engine are cleared and lost by the next run_command-shaped
        # unit, and a hybrid group worker would then re-run policies that
        # already dispatched.
        if not self._owns_units:
            return
        applied = self._engine.take_applied()
        if self._on_unit_done is not None:
            self._on_unit_done(applied)

    def _run_inline(self, lane: str, job_id: int, attempt: int | None, command: Any) -> None:
        # run_procedure + ingest rather than run_command: run_command CLEARS
        # applied_positions on entry, which under owns_units=False would throw
        # away the positions of the completions ingested earlier in this pass.
        # The composition is otherwise identical.
        try:
            self._engine.ingest(self._engine.run_procedure(command, lane=lane))
        except Exception as exc:
            self._engine.discard_pending_events()
            self._rollback()
            self._ack(job_id, attempt, f"{type(exc).__name__}: {exc}")
        else:
            self._ack(job_id, attempt, None)
        finally:
            self._finish_unit()

    def _undeclared(self, events: Sequence[Any]) -> list[str]:
        """Event types the store could not read back. Checked BEFORE the log.

        ``EventStore.append`` never consults its class map: it dumps whatever
        it is handed and stamps the class name. An event a child produced from
        a class this store does not declare therefore lands in the log happily
        and only surfaces later, at ``reconstruct_event`` / rebuild / replicate
        time — by which point the command's intent row is long gone and the
        stream cannot be folded at all. This is the one place a foreign event
        can enter, so it is the place to refuse it.
        """
        try:
            declared = set(self._engine.store.event_classes.values())
        except Exception:  # pragma: no cover - a store with no class map opts out
            return []
        if not declared:
            return []
        return sorted({type(e).__name__ for e in events if type(e) not in declared})

    def _ingest_completion(self, completion: Completion) -> None:
        self._emit_bus(completion)
        try:
            if completion.error is not None:
                self._engine.discard_pending_events()
                self._rollback()
                if self._ack(completion.job_id, completion.attempt, completion.error):
                    if completion.requeue:
                        self._queue.retry(completion.job_id)
                return
            unknown = self._undeclared(completion.events)
            if unknown:
                self._engine.discard_pending_events()
                self._rollback()
                self._ack(
                    completion.job_id,
                    completion.attempt,
                    f"lane {completion.lane!r} returned {', '.join(unknown)}, which this "
                    "store does not declare — appending it would write a stream nothing "
                    "can fold",
                )
                return
            try:
                self._engine.ingest(completion.events)
            except Exception as exc:
                self._engine.discard_pending_events()
                self._rollback()
                self._ack(completion.job_id, completion.attempt, f"{type(exc).__name__}: {exc}")
            else:
                self._ack(completion.job_id, completion.attempt, None)
        finally:
            self._finish_unit()

    def _emit_bus(self, completion: Completion) -> None:
        # The child's engine has no bus, so the `lane=` stamp run_procedure
        # writes there is lost with it. The parent emits it instead, so
        # telemetry still says where a procedure ran.
        if self._bus is None:
            return
        self._bus(
            {
                "kind": "procedure",
                "name": completion.command_type,
                "trigger": completion.command_type,
                "duration_ms": completion.duration_ms,
                "outcome": "error" if completion.error else "ok",
                "detail": completion.error or "",
                "lane": completion.lane,
                "pid": completion.pid,
            }
        )

    # ── A pool lane: claim, dispatch, collect ───────────────────────────────

    def _pool_for(self, state: _LaneState) -> tuple[Executor, int]:
        with state.lock:
            if state.pool is None:
                state.pool = self._pool_factory(state.name, state.workers)
            return state.pool, state.generation

    def _serve_pool_lane(self, state: _LaneState) -> None:
        while not self._stop.is_set():
            if not state.slots.acquire(timeout=self._poll_interval):
                continue
            job = None
            try:
                if not self._stop.is_set():
                    job = self._claim(state.name, timeout=self._poll_interval)
            except Exception:  # pragma: no cover - a claim that raises ends the lane
                job = None
            if job is None:
                state.slots.release()
                continue
            job_id, attempt, command = job
            with state.lock:
                state.inflight += 1
                seq = state.next_seq
                state.next_seq += 1
            self._dispatch(state, seq, job_id, attempt, command)

    def _dispatch(
        self, state: _LaneState, seq: int, job_id: int, attempt: int | None, command: Any
    ) -> None:
        command_type = type(command).__name__
        try:
            pool, generation = self._pool_for(state)
            future = pool.submit(run_procedure_job, self._spec, list(sys.path), state.name, command)
        except Exception as exc:
            # An unpicklable command, or a pool that will not start: the row
            # leaves `running` with a message naming the lane rather than
            # hanging there.
            self._deliver(
                state,
                Completion(
                    job_id=job_id,
                    lane=state.name,
                    seq=seq,
                    command_type=command_type,
                    attempt=attempt,
                    error=f"lane {state.name!r} could not dispatch: {type(exc).__name__}: {exc}",
                ),
            )
            state.slots.release()
            return
        future.add_done_callback(
            lambda f, s=state, q=seq, j=job_id, a=attempt, t=command_type, g=generation: (
                self._on_done(s, q, j, a, t, g, f)
            )
        )

    def _on_done(
        self,
        state: _LaneState,
        seq: int,
        job_id: int,
        attempt: int | None,
        command_type: str,
        generation: int,
        future: Future,
    ) -> None:
        # This runs on a pool-internal thread and `concurrent.futures` SWALLOWS
        # its exceptions — a silent callback is how this design would lose a
        # job forever. So the whole body is wrapped, twice.
        try:
            completion = self._completion_for(
                state, seq, job_id, attempt, command_type, generation, future
            )
        except BaseException as exc:  # noqa: BLE001 - never lose a job
            completion = Completion(
                job_id=job_id,
                lane=state.name,
                seq=seq,
                command_type=command_type,
                attempt=attempt,
                error=(
                    f"lane {state.name!r} completion callback failed: {type(exc).__name__}: {exc}"
                ),
            )
        try:
            self._deliver(state, completion)
        finally:
            state.slots.release()

    def _completion_for(
        self,
        state: _LaneState,
        seq: int,
        job_id: int,
        attempt: int | None,
        command_type: str,
        generation: int,
        future: Future,
    ) -> Completion:
        try:
            result = future.result()
        except BrokenExecutor as exc:
            # A child killed by the OS (OOM, segfault) poisons EVERY in-flight
            # future on this lane, not just the victim. Nothing was appended —
            # the log has one writer and it is alive — so the pool is rebuilt
            # and the row replays while its own durable attempt budget lasts.
            # See POOL_MAX_ATTEMPTS for why the budget is the row's and not a
            # set of ids kept on this side.
            self._rebuild_pool(state, generation)
            return Completion(
                job_id=job_id,
                lane=state.name,
                seq=seq,
                command_type=command_type,
                attempt=attempt,
                error=f"lane {state.name!r} worker died: {type(exc).__name__}: {exc}",
                requeue=attempt is not None and attempt < POOL_MAX_ATTEMPTS,
            )
        except BaseException as exc:  # noqa: BLE001 - reported, never re-raised
            return Completion(
                job_id=job_id,
                lane=state.name,
                seq=seq,
                command_type=command_type,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
        return Completion(
            job_id=job_id,
            lane=state.name,
            seq=seq,
            command_type=command_type,
            attempt=attempt,
            events=list(result.events),
            error=result.error,
            pid=result.pid,
            duration_ms=result.duration_ms,
        )

    def _rebuild_pool(self, state: _LaneState, generation: int) -> None:
        with state.lock:
            if state.generation != generation:
                return  # a sibling future already rebuilt it
            pool, state.pool = state.pool, None
            state.generation += 1
        if pool is not None:
            # Off-thread, and that is not a preference. This runs on the POOL's
            # own management thread: `terminate_broken` holds the executor's
            # `_shutdown_lock` while it sets BrokenProcessPool on every pending
            # future, and `shutdown()` takes that same lock. Shutting the pool
            # down inline therefore self-deadlocks the manager thread — the
            # completion callback never returns, the job is never delivered,
            # and the lane is wedged for the life of the process. Found by
            # killing a real child; a hand-broken Future cannot show it.
            threading.Thread(
                target=_shutdown_quietly,
                args=(pool,),
                name=f"dizzy-pool-shutdown-{state.name}",
                daemon=True,
            ).start()

    def _deliver(self, state: _LaneState, completion: Completion) -> None:
        """Hand a completion to the main lane — in completion order, or, with
        ``ordered=True``, in this lane's own DISPATCH order.

        The reorder buffer lives here, per lane, because ``seq`` is meaningful
        only within one claiming loop: two pool lanes claim independently and
        there is no dispatch order between them to restore.
        """
        ready: list[Completion] = []
        with state.lock:
            state.inflight -= 1
            if not self._ordered:
                ready.append(completion)
            else:
                state.held[completion.seq] = completion
                while state.next_out in state.held:
                    ready.append(state.held.pop(state.next_out))
                    state.next_out += 1
        for item in ready:
            self._inbox.put(item)
        if ready:
            self._nudge.set()
