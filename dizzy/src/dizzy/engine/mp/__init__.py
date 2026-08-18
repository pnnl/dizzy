"""The multiprocess engine's scheduling shell — Dramatiq/Redis workers, pool-routed.

``dizzy.engine.mp`` — a scheduling shell around an app it knows nothing about.
Install it with the extra that carries its broker: ``pip install dizzy[mp]``.

Same library, different engine: the app's ``build_engine`` is reused
verbatim; the only difference is what's passed in — a command queue whose
``put()`` publishes to the broker instead of an in-proc queue, and telemetry
sinks that publish to Redis pub/sub instead of the in-proc hub. Every
command — policy cascades included — flows through the broker. At-least-once
delivery stands (Dramatiq redelivers on worker death); the confluent
projections absorb the duplicates.

**Nothing in this file names a command, an event, or an environment field.**
Everything app-specific arrives through ``dizzy.engine.ports.HostApp``, which
the worker resolves from ``$DIZZY_HOST_APP`` (``module:attr``) at boot:

  * the app's topology is a ``FeatGraph`` — the ``.feat.yaml`` read, so
    ``run_command`` looks its command class up in the DECLARED graph rather
    than a hand-maintained dict;
  * ``build_runtime`` builds this process's engine (the app's wiring, its
    env hydration, its telemetry sinks — a sink that must cross the process
    boundary is simply one the app closed over ``publish`` with);
  * ``routes`` resolves command → (pool, message options);
  * ``origin_for`` / ``on_command_done`` carry the app's own causality
    (e.g. tool-call identity) across a dispatch without this shell knowing
    what a tool call is;
  * ``otel`` supplies tracing, defaulting to a no-op.

Pool routing: the app's ``routes()`` shapes the fleet — every dispatch
resolves to a pool and lands on that pool's queue (a pool's ``time_limit_ms``
rides the message as its dramatiq option). Producers can enqueue to any pool
queue; CONSUMPTION is what's capability-gated — a worker process serves the
pools named in ``$DIZZY_POOLS`` (default ``default``, declared at import so
``--queues`` can select them). Start non-default fleets with
``--queues <pool>``, after checking the node can actually serve it. A bare
``dramatiq dizzy.engine.mp`` consumes only the default queue — the uniform
fleet, unchanged.

The Redis TELEMETRY_CHANNEL carries records discriminated by ``kind``; this
shell emits two of them and the app is free to publish its own:

  * engine bus records (kind: procedure|projection|policy|event|telemetry) —
    self-observation, landing in the server's BUS ring;
  * ``{"kind": "job", "id", "status", "error"?}`` — broker-job lifecycle, so
    the server's command-queue ledger (and its SSE tab) tracks worker runs;
  * ``{"kind": "worker", ...}`` — worker logs, which ARE telemetry here.

Run a worker fleet (after ``just redis-up``):

    dramatiq dizzy.engine.mp --processes 4 --threads 1

NOTE: run workers with ``--threads 1`` — the per-process emitted-event
collection assumes one command in flight per process.

Dispatch from any process:

    from dizzy.engine.mp import dispatch_by_name
    dispatch_by_name("create_log_entry", {"body": "hi", ...})

Config: $DIZZY_HOST_APP (required — the app manifest), $DIZZY_REDIS_URL
(default redis://localhost:6379/0), $DIZZY_POOLS (pools this worker serves).
"""

from __future__ import annotations

import json
import os

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from opentelemetry.trace import SpanKind, Status, StatusCode

from dizzy.engine.ports import HostApp, ShellServices
from dizzy.engine.registry import snake_case

REDIS_URL = os.environ.get("DIZZY_REDIS_URL", "redis://localhost:6379/0")
TELEMETRY_CHANNEL = "dizzy.telemetry"

dramatiq.set_broker(RedisBroker(url=REDIS_URL))

# The pools THIS process serves (worker side: the dramatiq CLI only consumes
# declared queues, so declare them before it looks). Producers don't need
# declaration — enqueue writes to the message's queue_name regardless.
for _pool in os.environ.get("DIZZY_POOLS", "default").split(","):
    if _pool.strip():
        dramatiq.get_broker().declare_queue(_pool.strip())


# ── The app manifest — this shell's only door to app knowledge ──────────────

_app: HostApp | None = None


def app() -> HostApp:
    """Resolve $DIZZY_HOST_APP once per process. Lazy on purpose: a producer
    that only enqueues still pays for the app's imports otherwise, and the
    dramatiq CLI imports this module before the app's sys.path is settled."""
    global _app
    if _app is None:
        _app = HostApp.resolve()
    return _app


