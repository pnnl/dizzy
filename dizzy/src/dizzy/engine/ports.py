"""The ports an engine shell consumes — the seam that de-apps the shells.

A *scheduling shell* (``dizzy.engine.st``, ``dizzy.engine.mp``) decides who
holds the command
queue, who runs the workers, and where telemetry lands. It must decide those
things without knowing a single command name, event name, or environment
field. Everything app-specific therefore arrives through one object:

    HostApp
      .graph              the FeatGraph — the declared topology, resolved
      .build_runtime()    build this process's Engine (the app's wiring)
      .build_procedure_runtime()  a procedure-only Engine, for a pool child [optional]
      .routes()           command -> (pool, message options)   [optional]
      .otel               tracing/metrics/propagation provider  [optional]
      .origin_for()       correlation to carry on a dispatch    [optional]
      .on_command_done()  the app's post-command hook           [optional]
      .span_attrs()       origin -> tracing attributes          [optional]

The shell finds the HostApp through ``$DIZZY_HOST_APP`` (``module:attr``), so a
worker process boots from environment alone — no app import in the shell's
source. Everything but ``graph`` and ``build_runtime`` has a null default, so
a minimal app supplies two things.

"""

from __future__ import annotations

import importlib
import multiprocessing
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dizzy.engine.registry import FeatGraph
from dizzy.engine.registry import graph as default_graph

# ── The two ports a shell provides to the engine ────────────────────────────


@runtime_checkable
class CommandQueue(Protocol):
    """Where a policy's dispatch goes. The engine holds one of these."""

    def put(self, command: Any, origin: str = "policy") -> Any: ...

    def qsize(self) -> int: ...


@runtime_checkable
class TelemetryBus(Protocol):
    """Host-level observation — never events, never load-bearing."""

    def emit(self, record: dict) -> None: ...


Publish = Callable[[dict], None]


# ── Which process this is: host, or a pool child a shell spawned ────────────

POOL_CHILD_ENV = "DIZZY_POOL_CHILD"
"""Marker a shell stamps into a process it spawned to RUN PROCEDURES ONLY.

A pool child appends nothing and dispatches nothing, but until it declares
:attr:`HostApp.build_procedure_runtime` it still builds the app's full runtime
— which, for the generated wiring, means an ``EventStore`` on the path the
parent's own store resolves to (inherited across the spawn). The marker is how
the store learns that this process must not be able to write that file. It is
a fact about the PROCESS, not about any app: nothing here is a feature's
environment field."""


def mark_pool_child() -> None:
    """Stamp this process as a pool child. Only ever called IN the child.

    Guarded on ``parent_process()`` rather than trusted from the variable
    alone: an in-process ``pool_factory`` double runs the child entry point on
    the HOST's own thread, and a marker set there would make the host's store
    read-only for the rest of its life.
    """
    if multiprocessing.parent_process() is not None:
        os.environ[POOL_CHILD_ENV] = "1"


def in_pool_child() -> bool:
    """True only inside a spawned pool child — never in the host process."""
    return bool(os.environ.get(POOL_CHILD_ENV)) and multiprocessing.parent_process() is not None


# ── The no-op tracing provider (a shell must run without OTel) ──────────────


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_status(self, *a, **k) -> None:
        pass


class _NullTracer:
    def start_as_current_span(self, *a, **k):
        return _NullSpan()


class _NullInstrument:
    def record(self, *a, **k) -> None:
        pass

    def add(self, *a, **k) -> None:
        pass


class _NullMeter:
    def create_histogram(self, *a, **k):
        return _NullInstrument()

    def create_counter(self, *a, **k):
        return _NullInstrument()


class NullOtel:
    """Satisfies the tracing surface a shell uses, doing nothing."""

    def init(self, service_name: str) -> None:
        pass

    def tracer(self) -> Any:
        return _NullTracer()

    def meter(self) -> Any:
        return _NullMeter()

    def inject(self) -> dict:
        return {}

    def extract(self, carrier: dict | None) -> Any:
        return None

    def trace_id_hex(self) -> str:
        return ""


# ── What a shell hands the app, and what it gets back ───────────────────────


def _noop_observer(name: str, event: Any) -> None:
    return None


def chain_observers(*observers: Callable[[str, Any], None]) -> Callable[[str, Any], None]:
    """Compose observers into the one the engine accepts, in order."""

    def observer(name: str, event: Any) -> None:
        for obs in observers:
            obs(name, event)

    return observer


@dataclass
class ShellServices:
    """The shell's side of the contract, passed to ``build_runtime``.

    The app builds its Engine around these: dispatches go to *command_queue*,
    observations to *publish*. Telemetry sinks are the app's to construct —
    a sink that must cross the process boundary is just one that closes over
    *publish*, which keeps the shell ignorant of the app's payload shapes.
    """

    command_queue: Any
    publish: Publish
    pool: str = "default"

    observer: Callable[[str, Any], None] = _noop_observer
    """The SHELL's event observer, which the app MUST call from whatever
    observer it passes to its engine builder.

    The engine takes exactly one observer, so an app that installs its own
    without chaining this one silently unplugs the shell: mp collects the
    events a command emitted here, and ``on_command_done`` receives that list.
    Dropping it degrades every result to "no events emitted" rather than
    failing, which is why it is stated as a requirement and not a nicety.
    Use :func:`chain_observers` if you have nothing app-specific to add.
    """


