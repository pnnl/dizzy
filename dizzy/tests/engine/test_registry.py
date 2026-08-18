"""FeatGraph — the feat file read as an app's runtime topology.

The premise under test: a scheduling shell can run an app it has never heard
of, because the feat already declares everything and DIZZY's naming convention
resolves each declared name to its generated class. Everything else in the
engine layer rests on this holding, and on it failing loudly when it doesn't.
"""

from __future__ import annotations

import pytest
from dizzy.engine.registry import (
    FeatGraph,
    camel_case,
    check_name,
    find_feat,
    graph,
    snake_case,
)

# ── The convention, both directions ─────────────────────────────────────────

@pytest.mark.parametrize("snake,camel", [
    ("classify_image", "ClassifyImage"),
    ("sync_bluesky_bookmarks", "SyncBlueskyBookmarks"),
    ("record_recipe", "RecordRecipe"),
    ("llm", "Llm"),
    ("nutritiondb", "Nutritiondb"),
    ("parse_html5", "ParseHtml5"),
    ("record_ab_test", "RecordAbTest"),
    ("x_y_z", "XYZ"),
    ("2fa_check", "2faCheck"),
])
def test_names_round_trip(snake, camel):
    """LinkML's camelcase produced the classes, so this must match it — not
    merely resemble it. Acronyms and digits are the cases that break naive
    implementations."""
    assert camel_case(snake) == camel
    assert snake_case(camel) == snake


def test_the_recipe_fixture_round_trips_end_to_end(recipe_feat):
    """Every name DIZZY's own example declares survives the convention."""
    for section in ("commands", "events", "procedures", "policies",
                    "projections", "queries", "models"):
        for item in getattr(recipe_feat, section, None) or []:
            assert snake_case(camel_case(item.name)) == item.name, item.name


# ── Resolution ──────────────────────────────────────────────────────────────

def test_declared_names_resolve_to_generated_classes(write_feat, def_package):
    pkg = def_package(commands=["RecordRecipe", "RateRecipe"],
                      events=["RecipeRecorded"])
    feat = write_feat(
        "commands:\n  record_recipe: a\n  rate_recipe: b\n"
        "events:\n  recipe_recorded: c\n")
    g = FeatGraph.load(feat, def_package=pkg)
    assert set(g.commands) == {"record_recipe", "rate_recipe"}
    assert g.commands["record_recipe"].__name__ == "RecordRecipe"
    assert g.command_class("rate_recipe").__name__ == "RateRecipe"
    assert g.command_name(g.commands["record_recipe"]) == "record_recipe"
    assert set(g.events) == {"recipe_recorded"}


def test_a_declared_but_ungenerated_name_fails_loudly(write_feat, def_package):
    """The stale-generation check. A hand-maintained dict cannot make it: a
    missing entry there looks like a command nobody dispatches."""
    pkg = def_package(commands=["RecordRecipe"])
    feat = write_feat("commands:\n  record_recipe: a\n  rate_recipe: b\n")
    with pytest.raises(RuntimeError, match=r"rate_recipe \(RateRecipe\)"):
        _ = FeatGraph.load(feat, def_package=pkg).commands


def test_an_undeclared_command_lookup_names_the_feat(write_feat, def_package):
    pkg = def_package(commands=["RecordRecipe"])
    g = FeatGraph.load(write_feat("commands:\n  record_recipe: a\n"),
                       def_package=pkg)
    with pytest.raises(KeyError, match="not declared"):
        g.command_class("no_such_command")


def test_resolution_is_lazy_per_section(write_feat, def_package):
    """A producer that only enqueues resolves commands and never imports the
    events module — the shells rely on this to stay cheap."""
    import sys

    pkg = def_package(commands=["RecordRecipe"], events=["RecipeRecorded"])
    g = FeatGraph.load(
        write_feat("commands:\n  record_recipe: a\nevents:\n  recipe_recorded: b\n"),
        def_package=pkg)
    _ = g.commands
    assert f"{pkg}.commands" in sys.modules
    assert f"{pkg}.events" not in sys.modules
    _ = g.events
    assert f"{pkg}.events" in sys.modules


# ── Names that need no import at all ────────────────────────────────────────

def test_environment_and_telemetry_are_just_names(write_feat):
    g = FeatGraph.load(write_feat(
        "environment:\n  llm: a\n  cas: b\ntelemetry:\n  usage: c\n"))
    assert g.environment == ("llm", "cas")
    assert g.telemetry == ("usage",)


def test_entry_normalizes_the_three_ways_a_feat_spells_a_declaration(write_feat):
    g = FeatGraph.load(write_feat(
        "commands:\n"
        "  described: just a description\n"
        "  mapped:\n    description: d\n    extra: e\n"
        "  drafted:\n"))
    assert g.entry("commands", "described") == {"description": "just a description"}
    assert g.entry("commands", "mapped") == {"description": "d", "extra": "e"}
    # A null value is a declared-but-unwritten entry: present, just empty.
    assert g.entry("commands", "drafted") == {}
    with pytest.raises(KeyError, match="not declared"):
        g.entry("commands", "absent")


