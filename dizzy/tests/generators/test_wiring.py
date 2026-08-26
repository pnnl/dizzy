"""Snapshot + behavioural tests for the wiring generator.

The snapshots pin the emitted module; the assertions below pin the properties
that make it *correct* rather than merely stable — a snapshot happily records a
wiring that binds a policy to ``emit_event``, so the things that would actually
break a running feature are asserted by name.
"""

import ast

import pytest
from dizzy.feat_schema import FeatureDefinition
from dizzy.generators.lib_python_uv import _dizzy_source_entry
from dizzy.generators.wiring import (
    feat_name,
    render_wiring,
    render_wiring_pyproject_toml,
)
from dizzy.topology_loader import validate_topology
from dizzy.topology_schema import Topology
from linkml_runtime.utils.formatutils import camelcase
from syrupy.assertion import SnapshotAssertion


def test_render_wiring_snapshot(recipe_feat, snapshot: SnapshotAssertion):
    assert render_wiring(recipe_feat, "recipe.feat.yaml") == snapshot


def test_render_wiring_with_env_and_telemetry_snapshot(agent_feat, snapshot: SnapshotAssertion):
    assert render_wiring(agent_feat, "agent.feat.yaml") == snapshot


def test_render_wiring_pyproject_snapshot(recipe_feat, snapshot: SnapshotAssertion):
    assert render_wiring_pyproject_toml(recipe_feat) == snapshot


def test_feat_name_comes_from_the_file():
    assert feat_name("recipes.feat.yaml") == "recipes"


# ── The emitted module has to be real Python, and mean the right thing ──────


@pytest.mark.parametrize("fixture", ["recipe_feat", "partial_feat", "agent_feat"])
def test_the_emitted_wiring_parses(fixture, request):
    """Cheap, and catches every quoting/indentation slip a template can make."""
    feat = request.getfixturevalue(fixture)
    ast.parse(render_wiring(feat, "x.feat.yaml"))


def test_a_procedures_emitters_go_to_the_engine_not_another_element(recipe_feat):
    """The reactivity loop must cross the engine. A procedure that called a
    projection directly would skip the event store and the commit boundary."""
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    proc = recipe_feat.procedures[0]
    for event in proc.emits or []:
        assert f"{event}=engine.emit_event" in out


def test_a_policys_emitters_dispatch_commands_rather_than_emit_events(recipe_feat):
    """A policy's output is an intent, not a fact — it goes on the command
    queue, and a dispatched command must NOT recurse into a procedure."""
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    policies = recipe_feat.policies or []
    assert policies, "fixture must declare a policy for this to mean anything"
    for policy in policies:
        for command in policy.emits or []:
            assert f"{command}=engine.dispatch_command" in out


def test_every_declared_element_is_registered(recipe_feat):
    """The guarantee the generator exists to provide: no element declared in the
    feat can be missing from the wiring, because both come from the same read."""
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    for proc in recipe_feat.procedures or []:
        assert f'name="{proc.name}"' in out
    for policy in recipe_feat.policies or []:
        assert f'name="{policy.name}"' in out
    for proj in recipe_feat.projections or []:
        assert f'"{proj.name}", resources.overrides.get("{proj.name}"' in out


def test_a_projection_registers_under_the_event_its_feat_entry_names(recipe_feat):
    from linkml_runtime.utils.formatutils import camelcase

    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    for proj in recipe_feat.projections or []:
        assert f"runners.setdefault({camelcase(proj.event)}, [])" in out


def test_every_element_can_be_overridden_by_name(recipe_feat):
    """The declared escape hatch. An app that has to specialize one binding must
    not have to fork the module to do it."""
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    elements = (
        list(recipe_feat.procedures or [])
        + list(recipe_feat.policies or [])
        + list(recipe_feat.projections or [])
        + list(recipe_feat.queries or [])
    )
    for element in elements:
        assert f'resources.overrides.get("{element.name}"' in out


def test_declared_queries_are_bound_and_handed_to_the_elements_that_asked(recipe_feat):
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    for element in list(recipe_feat.procedures or []) + list(recipe_feat.policies or []):
        for query in element.queries or []:
            assert f"{query}=queries.{query}" in out


def test_environment_and_telemetry_reach_the_context_when_declared(agent_feat):
    """These are the optional context fields; a wiring that dropped them would
    hand an element a context its own generated dataclass rejects."""
    out = render_wiring(agent_feat, "agent.feat.yaml")
    for element in list(agent_feat.procedures or []) + list(agent_feat.policies or []):
        for entry in element.environment or []:
            assert f"{entry}=resources.env.{entry}" in out
        for sink in element.telemetry or []:
            assert f"{sink}=resources.telemetry.{sink}" in out


def test_a_feature_with_no_policies_still_emits_a_valid_module(partial_feat):
    """partial.feat.yaml has no events/policies/projections at all."""
    out = render_wiring(partial_feat, "partial.feat.yaml")
    ast.parse(out)
    assert "def build_engine(" in out
    assert "return {}" in out  # no projections to fold