@dataclass
class Runtime:
    """One process's live engine, as the app built it."""

    engine: Any
    session: Any = None
    """The read-model session, when there is one — the shell rolls it back
    after a failed command so a partial fold can't leak into the next."""
    refresh: Callable[[], None] = lambda: None
    """Re-hydrate mutable environment before each command (secrets can change
    under a long-lived worker). Derive the field list from
    ``graph.environment`` rather than listing it."""


# ── The app manifest a shell resolves at boot ───────────────────────────────


def _no_origin(event: Any, command: Any) -> str | None:
    return None


def _no_hook(*a, **k) -> None:
    return None


def _no_attrs(origin: str) -> Mapping[str, Any]:
    return {}


@dataclass
class HostApp:
    """Everything a scheduling shell needs to run an app it knows nothing about."""

    graph: FeatGraph
    build_runtime: Callable[[ShellServices], Runtime]

    routes: Callable[[], Mapping[str, tuple[str, dict]]] = dict
    """command name -> (pool, broker message options). Unlisted commands go to
    the default pool. Where the route table COMES from — a manifest, a config
    file, a constant — is the app's business, not a shell's."""

    otel: Any = field(default_factory=NullOtel)

    origin_for: Callable[[Any, Any], str | None] = _no_origin
    """(current_event, command_being_dispatched) -> correlation string, or None
    for the default. Lets an app thread its own causality (e.g. a tool call's
    identity) through a dispatch without the shell knowing those names."""

    on_command_done: Callable[..., Any] = _no_hook
    """(origin, status, detail, emitted) after a command finishes — the app's
    place to close out whatever *origin* referred to.

    Returning TRUTHY on a failure means "handled": the app turned the failure
    into a fact, so the shell must not also let the broker retry the side
    effect. A falsy return re-raises, keeping at-least-once delivery."""

    span_attrs: Callable[[str], Mapping[str, Any]] = _no_attrs
    """origin -> extra tracing attributes. Whatever an app encodes in an
    origin string is the app's to decode, but a shell that simply dropped it
    would make traces unsearchable by the app's own identifiers — so the
    decoding gets a door rather than being deleted."""

    service_name: str = "dizzy-worker"

    build_procedure_runtime: Callable[[ShellServices], Runtime] | None = None
    """A runtime that only needs to RUN PROCEDURES: environment hydration and
    the procedure runners, with no store, no read-model writer and no command
    queue — a pool child appends nothing and dispatches nothing.

    ``None`` falls back to :attr:`build_runtime`, which is what lets a pool lane
    run against every host that exists today without one of them changing a
    line — but the fallback is not free. The child builds the app's FULL
    runtime: with the generated wiring that means an ``EventStore`` on the path
    the parent's own store resolves to (inherited across the spawn), so the
    child OPENS the host's log and holds a connection to it for the worker's
    lifetime. What it cannot do is write it: a child stamps itself
    (:func:`mark_pool_child`) before the app builds anything, and a store built
    in a marked process opens the file read-only — so the DAG schema's DDL,
    which is a write transaction taken merely by constructing a store, does not
    run against the host's log either. The events still go home to the one
    writer. A host that pools anything owes itself this builder rather than the
    fallback — it opens nothing at all — and the generator owes it an emitted
    one, which it does not emit yet."""

    def procedure_runtime(self, services: ShellServices) -> Runtime:
        """The runtime a pool child builds: procedure-only where the app said
        how, otherwise the full one."""
        return (self.build_procedure_runtime or self.build_runtime)(services)

    @staticmethod
    def resolve(spec: str | None = None) -> HostApp:
        """Load the app manifest named by *spec* or ``$DIZZY_HOST_APP``.

        Spec form is ``module:attr``; *attr* may be a ``HostApp`` or a
        zero-argument callable returning one (the usual choice — it defers the
        app's imports to worker-boot time).
        """
        spec = spec or os.environ.get("DIZZY_HOST_APP") or ""
        if ":" not in spec:
            raise RuntimeError(
                f"$DIZZY_HOST_APP={spec!r} is not a 'module:attr' spec — set it "
                f"to a module path and the name of a HostApp (or of a callable "
                f"returning one)"
                if spec
                else "no host app: set $DIZZY_HOST_APP to 'module:attr' naming a "
                "HostApp (or a callable returning one)"
            )
        module_name, _, attr = spec.partition(":")
        # Every failure below names the variable: an operator reading a systemd
        # journal sees a bare ImportError otherwise, with nothing to act on.
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise RuntimeError(
                f"$DIZZY_HOST_APP={spec!r}: cannot import {module_name!r} "
                f"({exc}) — is it on sys.path from this process's cwd?"
            ) from exc
        try:
            obj = getattr(module, attr)
        except AttributeError as exc:
            raise RuntimeError(
                f"$DIZZY_HOST_APP={spec!r}: {module_name!r} has no {attr!r}"
            ) from exc
        try:
            app = obj() if callable(obj) and not isinstance(obj, HostApp) else obj
        except Exception as exc:
            raise RuntimeError(
                f"$DIZZY_HOST_APP={spec!r}: building the HostApp raised {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(app, HostApp):
            raise TypeError(
                f"$DIZZY_HOST_APP={spec!r} resolved to {type(app).__name__}, not HostApp"
            )
        return app


def null_app(
    build_runtime: Callable[[ShellServices], Runtime], feat_path: str | None = None
) -> HostApp:
    """The minimal HostApp: a feat file and a way to build the engine."""
    return HostApp(graph=default_graph(feat_path), build_runtime=build_runtime)