# ── Guards: every one of these is typo-shaped ───────────────────────────────

@pytest.mark.parametrize("bad", [
    "record-recipe", "recordRecipe", "record__recipe", "_record_recipe",
    "record recipe", "record.recipe",
])
def test_a_name_that_would_alias_onto_another_class_is_refused(write_feat, bad):
    """camel_case splits on any non-word run, so all of these collapse onto
    RecordRecipe — the same class as `record_recipe`. Unchecked, a typo'd
    entry resolves to its NEIGHBOUR and the reverse lookup (which routes
    commands) maps it back to the wrong name."""
    with pytest.raises(RuntimeError, match="not snake_case"):
        FeatGraph.load(write_feat(f"commands:\n  {bad!r}: x\n"))


@pytest.mark.parametrize("scalar", ["123", "yes", "on", "off", "null"])
def test_a_yaml_scalar_key_names_the_feat(write_feat, scalar):
    """YAML reads these as int/bool/None, which would die inside `re` with no
    mention of the file that caused it."""
    with pytest.raises(RuntimeError, match="non-string name"):
        FeatGraph.load(write_feat(f"commands:\n  {scalar}: x\n"))


def test_check_name_is_reusable_on_its_own():
    assert check_name("record_recipe", "commands", "app.feat.yaml") == "record_recipe"
    with pytest.raises(RuntimeError, match="commands"):
        check_name("Record-Recipe", "commands", "app.feat.yaml")


def test_a_malformed_section_is_refused_at_load(write_feat):
    with pytest.raises(RuntimeError, match="is a list"):
        FeatGraph.load(write_feat("commands:\n  - record_recipe\n"))
    with pytest.raises(RuntimeError, match="is a str"):
        FeatGraph.load(write_feat("commands: record_recipe\n"))
    with pytest.raises(RuntimeError, match="not a feat file"):
        FeatGraph.load(write_feat("- a\n- b\n"))


def test_an_empty_feat_is_legal(write_feat):
    g = FeatGraph.load(write_feat(""))
    assert g.names("commands") == () and g.environment == ()


# ── Validation ──────────────────────────────────────────────────────────────

def test_validate_registered_reports_both_directions(write_feat):
    g = FeatGraph.load(write_feat("policies:\n  on_recorded: a\n  on_rated: b\n"))
    g.validate_registered({"policies": {"on_recorded", "on_rated"}})
    with pytest.raises(RuntimeError) as exc:
        g.validate_registered({"policies": {"on_recorded", "invented"}})
    message = str(exc.value)
    assert "not wired=['on_rated']" in message
    assert "not in feat=['invented']" in message


def test_an_unknown_section_is_an_error_not_an_empty_answer(write_feat):
    """It used to compare against an empty set: silently PASSING when nothing
    was wired, and blaming the app for everything when something was."""
    g = FeatGraph.load(write_feat("policies:\n  on_recorded: a\n"))
    with pytest.raises(KeyError, match="not a feat section"):
        g.names("widgets")
    with pytest.raises(KeyError, match="not a feat section"):
        g.validate_registered({"policy": set()})      # a singular typo


# ── Discovery: a worker boots knowing only where it is ──────────────────────

def test_find_feat_prefers_the_env_var(monkeypatch, write_feat, tmp_path):
    feat = write_feat("commands: {}\n")
    monkeypatch.setenv("DIZZY_FEAT_PATH", str(feat))
    assert find_feat(tmp_path / "elsewhere") == feat


def test_find_feat_walks_up(write_feat, tmp_path):
    feat = write_feat("commands: {}\n")
    nested = tmp_path / "host" / "deep"
    nested.mkdir(parents=True)
    assert find_feat(nested) == feat


def test_find_feat_refuses_to_guess(write_feat, tmp_path):
    write_feat("commands: {}\n", "a.feat.yaml")
    write_feat("commands: {}\n", "b.feat.yaml")
    with pytest.raises(RuntimeError, match="DIZZY_FEAT_PATH"):
        find_feat(tmp_path)


def test_find_feat_rejects_a_directory_and_an_empty_var(monkeypatch, tmp_path):
    monkeypatch.setenv("DIZZY_FEAT_PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="is a directory"):
        find_feat()
    # An exported-but-empty variable is a misconfiguration, not "unset":
    # falling through to the walk-up would pick up whatever feat is nearest.
    monkeypatch.setenv("DIZZY_FEAT_PATH", "")
    with pytest.raises(RuntimeError, match="set but empty"):
        find_feat()


def test_the_cache_is_keyed_on_the_resolved_feat(monkeypatch, write_feat):
    """It once cached a single graph, so the first caller's feat — and cwd,
    since discovery walks up — was handed to everyone after."""
    first_path = write_feat("commands: {}\n", "first.feat.yaml")
    second_path = write_feat("policies:\n  p: x\n", "second.feat.yaml")

    monkeypatch.setenv("DIZZY_FEAT_PATH", str(first_path))
    first = graph()
    assert graph() is first

    monkeypatch.setenv("DIZZY_FEAT_PATH", str(second_path))
    second = graph()
    assert second is not first
    assert second.feat_path == second_path
