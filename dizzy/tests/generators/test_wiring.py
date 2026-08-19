"""Snapshot + behavioural tests for the wiring generator.

The snapshots pin the emitted module; the assertions below pin the properties
that make it *correct* rather than merely stable — a snapshot happily records a
wiring that binds a policy to ``emit_event``, so the things that would actually
break a running feature are asserted by name.
"""

import ast

import pytest
from dizzy.generators.wiring import (
    feat_name,
    render_wiring,
    render_wiring_pyproject_toml,
)
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