def test_the_wiring_package_depends_on_dizzy_and_on_every_element(recipe_feat):
    """It is the only generated package that needs the runtime: the elements
    import their contracts, the wiring imports the engine."""
    out = render_wiring_pyproject_toml(recipe_feat)
    assert '"dizzy",' in out
    for proc in recipe_feat.procedures or []:
        assert f'"procedure-{proc.name}",' in out
    for proj in recipe_feat.projections or []:
        assert f'"projection-{proj.name}",' in out


# ── Where DIZZY comes from ──────────────────────────────────────────────────
#
# DIZZY is not published to a package index, and the name there belongs to an
# unrelated project — so "resolve it however pip likes" is not an option. Every
# generated workspace names a source explicitly.


def test_the_default_source_is_the_canonical_repository():
    assert _dizzy_source_entry(None) == 'dizzy = { git = "https://github.com/PNNL/dizzy" }'


def test_a_path_becomes_an_editable_checkout():
    assert _dizzy_source_entry("../../../..") == (
        'dizzy = { path = "../../../..", editable = true }'
    )


@pytest.mark.parametrize(
    "spec",
    [
        "https://github.com/PNNL/dizzy",
        "git+https://github.com/PNNL/dizzy",
        "ssh://git@github.com/PNNL/dizzy",
    ],
)
def test_a_url_becomes_a_git_source(spec):
    entry = _dizzy_source_entry(spec)
    assert entry.startswith("dizzy = { git = ")
    assert "git+" not in entry  # uv wants the bare URL


def test_the_wiring_manifest_leaves_the_source_to_the_workspace_root(recipe_feat):
    """The package declares the dependency; the workspace says where it comes
    from. That keeps this manifest identical wherever the lib is shipped."""
    out = render_wiring_pyproject_toml(recipe_feat)
    assert '"dizzy",' in out
    assert "dizzy = {" not in out


# ── Placement: topology.yaml, emitted as data ──────────────────────────────

# The `isolation` topology from docs/explanation/topology.md, and a feature-file
# shaped to it: two paid, query-free procedures for the pool lane, one operator
# notification and one approval for the interactive level, and a projection plus a
# policy on the group that holds the lock. Written out here rather than fixtured
# because the assertions below pin its emitted table entry for entry — the point of
# the test is that a reader can check the table against the YAML by eye.
ISOLATION_FEAT = {
    "description": "labthing-shaped isolation feature",
    "procedures": [
        {"name": "draft_completion", "command": "request_completion"},
        {"name": "summarize_run", "command": "summarize_run"},
        {"name": "notify_operator", "command": "send_notification"},
        {"name": "approve_run", "command": "approve_run", "queries": ["get_run"]},
    ],
    "policies": [{"name": "notify_on_drafted", "event": "completion_drafted"}],
    "projections": [
        {"name": "run_log", "description": "d", "event": "completion_drafted", "model": "runs"}
    ],
    "models": [{"name": "runs"}],
    "queries": [{"name": "get_run", "description": "d", "model": "runs"}],
}

ISOLATION_TOPOLOGY = {
    "groups": [
        {"name": "fast", "include": [{"kind": "projections"}, {"kind": "policies"}]},
        {
            "name": "paid",
            "include": [{"kind": "procedures", "names": ["*_completion", "summarize_*"]}],
            "executor": "pool",
            "workers": 4,
        },
        {"name": "default", "catch_all": True},
    ],
    "priority": {"interactive": ["send_notification", "approve_*"]},
}


@pytest.fixture
def isolation() -> tuple[FeatureDefinition, Topology]:
    feat = FeatureDefinition.model_validate(ISOLATION_FEAT)
    topology = Topology.model_validate(ISOLATION_TOPOLOGY)
    # A table generated from an illegal topology would pin nothing worth pinning.
    assert validate_topology(topology, feat) == []
    return feat, topology


def _placement_block(module: str) -> str:
    """The emitted placement section, sliced out of the module."""
    return module[module.index("COMMAND_PLACEMENT") : module.index("def build_engine(")]


def test_no_topology_emits_no_placement_table(recipe_feat):
    """The default topology is one inline lane and all-batch, which is what a queue
    given no lane_of/priority_of already does — so a topology-free wiring must be the
    module it was before topology existed, not that behaviour spelled out."""
    out = render_wiring(recipe_feat, "recipe.feat.yaml")
    assert "COMMAND_PLACEMENT" not in out
    assert "LANES" not in out


