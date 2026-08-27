"""A HostApp a SPAWNED pool child can resolve — the far side of the boundary.

Module level, and only module level: everything crossing the pipe has to
pickle by reference, and a class defined inside a test function does not. This
module is imported by the child through the ``sys.path`` the parent hands it,
which is what the boundary tests are actually asserting.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dizzy.engine.loop import Engine
from dizzy.engine.ports import HostApp, Runtime, ShellServices
from dizzy.engine.registry import FeatGraph
from pydantic import BaseModel


class PoolPing(BaseModel):
    key: str


class PoolBoom(BaseModel):
    key: str


class PoolCrashOnce(BaseModel):
    """Kills its worker on the FIRST attempt and succeeds on the replay.

    *marker* is a path the child creates before dying, so "first attempt" is a
    fact on disk that survives the process it was decided in — the only way a
    once-only crash can be expressed to a child that is spawned fresh.
    """

    key: str
    marker: str


class PoolRendezvous(BaseModel):
    """Waits for *expect* distinct children before emitting.

    A cross-process barrier made of files, because that is the only rendezvous
    a spawned child shares with its siblings: each child drops a file named for
    its pid and waits until the directory holds *expect* of them. A lane one
    worker wide can never satisfy it, so the test that uses it is a real
    statement about the lane's WIDTH rather than about its pids.
    """

    key: str
    barrier_dir: str
    expect: int
    timeout: float = 20.0


class PoolStoreProbe(BaseModel):
    """Asks the CHILD what it can do to the host's log.

    Only meaningful under a host that declares no ``build_procedure_runtime``:
    the child then builds the app's full runtime, whose store resolves to the
    parent's own file. *marker* is where the answer is written, because the
    answer must survive the process that found it.
    """

    key: str
    marker: str


class PoolOpenProbe(BaseModel):
    """Asks the CHILD what it has OPEN — the cure, not the mitigation.

    ``PoolStoreProbe`` above pins what a fallback child may DO to the host's
    log; this one pins whether the file is touched at all. The answer is read
    out of ``/proc/self/fd``, because "holds no connection" is a fact about
    file descriptors and nothing weaker proves it: a store constructed and
    never written still holds one.
    """

    key: str
    marker: str


class PoolSleep(BaseModel):
    """Blocks in the CHILD for *seconds* — the forty-second procedure, in
    miniature. Touches *marker* first so the parent can wait on the fact that
    the child really is inside the procedure."""

    key: str
    marker: str
    seconds: float = 30.0


class PoolPinged(BaseModel):
    key: str
    pid: int


EVENT_CLASSES = {"pool_pinged": PoolPinged}
COMMAND_CLASSES = {
    "PoolPing": PoolPing,
    "PoolStoreProbe": PoolStoreProbe,
    "PoolOpenProbe": PoolOpenProbe,
    "PoolBoom": PoolBoom,
    "PoolCrashOnce": PoolCrashOnce,
    "PoolRendezvous": PoolRendezvous,
    "PoolSleep": PoolSleep,
}


def build_procedure_engine() -> Engine:
    """An engine with the procedures and NOTHING else: no store, no queue.

    ``run_procedure`` provably touches neither, which is the whole reason this
    half can run in another process.
    """
    engine = Engine(command_queue=None, store=None)

    def ping(command: PoolPing) -> None:
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    def boom(command: PoolBoom) -> None:
        # Emits FIRST, then raises: the stranded emit is what the child has to
        # discard, or the next job on this worker inherits it.
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))
        raise ValueError(f"boom {command.key}")

    def crash_once(command: PoolCrashOnce) -> None:
        # os._exit, not an exception: the point is a child the OS takes away
        # mid-procedure — no result, no traceback, a genuinely broken pool.
        marker = Path(command.marker)
        if not marker.exists():
            marker.write_text("crashed")
            os._exit(9)
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    def rendezvous(command: PoolRendezvous) -> None:
        room = Path(command.barrier_dir)
        room.mkdir(parents=True, exist_ok=True)
        (room / str(os.getpid())).write_text("here")
        end = time.monotonic() + command.timeout
        while len(list(room.iterdir())) < command.expect and time.monotonic() < end:
            time.sleep(0.005)
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    def sleeper(command: PoolSleep) -> None:
        Path(command.marker).write_text(str(os.getpid()))
        time.sleep(command.seconds)
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    engine.register_procedure(PoolRendezvous, rendezvous, name="rendezvous")
    engine.register_procedure(PoolSleep, sleeper, name="sleeper")
    engine.register_procedure(PoolPing, ping, name="ping")
    engine.register_procedure(PoolCrashOnce, crash_once, name="crash_once")
    engine.register_procedure(PoolBoom, boom, name="boom")
    return engine


def _build_procedure_runtime(services: ShellServices) -> Runtime:
    return Runtime(engine=build_procedure_engine())


def _build_runtime(services: ShellServices) -> Runtime:
    # The full builder a child falls back to when no procedure-only one is
    # declared. Here they coincide; the point under test is which one is CALLED.
    return Runtime(engine=build_procedure_engine())


def host_app() -> HostApp:
    """Procedure-only child: the field this design adds is populated."""
    return HostApp(
        graph=FeatGraph.__new__(FeatGraph),  # never consulted by a pool child
        build_runtime=_build_runtime,
        build_procedure_runtime=_build_procedure_runtime,
    )


def fallback_app() -> HostApp:
    """No ``build_procedure_runtime``: every host that exists today, unchanged."""
    return HostApp(graph=FeatGraph.__new__(FeatGraph), build_runtime=_build_runtime)


def build_store_engine() -> Engine:
    """The GENERATED WIRING's shape: an engine whose store is ``EventStore()``.

    No path argument, so the path comes from ``$DIZZY_STORE_PATH`` — which is
    inherited across a spawn, and is therefore the parent's own log. This is
    what a child gets today when its host declares no procedure-only runtime,
    and the probe below reports what that child can do with it.
    """
    from dizzy.engine.store import EventStore

    store = EventStore(event_classes=EVENT_CLASSES)
    engine = Engine(command_queue=None, store=store)

    def probe(command: PoolStoreProbe) -> None:
        try:
            store.append(PoolPinged(key="written-by-the-child", pid=os.getpid()))
        except Exception as exc:
            outcome = f"REFUSED {type(exc).__name__}"
        else:
            outcome = "WROTE THE HOST'S LOG"
        Path(command.marker).write_text(f"read_only={store.dag.read_only} {outcome}")
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    engine.register_procedure(PoolStoreProbe, probe, name="probe")
    return engine


def _build_store_runtime(services: ShellServices) -> Runtime:
    return Runtime(engine=build_store_engine())


def store_fallback_app() -> HostApp:
    """A host with NO ``build_procedure_runtime`` whose runtime opens a store —
    the shape every generated wiring has today."""
    return HostApp(graph=FeatGraph.__new__(FeatGraph), build_runtime=_build_store_runtime)


def report_queue_recovery(path: str, sink) -> None:
    """Open the host's command queue HERE, in a spawned child, and report.

    Stands in for the ordinary host shape — a module-scope
    ``DurableCommandQueue`` that a spawned worker re-imports as
    ``__mp_main__``. What it reports is how many rows opening the file
    re-queued, which must be none: crash recovery belongs to the host.
    """
    from dizzy.engine.st import DurableCommandQueue

    queue = DurableCommandQueue(registry=COMMAND_CLASSES, path=path)
    try:
        sink.put((queue.recovered, [(j["id"], j["status"]) for j in queue.jobs()]))
    finally:
        queue.close()


# ── The generated shape, both halves ────────────────────────────────────────


def _open_logs() -> list[str]:
    """Every path this process has open that looks like an event store.

    ``/proc`` is Linux-only, and the test that reads this skips elsewhere. The
    alternative — trusting ``engine.store is None`` — asserts the runtime the
    child MEANT to build rather than the file handles it actually holds, and
    the whole point of the assertion is the handle.
    """
    out = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:  # the fd closed under us, or the listdir's own handle
            continue
        if "events.db" in target:
            out.append(os.path.basename(target))
    return sorted(set(out))


def _register_open_probe(engine: Engine) -> None:
    def probe(command: PoolOpenProbe) -> None:
        Path(command.marker).write_text(
            f"store={'yes' if engine.store is not None else 'no'} open={_open_logs()}"
        )
        engine.emit_event(PoolPinged(key=command.key, pid=os.getpid()))

    engine.register_procedure(PoolOpenProbe, probe, name="open_probe")


def _generated_build_runtime(services: ShellServices) -> Runtime:
    """What the generated ``build_runtime`` does: ``EventStore(graph=...)`` with
    no path, so the path is the ``$DIZZY_STORE_PATH`` the child inherited."""
    from dizzy.engine.store import EventStore

    engine = Engine(command_queue=None, store=EventStore(event_classes=EVENT_CLASSES))
    _register_open_probe(engine)
    return Runtime(engine=engine)


def _generated_build_procedure_runtime(services: ShellServices) -> Runtime:
    """What the generated ``build_procedure_runtime`` does: the same procedures,
    ``store=None``."""
    engine = Engine(command_queue=None, store=None)
    _register_open_probe(engine)
    return Runtime(engine=engine)


def generated_app() -> HostApp:
    """The shape ``host_app()`` now emits: both builders, the child taking the
    procedure-only one."""
    return HostApp(
        graph=FeatGraph.__new__(FeatGraph),
        build_runtime=_generated_build_runtime,
        build_procedure_runtime=_generated_build_procedure_runtime,
    )


def pre_generated_app() -> HostApp:
    """The shape it emitted BEFORE: one builder, and the child falls back to it.
    Here so the probe above is calibrated — a probe that reports "nothing open"
    under both hosts is measuring nothing."""
    return HostApp(
        graph=FeatGraph.__new__(FeatGraph),
        build_runtime=_generated_build_runtime,
    )
