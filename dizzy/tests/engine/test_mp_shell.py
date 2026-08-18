"""The multiprocess shell — routing, the runtime seam, and correlation.

No broker: what is under test is the shell's own decisions (which pool a
dispatch lands on, what it hands the app, what it does when a command fails),
not Dramatiq's delivery. The app is a stub, which is the point — the shell
must run one it has never heard of.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("dramatiq")

from dizzy.engine.ports import HostApp, Runtime, ShellServices  # noqa: E402
from dizzy.engine.registry import FeatGraph  # noqa: E402


class Command:
    """A stand-in for a generated command class."""

    def __init__(self, **payload):
        self.payload = payload

    def model_dump_json(self) -> str:
        return json.dumps(self.payload)


class RecordRecipe(Command):
    pass


class RateRecipe(Command):
    pass


class StubEngine:
    def __init__(self):
        self.ran = []
        self._events = []
        self.current_event = None

    def run_command(self, command):
        self.ran.append(command)


@pytest.fixture
def mp(monkeypatch, write_feat, def_package):
    """The shell module with a stub broker and a stub app installed."""
    import dizzy.engine.mp as shell

    sent = []

    class StubBroker:
        def enqueue(self, message):
            sent.append(message)

        def declare_queue(self, name):
            pass

    monkeypatch.setattr(shell.dramatiq, "get_broker", StubBroker)
    monkeypatch.setattr(shell, "_routes", None)
    monkeypatch.setattr(shell, "_runtime", None)
    monkeypatch.setattr(shell, "publish", lambda record: None)

    pkg = def_package(commands=["RecordRecipe", "RateRecipe"])
    feat = write_feat("commands:\n  record_recipe: a\n  rate_recipe: b\n")
    graph = FeatGraph.load(feat, def_package=pkg)

    state: dict[str, Any] = {"services": None, "engine": StubEngine(), "done": []}

    def build_runtime(services: ShellServices) -> Runtime:
        state["services"] = services
        return Runtime(engine=state["engine"])

    def make_app(**overrides) -> HostApp:
        kwargs: dict[str, Any] = dict(graph=graph, build_runtime=build_runtime)
        kwargs.update(overrides)
        app = HostApp(**kwargs)
        shell.set_app(app)
        monkeypatch.setattr(shell, "_routes", None)
        return app

    make_app()
    state.update(shell=shell, sent=sent, graph=graph, make_app=make_app)
    yield state
    shell.set_app(None)


# ── Routing ─────────────────────────────────────────────────────────────────

def test_an_unrouted_command_goes_to_the_default_pool(mp):
    mp["shell"].send_routed("record_recipe", "{}")
    (message,) = mp["sent"]
    assert message.queue_name == "default"
    assert message.actor_name == "run_command"
    assert "time_limit" not in message.options
    assert message.args[:4] == ("record_recipe", "{}", "", None)


def test_a_routed_command_lands_on_its_pool_with_its_options(mp):
    mp["make_app"](routes=lambda: {"rate_recipe": ("ml", {"time_limit": 5000})})
    mp["shell"].send_routed("rate_recipe", "{}", "policy", 7)
    mp["shell"].send_routed("record_recipe", "{}")

    ml, default = mp["sent"]
    assert ml.queue_name == "ml" and ml.options["time_limit"] == 5000
    assert ml.args[:4] == ("rate_recipe", "{}", "policy", 7)
    assert default.queue_name == "default" and "time_limit" not in default.options


def test_the_route_table_is_read_once(mp):
    calls = {"n": 0}

    def routes():
        calls["n"] += 1
        return {}

    mp["make_app"](routes=routes)
    mp["shell"].send_routed("record_recipe", "{}")
    mp["shell"].send_routed("rate_recipe", "{}")
    assert calls["n"] == 1, "pool wiring is startup config, not per-dispatch"


def test_dispatch_by_name_enqueues_without_building_the_library(mp):
    mp["shell"].dispatch_by_name("record_recipe", {"title": "soup"})
    (message,) = mp["sent"]
    assert json.loads(message.args[1]) == {"title": "soup"}
    assert mp["services"] is None, "a producer must not build the engine"


# ── The runtime seam ────────────────────────────────────────────────────────

def test_the_shell_hands_its_collector_to_the_app(mp):
    """The engine takes ONE observer, so the shell's collector reaches it only
    if the app chains it. If the shell fails to offer one, there is nothing to
    chain and every result silently reports no events."""
    mp["shell"]._get_runtime()
    services = mp["services"]
    assert services is not None

    mp["shell"]._emitted.clear()
    services.observer("recipe_recorded", "the-event")
    assert mp["shell"]._emitted == [("recipe_recorded", "the-event")]


def test_the_runtime_is_built_once_and_refreshed_thereafter(mp):
    refreshes = {"n": 0}

    def build_runtime(services):
        return Runtime(engine=mp["engine"],
                       refresh=lambda: refreshes.__setitem__("n", refreshes["n"] + 1))

    mp["make_app"](build_runtime=build_runtime)
    mp["shell"]._get_runtime()
    mp["shell"]._get_runtime()
    mp["shell"]._get_runtime()
    assert refreshes["n"] == 2, "built once, then re-hydrated per command"


def test_services_carry_the_pool_this_worker_serves(mp, monkeypatch):
    monkeypatch.setenv("DIZZY_POOLS", "ml")
    mp["shell"]._get_runtime()
    assert mp["services"].pool == "ml"


# ── Correlation ─────────────────────────────────────────────────────────────

def test_a_dispatch_carries_the_origin_the_app_decides(mp):
    """The shell knows nothing about what the origin means — only that the app
    may want one attached while a particular event is draining."""
    mp["make_app"](origin_for=lambda event, command: f"caused-by:{event}")
    mp["shell"]._get_runtime()
    mp["engine"].current_event = "tool_call_requested"

    mp["shell"].BrokerCommandQueue().put(RecordRecipe(title="soup"))
    (message,) = mp["sent"]
    assert message.args[2] == "caused-by:tool_call_requested"


def test_no_current_event_means_the_default_origin(mp):
    mp["make_app"](origin_for=lambda event, command: "never")
    mp["shell"]._get_runtime()
    mp["engine"].current_event = None
    mp["shell"].BrokerCommandQueue().put(RecordRecipe(), origin="policy")
    assert mp["sent"][0].args[2] == "policy"


def test_an_app_that_declines_to_correlate_keeps_the_default(mp):
    mp["shell"]._get_runtime()
    mp["engine"].current_event = "some_event"
    mp["shell"].BrokerCommandQueue().put(RecordRecipe(), origin="policy")
    assert mp["sent"][0].args[2] == "policy"


def test_span_attributes_merge_the_apps_decoding(mp):
    mp["make_app"](span_attrs=lambda origin: {"dizzy.tool": origin.split(":")[-1]})
    attrs = mp["shell"]._span_attrs("record_recipe", "tool:run_query", 7)
    assert attrs["dizzy.command"] == "record_recipe"
    assert attrs["dizzy.job_id"] == 7
    assert attrs["dizzy.tool"] == "run_query"


def test_a_raising_span_attrs_hook_cannot_break_a_dispatch(mp):
    """Tracing is never load-bearing."""
    def boom(origin):
        raise RuntimeError("no")

    mp["make_app"](span_attrs=boom)
    assert mp["shell"]._span_attrs("record_recipe", "x", None)["dizzy.command"] \
        == "record_recipe"


# ── Running a command ───────────────────────────────────────────────────────

def test_run_command_looks_the_class_up_in_the_declared_graph(mp):
    mp["shell"].run_command("record_recipe", "{}")
    assert len(mp["engine"].ran) == 1


def test_an_undeclared_command_is_refused(mp):
    with pytest.raises(KeyError, match="not declared"):
        mp["shell"].run_command("no_such_command", "{}")


def test_a_failure_the_app_handles_is_not_retried(mp):
    """The app turned the failure into a fact, so the broker must not also
    retry the side effect."""
    class Boom(StubEngine):
        def run_command(self, command):
            raise ValueError("nope")

    mp["engine"] = Boom()
    mp["make_app"](build_runtime=lambda s: Runtime(engine=mp["engine"]),
                   on_command_done=lambda *a: True)
    mp["shell"].run_command("record_recipe", "{}")     # must not raise


def test_a_failure_the_app_declines_keeps_at_least_once_delivery(mp):
    class Boom(StubEngine):
        def run_command(self, command):
            raise ValueError("nope")

    mp["engine"] = Boom()
    mp["make_app"](build_runtime=lambda s: Runtime(engine=mp["engine"]),
                   on_command_done=lambda *a: False)
    with pytest.raises(ValueError, match="nope"):
        mp["shell"].run_command("record_recipe", "{}")


def test_a_failed_command_leaves_nothing_behind_for_the_next(mp):
    """This worker's engine is a per-process singleton, so un-appended events
    and a dirty session must be cleared — st builds a fresh engine per job and
    gets this for free."""
    rolled_back = {"n": 0}

    class Boom(StubEngine):
        def run_command(self, command):
            self._events.append("half-done")
            raise ValueError("nope")

    engine = Boom()

    class Session:
        def rollback(self):
            rolled_back["n"] += 1

    mp["make_app"](build_runtime=lambda s: Runtime(engine=engine, session=Session()),
                   on_command_done=lambda *a: True)
    mp["shell"].run_command("record_recipe", "{}")
    assert engine._events == []
    assert rolled_back["n"] == 1