def set_app(host_app: HostApp | None) -> None:
    """Inject the manifest directly (tests, embedding hosts)."""
    global _app, _routes
    _app, _routes = host_app, None


def _otel():
    return app().otel


# command name -> (pool, dramatiq message options). Commands the app does not
# route fall back to DEFAULT_ROUTE rather than living in the table under a
# None key, which no mapping type can honestly describe.
DEFAULT_ROUTE: tuple[str, dict] = ("default", {})

_routes: dict[str, tuple[str, dict]] | None = None


def send_routed(
    command_name: str, payload_json: str, origin: str = "", job_id: int | None = None
) -> None:
    """Every broker dispatch funnels here: resolve the command's pool from the
    app's route table and enqueue on that pool's queue with its message
    options. Cached — pool wiring is startup config, a manifest edit means
    restart."""
    global _routes
    if _routes is None:
        _routes = dict(app().routes())
    pool, options = _routes.get(command_name) or DEFAULT_ROUTE
    # Trace context rides the message as an extra actor ARG (not a dramatiq
    # option: options are validated against middleware-known names). The
    # producer span parents the worker's consumer span, so policy cascades —
    # which re-enter here from inside a running consumer span — chain into one
    # trace tree per root dispatch.
    otel = _otel()
    with otel.tracer().start_as_current_span(
        f"enqueue {command_name}",
        kind=SpanKind.PRODUCER,
        attributes={"dizzy.command": command_name, "dizzy.pool": pool, "dizzy.origin": origin},
    ):
        message = run_command.message_with_options(
            args=(command_name, payload_json, origin, job_id, otel.inject()), **options
        )
        if pool != message.queue_name:
            message = message.copy(queue_name=pool)
        dramatiq.get_broker().enqueue(message)


_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        import redis as redis_mod

        _redis_client = redis_mod.Redis.from_url(REDIS_URL)
    return _redis_client


def publish(record: dict) -> None:
    """One channel for all worker→server telemetry; never load-bearing.
    Records published inside a recording span carry its trace_id, so the
    server's BUS ring (and the ledger's job rows) can link back to traces."""
    try:
        tid = _otel().trace_id_hex()
        if tid and "trace_id" not in record:
            record["trace_id"] = tid
    except Exception:
        pass
    try:
        _redis().publish(TELEMETRY_CHANNEL, json.dumps(record, default=str))
    except Exception:
        pass


def _log(msg: str, name: str = "log", outcome: str = "") -> None:
    """Worker logs ARE telemetry: publish a bus record so they land in the
    server's ring and the UI logs tab (worker chip), instead of stderr.
    Two reasons stderr lost: it's invisible in the UI, and under dramatiq
    it's a multiprocessing pipe back to the parent that can BREAK — a bare
    print then crashes (and dead-letters!) whatever command this worker
    claimed (found by the 2026-07-09 soak). publish() is never load-bearing.

    Also mirrored through stdlib logging: an OTLP LoggingHandler on the root
    logger sends the same record to the logs backend (with trace correlation).
    The pipe handler it ALSO reaches is armored below (_safe_pipe_write)."""
    publish(
        {
            "kind": "worker",
            "name": name,
            "trigger": f"pid {os.getpid()}",
            "duration_ms": None,
            "outcome": outcome,
            "detail": msg,
        }
    )
    try:
        import logging

        (
            logging.getLogger("dizzy-worker").error
            if outcome == "error"
            else logging.getLogger("dizzy-worker").info
        )("%s: %s", name, msg)
    except Exception:
        pass


# The same broken-pipe failure reaches us through DRAMATIQ'S OWN logging
# (every logger in a worker writes through compat.StreamablePipe), so guarding
# our prints isn't enough — armor the pipe wrapper itself. A worker whose
# parent stopped reading its log pipe must keep executing commands silently,
# not dead-letter them.
try:
    from dramatiq import compat as _dramatiq_compat

    _orig_pipe_write = _dramatiq_compat.StreamablePipe.write

    def _safe_pipe_write(self, s):
        try:
            return _orig_pipe_write(self, s)
        except OSError:
            return len(s or "")

    _dramatiq_compat.StreamablePipe.write = _safe_pipe_write  # noqa: B010  # ty: ignore[invalid-assignment]
except Exception:  # pragma: no cover — future dramatiq refactor: fail open
    pass