def test_the_isolation_topology_pins_an_exact_placement_table(isolation):
    """Every entry, spelled out. Not a snapshot: the value of this table is that it
    can be read against the YAML above, and a snapshot records whatever it is told."""
    feat, topology = isolation
    block = _placement_block(render_wiring(feat, "labthing.feat.yaml", topology))
    assert (
        "COMMAND_PLACEMENT: dict[str, tuple[str, str]] = {\n"
        '    "ApproveRun": ("default", "interactive"),\n'
        '    "RequestCompletion": ("paid", "batch"),\n'
        '    "SendNotification": ("default", "interactive"),\n'
        '    "SummarizeRun": ("paid", "batch"),\n'
        "}\n"
    ) in block
    assert (
        "LANES: dict[str, tuple[str, int]] = {\n"
        '    "fast": ("inline", 1),\n'
        '    "paid": ("pool", 4),\n'
        '    "default": ("inline", 1),\n'
        "}\n"
    ) in block


def test_the_placement_keys_are_what_the_module_calls_a_command_class(isolation):
    """The table is looked up by ``type(command).__name__``, so its keys have to be
    the names the wiring's own imports bind — a mismatch is a silent miss, never an
    error, and every command would quietly land on the fallback lane."""
    feat, topology = isolation
    out = render_wiring(feat, "labthing.feat.yaml", topology)
    imported = {
        line.strip().rstrip(",")
        for line in out[out.index("from gen_def.pydantic.commands import (") :]
        .split(")")[0]
        .splitlines()[1:]
    }
    for command in {p.command for p in feat.procedures}:
        assert camelcase(command) in imported


def test_placement_runs_and_answers_for_a_command_it_never_heard_of(isolation):
    """The emitted helpers are executable data, not a template that only looks right."""
    feat, topology = isolation
    namespace: dict = {"Any": object}
    exec(_placement_block(render_wiring(feat, "labthing.feat.yaml", topology)), namespace)

    class SendNotification: ...

    class SummarizeRun: ...

    class Unknown: ...

    assert namespace["command_lane"](SendNotification()) == "default"
    assert namespace["command_priority"](SendNotification()) == 1
    assert namespace["command_lane"](SummarizeRun()) == "paid"
    assert namespace["command_priority"](SummarizeRun()) == 0
    # The fallback is not the literal string "default" — a topology need not declare
    # a group by that name — but the first lane served in the host process, so an
    # unplaced command runs somewhere rather than waiting on a lane with no worker.
    lane = namespace["command_lane"](Unknown())
    assert namespace["command_priority"](Unknown()) == 0
    assert namespace["LANES"][lane][0] == "inline"


def test_every_placed_lane_is_a_declared_group(isolation):
    """A command routed to a lane no worker serves never runs at all."""
    feat, topology = isolation
    namespace: dict = {"Any": object}
    exec(_placement_block(render_wiring(feat, "labthing.feat.yaml", topology)), namespace)
    lanes = set(namespace["LANES"])
    assert lanes == {g.name for g in topology.groups}
    assert {lane for lane, _ in namespace["COMMAND_PLACEMENT"].values()} <= lanes
    assert namespace["DEFAULT_PLACEMENT"][0] in lanes


def test_priority_is_a_property_of_the_command_type_not_of_its_procedure(isolation):
    """`approve_*` is matched against `command:` values. Its procedure is
    `approve_run` and its command is `approve_run` only by coincidence here, so the
    one that must NOT be interactive is the procedure whose command is named
    differently — `draft_completion` handles `request_completion`."""
    feat, topology = isolation
    namespace: dict = {"Any": object}
    exec(_placement_block(render_wiring(feat, "labthing.feat.yaml", topology)), namespace)
    assert namespace["COMMAND_PLACEMENT"]["RequestCompletion"][1] == "batch"
    assert "DraftCompletion" not in namespace["COMMAND_PLACEMENT"]


def test_an_all_pool_topology_falls_back_to_the_lane_it_actually_declares():
    """A topology of nothing but pool lanes is legal — a group of query-free
    procedures is exactly what `executor: pool` is for — so the fallback cannot
    promise an inline lane. It names the first declared group, and the emitted
    docstring says so rather than asserting a lane the topology never declared.
    """
    feat = FeatureDefinition.model_validate(
        {
            "description": "every procedure reads nothing",
            "procedures": [
                {"name": "draft_completion", "command": "request_completion"},
                {"name": "summarize_run", "command": "summarize_run"},
            ],
        }
    )
    topology = Topology.model_validate(
        {
            "groups": [
                {
                    "name": "paid",
                    "include": [{"kind": "procedures", "names": ["*_completion"]}],
                    "executor": "pool",
                    "workers": 4,
                },
                {"name": "spare", "catch_all": True, "executor": "pool"},
            ]
        }
    )
    assert validate_topology(topology, feat) == []
    namespace: dict = {"Any": object}
    exec(_placement_block(render_wiring(feat, "labthing.feat.yaml", topology)), namespace)

    class Unknown: ...

    lane = namespace["command_lane"](Unknown())
    assert lane == "paid"
    assert lane in namespace["LANES"]
    assert namespace["LANES"][lane][0] == "pool"
