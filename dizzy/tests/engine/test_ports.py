"""The ports — the seam through which an app reaches a scheduling shell.

What matters here is that a shell needs exactly two things from an app, that
every other hook is inert by default, and that the one obligation running the
other way (chaining the shell's observer) is stated and testable. That last
one is not pedantry: dropping it degrades a shell silently rather than
failing, which is how it escaped a green suite once already.
"""

from __future__ import annotations

import pytest
from dizzy.engine.ports import (
    CommandQueue,
    HostApp,
    NullOtel,
    Runtime,
    ShellServices,
    TelemetryBus,
    chain_observers,
    null_app,
)
from dizzy.engine.registry import FeatGraph


@pytest.fixture
def app_graph(write_feat) -> FeatGraph:
    return FeatGraph.load(write_feat("commands:\n  record_recipe: a\n"))


# ── A minimal app supplies two things ──────────────────────────────────────

def test_the_defaults_are_all_inert(app_graph):
    app = HostApp(graph=app_graph, build_runtime=lambda s: Runtime(engine=None))
    assert app.routes() == {}
    assert app.origin_for(object(), object()) is None
    assert not app.on_command_done("policy", "ok", "", [])
    assert app.span_attrs("anything") == {}
    assert app.service_name == "dizzy-worker"


def test_tracing_is_optional(app_graph):
    """A shell must run with tracing switched off, so the null provider has to
    satisfy the whole surface a shell touches."""
    app = HostApp(graph=app_graph, build_runtime=lambda s: Runtime(engine=None))
    assert isinstance(app.otel, NullOtel)
    app.otel.init("svc")
    with app.otel.tracer().start_as_current_span("noop", kind=None) as span:
        span.set_status("ignored")
    assert app.otel.inject() == {}
    assert app.otel.extract({"traceparent": "x"}) is None
    assert app.otel.trace_id_hex() == ""


def test_null_app_builds_from_a_feat_path(write_feat, monkeypatch):
    feat = write_feat("commands:\n  record_recipe: a\n")
    monkeypatch.setenv("DIZZY_FEAT_PATH", str(feat))
    app = null_app(lambda s: Runtime(engine="e"))
    assert app.graph.feat_path == feat
    assert app.build_runtime(ShellServices(command_queue=None,
                                           publish=lambda r: None)).engine == "e"


# ── The obligation that runs the other way ─────────────────────────────────

def test_chain_observers_calls_every_observer_in_order():
    seen: list[str] = []
    observer = chain_observers(lambda n, e: seen.append(f"first:{n}"),
                               lambda n, e: seen.append(f"second:{n}"))
    observer("recipe_recorded", object())
    assert seen == ["first:recipe_recorded", "second:recipe_recorded"]


def test_shell_services_carry_an_observer_the_app_must_chain():
    """The engine takes ONE observer. A shell hands its collector over here;
    an app that installs its own without chaining unplugs the shell."""
    collected: list[tuple[str, object]] = []
    published: list[dict] = []
    services = ShellServices(command_queue=object(), publish=published.append,
                             pool="ml", observer=lambda n, e: collected.append((n, e)))

    # What a well-behaved build_runtime does with it.
    app_observer = chain_observers(services.observer,
                                   lambda n, e: services.publish({"kind": n}))
    event = object()
    app_observer("recipe_recorded", event)

    assert collected == [("recipe_recorded", event)]
    assert published == [{"kind": "recipe_recorded"}]
    assert services.pool == "ml"


def test_the_default_observer_is_a_no_op():
    """A shell that doesn't collect anything still satisfies the contract."""
    services = ShellServices(command_queue=None, publish=lambda r: None)
    assert services.observer("anything", object()) is None


def test_runtime_defaults_to_no_session_and_a_no_op_refresh():
    runtime = Runtime(engine="e")
    assert runtime.session is None
    assert runtime.refresh() is None


# ── Resolving an app from the environment ──────────────────────────────────

def test_resolve_accepts_a_hostapp_or_a_factory(monkeypatch, app_graph, tmp_path):
    module = tmp_path / "someapp.py"
    module.write_text(
        "from dizzy.engine.ports import HostApp, Runtime\n"
        "from dizzy.engine.registry import FeatGraph\n"
        f"GRAPH = FeatGraph.load({str(app_graph.feat_path)!r})\n"
        "APP = HostApp(graph=GRAPH, build_runtime=lambda s: Runtime(engine='direct'))\n"
        "def build():\n"
        "    return HostApp(graph=GRAPH, build_runtime=lambda s: Runtime(engine='made'))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    services = ShellServices(command_queue=None, publish=lambda r: None)
    direct = HostApp.resolve("someapp:APP")
    assert direct.build_runtime(services).engine == "direct"
    made = HostApp.resolve("someapp:build")
    assert made.build_runtime(services).engine == "made"


def test_resolve_reads_the_environment(monkeypatch, app_graph, tmp_path):
    module = tmp_path / "envapp.py"
    module.write_text(
        "from dizzy.engine.ports import HostApp, Runtime\n"
        "from dizzy.engine.registry import FeatGraph\n"
        f"APP = HostApp(graph=FeatGraph.load({str(app_graph.feat_path)!r}),\n"
        "              build_runtime=lambda s: Runtime(engine='e'))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DIZZY_HOST_APP", "envapp:APP")
    assert HostApp.resolve().graph.feat_path == app_graph.feat_path


@pytest.mark.parametrize("spec,match", [
    ("", "no host app"),
    ("envapp", "not a 'module:attr' spec"),
    ("no_such_module:APP", "cannot import"),
])
def test_resolve_failures_name_the_variable(monkeypatch, spec, match):
    """An operator reading a systemd journal has only what the error says."""
    monkeypatch.delenv("DIZZY_HOST_APP", raising=False)
    with pytest.raises(RuntimeError, match=match):
        HostApp.resolve(spec)


def test_resolve_failures_inside_the_app_are_attributed(monkeypatch, tmp_path):
    module = tmp_path / "brokenapp.py"
    module.write_text(
        "NOT_AN_APP = 42\n"
        "def raises():\n"
        "    raise ValueError('secrets not loaded')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError, match="has no 'missing'"):
        HostApp.resolve("brokenapp:missing")
    with pytest.raises(RuntimeError, match="building the HostApp raised ValueError"):
        HostApp.resolve("brokenapp:raises")
    with pytest.raises(TypeError, match="resolved to int, not HostApp"):
        HostApp.resolve("brokenapp:NOT_AN_APP")


# ── Both shells' queues are the same port ──────────────────────────────────

def test_the_st_queue_and_bus_satisfy_the_ports(tmp_path):
    from dizzy.engine.st import DurableCommandQueue
    from dizzy.engine.st import TelemetryBus as StBus

    queue = DurableCommandQueue(registry={}, path=tmp_path / "q.db")
    try:
        assert isinstance(queue, CommandQueue)
        assert isinstance(StBus(), TelemetryBus)
    finally:
        queue.close()


def test_the_mp_queue_satisfies_the_same_port():
    """The shells differ in who holds the queue, not in what the engine sees."""
    pytest.importorskip("dramatiq")
    from dizzy.engine.mp import BrokerCommandQueue

    assert isinstance(BrokerCommandQueue(), CommandQueue)