class BrokerCommandQueue:
    """The CommandQueue port: a policy's dispatch becomes a broker message.

    Correlation on a dispatch is the APP's to decide: ``origin_for`` sees the
    event currently draining and the command going out, and returns whatever
    string should ride the message (the st shell's ``_ToolOriginQueue`` rule,
    expressed app-side).
    """

    def put(self, command, origin: str = "policy") -> None:
        current = getattr(_runtime_engine(), "current_event", None)
        if current is not None:
            origin = app().origin_for(current, command) or origin
        send_routed(snake_case(type(command).__name__), command.model_dump_json(), origin, None)

    def qsize(self) -> int:
        return 0  # broker-side depth is Redis's business, not the engine's


_runtime = None
_emitted: list = []  # (name, event) of the command currently running


def _runtime_engine():
    return _runtime.engine if _runtime is not None else None


def _collect(name: str, event) -> None:
    """The shell's observer: remember what the running command emitted, so
    ``on_command_done`` can describe the work. Handed to the app through
    ShellServices, which must chain it into whatever observer it installs —
    the engine accepts only one."""
    _emitted.append((name, event))


def _get_runtime():
    """Per-process singleton: each worker process builds the app's engine once,
    then re-hydrates its mutable environment before every command (the server
    can change secrets while workers run)."""
    global _runtime
    if _runtime is None:
        _runtime = app().build_runtime(
            ShellServices(
                command_queue=BrokerCommandQueue(),
                publish=publish,
                pool=os.environ.get("DIZZY_POOLS", "default"),
                observer=_collect,
            )
        )
    else:
        _runtime.refresh()
    return _runtime


def _span_attrs(command_name: str, origin: str, job_id: int | None) -> dict:
    """Correlation as span attributes: traces become searchable by the same
    keys the app uses — the ledger id here, and whatever the app decodes out
    of its own origin string through ``span_attrs``."""
    attrs = {
        "dizzy.command": command_name,
        "dizzy.origin": origin,
        "dizzy.pool": os.environ.get("DIZZY_POOLS", "default"),
    }
    if job_id is not None:
        attrs["dizzy.job_id"] = job_id
    try:
        attrs.update(app().span_attrs(origin))
    except Exception:  # tracing is never load-bearing
        pass
    return attrs


@dramatiq.actor(max_retries=3, time_limit=600_000)
def run_command(
    command_name: str,
    payload_json: str,
    origin: str = "",
    job_id: int | None = None,
    carrier: dict | None = None,
) -> None:
    """The uniform unit of work: claim → build engine → run_command → ack.

    ``origin``/``job_id`` are correlation, not routing: origin carries whatever
    identity the app's ``origin_for`` encoded, job_id ties the run back to the
    server's ledger row. ``carrier`` is the producer's W3C trace context.
    """
    host = app()
    otel = host.otel
    # Per worker PROCESS, after the fork — batch-export threads must never be
    # inherited from the dramatiq parent.
    otel.init(f"{host.service_name}-{os.environ.get('DIZZY_POOLS', 'default')}")

    command_cls = host.graph.command_class(command_name)
    with otel.tracer().start_as_current_span(
        f"run {command_name}",
        context=otel.extract(carrier),
        kind=SpanKind.CONSUMER,
        record_exception=True,
        attributes=_span_attrs(command_name, origin, job_id),
    ) as span:
        runtime = _get_runtime()
        _log(command_name, name="run_command")
        _emitted.clear()
        if job_id is not None:
            publish({"kind": "job", "id": job_id, "status": "running"})
        try:
            runtime.engine.run_command(command_cls(**json.loads(payload_json)))
        except Exception as exc:
            runtime.engine._events.clear()  # a failed run must not leak its
            if runtime.session is not None:  # un-appended events into the NEXT
                runtime.session.rollback()  # command on this process (st builds
                # a fresh engine per job; this
                # singleton must reset instead)
            import traceback

            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            _log(f"{type(exc).__name__}: {exc}", name=command_name, outcome="error")
            if job_id is not None:
                publish(
                    {
                        "kind": "job",
                        "id": job_id,
                        "status": "error",
                        "error": traceback.format_exc()[:4000],
                    }
                )
            # The app decides whether this origin means "record the failure as
            # a fact instead of retrying" — if it handled it, don't ALSO let
            # the broker retry the side effect.
            if host.on_command_done(
                origin, "error", f"{type(exc).__name__}: {exc}", list(_emitted)
            ):
                return
            raise
        if job_id is not None:
            publish({"kind": "job", "id": job_id, "status": "done"})
        host.on_command_done(origin, "ok", "", list(_emitted))


def dispatch_by_name(command_name: str, payload: dict) -> None:
    """Producer-side dispatch: enqueue without building the library."""
    send_routed(command_name, json.dumps(payload), "", None)
