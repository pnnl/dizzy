"""Tests for topology_loader — load_topology and validate_topology."""

import random
import textwrap
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml
from dizzy.feat_schema import FeatureDefinition
from dizzy.topology_loader import _SINGULAR, load_topology, validate_topology
from dizzy.topology_schema import Selector, Topology
from pydantic import ValidationError


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "topology.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# --- schema drift ---------------------------------------------------------------------

# Every feature-file section that is NOT a placeable element, and why. The pairing with
# _SINGULAR below is the point: a section is placeable or it is excluded here, and the
# suite will not let a third option exist silently.
_NOT_PLACEABLE = {
    "description": "prose about the feature, not an element",
    "commands": "a request. Procedures consume them; a command has no cursor of its own",
    "events": "the log's contents, not a consumer of the log",
    "models": "folded into, never placed — a model rides with its projections (rule 1)",
    "queries": "a read over a model, so placement is a read-substrate question (rule 4)",
    "environment": "values injected into an element, not an element",
    "telemetry": "signals emitted by an element, not an element",
}


def test_every_feat_section_is_placed_or_deliberately_excluded() -> None:
    """A section the validator has never heard of is INVISIBLE, not unclaimed.

    `_universe` builds the placeable set from a hardcoded list of three sections. If the
    feature-file gains a kind and that list is not updated, every instance of it falls
    out of the universe — so the rule-5 "claimed by no group" check never runs on it and
    the topology validates clean while an entire kind goes unplaced. That is the exact
    failure `catch_all` exists to prevent, arriving by a route `catch_all` cannot see.

    This is not hypothetical maintenance anxiety: `environment:` is slated for removal in
    the capabilities work, so this field list is actively moving. Failing here is the
    cheap outcome — someone reads the two sets and decides which side the new section is
    on. The expensive outcome is silent misplacement in generated deployment wiring.
    """
    declared = set(FeatureDefinition.model_fields)
    placeable = set(_SINGULAR)
    accounted = placeable | set(_NOT_PLACEABLE)

    unaccounted = declared - accounted
    assert not unaccounted, (
        f"feature-file section(s) {sorted(unaccounted)} are neither placed by "
        f"topology_loader._SINGULAR nor listed in _NOT_PLACEABLE. Decide which: add the "
        f"section to _SINGULAR (and _universe) if a topology should place it, or to "
        f"_NOT_PLACEABLE with the reason it is not an element."
    )
    stale = accounted - declared
    assert not stale, (
        f"{sorted(stale)} is claimed here but the feature-file no longer declares it — "
        f"drop it from _SINGULAR/_NOT_PLACEABLE so this test keeps meaning something."
    )
    # Disjoint, or a section could be quietly listed as both.
    assert not (placeable & set(_NOT_PLACEABLE))


# --- load ---------------------------------------------------------------------------


def test_load_minimal(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        groups:
          - name: fast
            include:
              - kind: projections
        """,
    )
    topology = load_topology(p)
    assert topology.groups is not None
    assert len(topology.groups) == 1
    assert topology.groups[0].include is not None
    assert len(topology.groups[0].include) == 1


def test_load_empty_file_yields_empty_groups(tmp_path: Path) -> None:
    topology = load_topology(_write(tmp_path, ""))
    assert topology.groups == []


def test_load_full_topology(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        description: Everything the schema allows
        transport:
          kind: nats
          url: nats://localhost:4222
        cursors:
          kind: sqlite
          url: ./cursors.db
        defaults:
          workers: 1
          max_in_flight: 16
          retry:
            max_attempts: 3
            backoff: exponential
            initial_delay_ms: 100
            dead_letter: true
        groups:
          - name: fanout
            description: The wide one
            catch_all: true
            workers: 1
            max_in_flight: 64
            retry:
              max_attempts: 5
              backoff: fixed
              initial_delay_ms: 50
            transport:
              kind: redis
              url: redis://localhost:6379
        """,
    )
    topology = load_topology(p)
    assert topology.transport is not None
    assert topology.transport.kind == "nats"
    assert topology.cursors is not None
    assert topology.cursors.url == "./cursors.db"
    assert topology.defaults is not None
    assert topology.defaults.retry is not None
    assert topology.defaults.retry.max_attempts == 3
    assert topology.groups is not None
    group = topology.groups[0]
    assert group.max_in_flight == 64
    assert group.transport is not None
    assert group.transport.kind == "redis"


def test_load_rejects_unknown_key(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        groups:
          - name: fast
            include:
              - nmaes: [ingest]
        """,
    )
    with pytest.raises(ValidationError):
        load_topology(p)


def test_load_rejects_queries_kind(tmp_path: Path) -> None:
    """Rule 4's `kind` half is unrepresentable: ElementKind has no `queries` value."""
    p = _write(
        tmp_path,
        """
        groups:
          - name: reads
            include:
              - kind: queries
        """,
    )
    with pytest.raises(ValidationError):
        load_topology(p)


def test_load_enum_is_a_plain_string(tmp_path: Path) -> None:
    """`use_enum_values = True` — a comparison against ElementKind would silently fail."""
    p = _write(
        tmp_path,
        """
        groups:
          - name: folds
            include:
              - kind: projections
        """,
    )
    topology = load_topology(p)
    assert topology.groups is not None
    assert topology.groups[0].include is not None
    assert topology.groups[0].include[0].kind == "projections"


# --- feats used by the validator tests ------------------------------------------------


def _feat(**sections: object) -> FeatureDefinition:
    return FeatureDefinition.model_validate({"description": "test feat", **sections})


def _one_procedure_feat() -> FeatureDefinition:
    return _feat(procedures=[{"name": "ingest_recipe", "command": "ingest"}])


def _reading_procedure_feat() -> FeatureDefinition:
    """One procedure that queries a model — the read-modify-write rule 3 exists for."""
    return _feat(
        models=[{"name": "catalog"}],
        queries=[{"name": "get_recipe", "description": "d", "model": "catalog"}],
        procedures=[{"name": "open_batch", "command": "open", "queries": ["get_recipe"]}],
    )


def _catalog_feat() -> FeatureDefinition:
    """Two projections folding one model — the fixture rule 1 exists for."""
    return _feat(
        models=[{"name": "catalog"}],
        projections=[
            {
                "name": "ingredient_catalog",
                "description": "d",
                "event": "recipe_ingested",
                "model": "catalog",
            },
            {
                "name": "recipe_catalog",
                "description": "d",
                "event": "recipe_ingested",
                "model": "catalog",
            },
        ],
    )


# --- structural -----------------------------------------------------------------------


def test_validate_empty_topology(tmp_path: Path) -> None:
    topology = load_topology(_write(tmp_path, "groups: []"))
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1
    assert "no consumer groups" in errors[0]


def test_validate_duplicate_group_name(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: fast
                catch_all: true
              - name: fast
                include:
                  - kind: procedures
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("fast" in e and "more than once" in e for e in errors)


def test_validate_group_claims_nothing(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: idle
              - name: work
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("idle" in e and "claims nothing" in e for e in errors)


def test_validate_empty_selector(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: all
                include:
                  - {}
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1
    assert "all" in errors[0] and "selector #1" in errors[0] and "states none of" in errors[0]


@pytest.mark.parametrize(
    "body",
    [
        # A group served by no worker, or holding no unit of work, never advances; a
        # unit failed before it is attempted, or a negative delay, is the same class of
        # stalled state. The schema bounds them, so they are unrepresentable, not
        # detectable — validate_topology never sees one.
        "groups:\n  - name: stalled\n    catch_all: true\n    workers: 0\n",
        "groups:\n  - name: stalled\n    catch_all: true\n    max_in_flight: 0\n",
        "groups:\n  - name: stalled\n    catch_all: true\n    retry: {max_attempts: 0}\n",
        "groups:\n  - name: stalled\n    catch_all: true\n"
        "    retry: {backoff: fixed, initial_delay_ms: -5}\n",
        "defaults:\n  workers: 0\ngroups:\n  - name: work\n    catch_all: true\n",
        "defaults:\n  max_in_flight: 0\ngroups:\n  - name: work\n    catch_all: true\n",
    ],
)
def test_load_rejects_stalled_knobs(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValidationError):
        load_topology(_write(tmp_path, body))


def test_validate_two_catch_all_groups(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: alpha
                catch_all: true
              - name: beta
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("alpha" in e and "beta" in e and "rule 5" in e for e in errors)


# --- referential ------------------------------------------------------------------------


def test_validate_unknown_model(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - model: recipies
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "recipes"}],
        projections=[
            {"name": "recipe_library", "description": "d", "event": "e", "model": "recipes"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("recipies" in e and "not declared in models" in e for e in errors)


def test_validate_model_no_projection_folds_it(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - model: recipes
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "recipes"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
    )
    errors = validate_topology(topology, feat)
    assert any("recipes" in e and "matches nothing" in e for e in errors)


def test_validate_unknown_literal_name(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                include:
                  - names: [sumarize_text]
              - name: rest
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("sumarize_text" in e for e in errors)


def test_validate_glob_matches_nothing(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                include:
                  - names: ["expand_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("expand_*" in e for e in errors)


def test_validate_name_declared_under_other_kind(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                include:
                  - kind: procedures
                    names: [ingest]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "recipes"}],
        projections=[{"name": "ingest", "description": "d", "event": "e", "model": "recipes"}],
    )
    errors = validate_topology(topology, feat)
    assert any("procedures" in e and "projection" in e for e in errors)


def test_validate_kind_excluded_glob_beside_a_live_pattern(tmp_path: Path) -> None:
    """A sibling pattern that matches must not swallow one the selector's kind excludes:
    the elements the author meant to place would drift into catch_all, silently."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: paid
                include:
                  - kind: procedures
                    names: [ingest_recipe, "summarize_*"]
              - name: default
                catch_all: true
            """,
        )
    )
    feat = _feat(
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
        policies=[
            {"name": "summarize_recipe", "event": "recipe_ingested"},
            {"name": "summarize_notes", "event": "recipe_ingested"},
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "summarize_*" in errors[0] and "procedures" in errors[0]
    assert "summarize_recipe" in errors[0] and "summarize_notes" in errors[0]


def test_validate_dead_glob_beside_a_live_pattern_is_clean(tmp_path: Path) -> None:
    """Matching by property is what lets a topology survive the feat gaining an element:
    a glob matching nothing *today* is not an error while the selector still claims."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: expand
                include:
                  - kind: procedures
                    names: ["ingest_*", "derive_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []


def test_validate_wholly_dead_selector_names_the_pattern(tmp_path: Path) -> None:
    """...but a selector that claims nothing must say which pattern went dead."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: expand
                include:
                  - kind: procedures
                    names: ["derive_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1
    assert "claims nothing" in errors[0] and "'derive_*'" in errors[0]


def test_validate_name_declared_under_two_kinds_lists_both(tmp_path: Path) -> None:
    """Half the truth would send the author looking for the wrong declaration."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
                    names: [ingest]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        procedures=[{"name": "ingest", "command": "ingest"}],
        policies=[{"name": "ingest", "event": "recipe_ingested"}],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "a procedure and a policy" in errors[0]


def test_validate_model_with_non_projection_kind(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                include:
                  - kind: policies
                    model: recipes
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "recipes"}],
        policies=[{"name": "index_recipe", "event": "recipe_ingested"}],
        projections=[
            {"name": "recipe_library", "description": "d", "event": "e", "model": "recipes"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("only projections fold a model" in e for e in errors)


def test_validate_selector_fields_have_no_element_in_common(tmp_path: Path) -> None:
    """The pattern matches; the kind excludes everything it matched. R4's wording would lie."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                include:
                  - kind: policies
                    names: ["recipe_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("claims nothing" in e for e in errors)
    assert not any("matches no declared" in e for e in errors)


# --- rule 4 -------------------------------------------------------------------------------


def test_validate_glob_matches_query(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reads
                include:
                  - names: ["get_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        queries=[{"name": "get_recipe", "description": "d"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
    )
    errors = validate_topology(topology, feat)
    assert any("get_recipe" in e and "rule 4" in e for e in errors)


def test_validate_glob_matches_query_and_procedure(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reads
                include:
                  - names: ["get_*"]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        queries=[{"name": "get_recipe", "description": "d"}],
        procedures=[{"name": "get_batch", "command": "get"}],
    )
    errors = validate_topology(topology, feat)
    assert any("get_recipe" in e and "rule 4" in e for e in errors)


def test_validate_repeated_query_pattern_is_reported_once(tmp_path: Path) -> None:
    """One mistake, one fix, one message — a pattern written twice is still one pattern."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reads
                include:
                  - names: [get_recipe, get_recipe]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        queries=[{"name": "get_recipe", "description": "d"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "get_recipe" in errors[0] and "rule 4" in errors[0]


# SUBSUMED: test_oracle_agrees_with_validator — dropping rule 4's literal exemption
# (`if hits and (is_glob or not any_kind)` -> `if hits`) makes the differential disagree, the
# generator aliasing a query name onto a procedure name often enough to reach it.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_exact_name_shared_with_a_query_claims_the_element(tmp_path: Path) -> None:
    """Nothing enforces cross-kind name uniqueness, so a name may be both. An exact name
    that resolves to a placeable element is unambiguous — it cannot be a query reference,
    because no selector can claim a query."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                include:
                  - names: [audit]
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        queries=[{"name": "audit", "description": "d"}],
        policies=[{"name": "audit", "event": "recipe_ingested"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
    )
    assert validate_topology(topology, feat) == []


def test_validate_empty_names_list_claims_nothing(tmp_path: Path) -> None:
    """`names: []` states a name constraint nothing satisfies.

    Reading it as "no name constraint" would widen the selector to every projection —
    a file that enumerates no name placing a whole kind, which is the opposite of what
    it says.
    """
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
                    names: []
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "folds" in errors[0] and "selector #1 claims nothing" in errors[0]


# SUBSUMED: test_oracle_agrees_with_validator — applying rule 4 to a selector that
# states a kind (dropping the `selector.kind is None and selector.model is None` guard) makes
# the differential disagree.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_kind_scoped_glob_over_query_name_is_clean(tmp_path: Path) -> None:
    """A selector stating a kind provably cannot claim a query, so rule 4 must not fire."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                include:
                  - kind: procedures
                    names: ["get_*"]
            """,
        )
    )
    feat = _feat(
        queries=[{"name": "get_recipe", "description": "d"}],
        procedures=[{"name": "get_batch", "command": "get"}],
    )
    assert validate_topology(topology, feat) == []


def test_validate_catch_all_does_not_sweep_queries(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: everything
                catch_all: true
            """,
        )
    )
    feat = _feat(
        queries=[
            {"name": "get_recipe", "description": "d"},
            {"name": "get_recipe_text", "description": "d"},
            {"name": "list_recipes", "description": "d"},
        ],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
    )
    assert validate_topology(topology, feat) == []


# --- rule 5 -------------------------------------------------------------------------------


def _double_claim_topology(tmp_path: Path) -> Topology:
    return load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: fast
                include:
                  - kind: projections
                    names: ["batch_*"]
              - name: ledger
                include:
                  - model: batches
            """,
        )
    )


def _batches_feat() -> FeatureDefinition:
    return _feat(
        models=[{"name": "batches"}],
        projections=[
            {"name": "batch_ledger", "description": "d", "event": "e", "model": "batches"}
        ],
    )


def test_validate_double_claim(tmp_path: Path) -> None:
    errors = validate_topology(_double_claim_topology(tmp_path), _batches_feat())
    assert any(
        "batch_ledger" in e and "fast" in e and "ledger" in e and "rule 5" in e for e in errors
    )


def test_validate_double_claim_emits_one_message_per_element(tmp_path: Path) -> None:
    errors = validate_topology(_double_claim_topology(tmp_path), _batches_feat())
    assert len([e for e in errors if "batch_ledger" in e]) == 1


# SUBSUMED: test_oracle_agrees_with_validator and
# test_metamorphic_catch_all_equals_writing_out_the_sweep — counting a claim per matching
# selector instead of per group turns within-group overlap into a double claim, and both fail.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_two_selectors_in_one_group_are_additive(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
                    names: ["batch_*"]
                  - model: batches
            """,
        )
    )
    assert validate_topology(topology, _batches_feat()) == []


def test_validate_unclaimed_element_without_catch_all(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
            """,
        )
    )
    feat = _feat(
        models=[{"name": "batches"}],
        procedures=[{"name": "ingest_batch", "command": "ingest"}],
        projections=[
            {"name": "batch_ledger", "description": "d", "event": "e", "model": "batches"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("ingest_batch" in e and "catch_all" in e for e in errors)


# SUBSUMED: test_oracle_agrees_with_validator plus three metamorphic
# relations — stopping the sweep in `_resolve` leaves the remainder unclaimed, and all four fail.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_catch_all_claims_the_remainder(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "batches"}],
        procedures=[{"name": "ingest_batch", "command": "ingest"}],
        projections=[
            {"name": "batch_ledger", "description": "d", "event": "e", "model": "batches"}
        ],
    )
    assert validate_topology(topology, feat) == []


# SUBSUMED: test_oracle_agrees_with_validator,
# test_metamorphic_catch_all_equals_writing_out_the_sweep and test_metamorphic_concurrency_gate
# — making an empty sweep an error fails all three.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_catch_all_sweeping_nothing_is_clean(tmp_path: Path) -> None:
    """A fully enumerated topology plus a safety net is good practice, not an error."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - kind: projections
                  - kind: procedures
              - name: net
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "batches"}],
        procedures=[{"name": "ingest_batch", "command": "ingest"}],
        projections=[
            {"name": "batch_ledger", "description": "d", "event": "e", "model": "batches"}
        ],
    )
    assert validate_topology(topology, feat) == []


# --- rule 1 -------------------------------------------------------------------------------


def test_validate_model_projections_split_across_groups(tmp_path: Path) -> None:
    """The one-line break: pull the hot projection out, and the fold interleaves."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: hot
                include:
                  - names: [ingredient_catalog]
              - name: default
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _catalog_feat())
    assert any(
        "catalog" in e and "ingredient_catalog" in e and "recipe_catalog" in e and "rule 1" in e
        for e in errors
    )


# SUBSUMED: test_oracle_agrees_with_validator — narrowing `_matches` so a
# `model:` selector reaches only part of its cohort splits the fold and the differential
# disagrees. Medium confidence: `model:` selectors are a rare draw, so the property tests reach
# this shape less often than the rest of this list.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_model_selector_keeps_cohort_together(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - model: catalog
              - name: default
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, _catalog_feat()) == []


def test_validate_split_cohort_with_different_adapters_still_errors(tmp_path: Path) -> None:
    """Rule 1 keys on the model alone — the adapter does not make two folds independent."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: hot
                include:
                  - names: [ingredient_catalog]
              - name: default
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog", "adapters": ["sqla", "relative_filesystem"]}],
        projections=[
            {
                "name": "ingredient_catalog",
                "description": "d",
                "event": "e",
                "model": "catalog",
                "adapter": "sqla",
            },
            {
                "name": "recipe_catalog",
                "description": "d",
                "event": "e",
                "model": "catalog",
                "adapter": "relative_filesystem",
            },
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("rule 1" in e for e in errors)


# SUBSUMED: test_oracle_agrees_with_validator — cohorting model-less
# projections (`(p.model or model.name) == model.name`) makes the differential disagree.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_model_less_projections_split_is_clean(tmp_path: Path) -> None:
    """A projection folding no declared model is in no cohort — nothing to constrain."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: hot
                include:
                  - names: [ingredient_catalog]
              - name: default
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "ingredient_catalog", "description": "d", "event": "e"},
            {"name": "recipe_catalog", "description": "d", "event": "e"},
        ],
    )
    assert validate_topology(topology, feat) == []


def test_validate_split_suppressed_when_double_claimed(tmp_path: Path) -> None:
    """A doubly claimed projection is in nobody's membership: one message, not two."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: hot
                include:
                  - names: [ingredient_catalog]
              - name: also_hot
                include:
                  - names: ["ingredient_*"]
              - name: default
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _catalog_feat())
    assert any("ingredient_catalog" in e and "rule 5" in e for e in errors)
    assert not any("rule 1" in e for e in errors)


# --- rule 2 and the workers gate -----------------------------------------------------------


def test_validate_workers_with_projection_rejected(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                workers: 4
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("folds" in e and "recipe_catalog" in e and "workers" in e for e in errors)


def test_validate_workers_with_querying_procedure_rejected(tmp_path: Path) -> None:
    """Rule 3: a procedure that reads a model decides on state a peer may be changing."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                workers: 4
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert any("open_batch" in e and "'get_recipe'" in e and "aggregate key" in e for e in errors)


# SUBSUMED: test_metamorphic_concurrency_gate and
# test_oracle_agrees_with_validator — gating on kind `procedure` instead of on `reads` fails both.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_query_free_procedure_group_may_scale(tmp_path: Path) -> None:
    """A procedure declaring no queries computes its events from the command alone.

    Nothing a concurrent procedure does can reach it, so the group scales. This is the
    labthing case — a paid model call reads no model — and the fan-out `expand_*` tier.
    """
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: expand
                workers: 32
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []


# SUBSUMED: test_metamorphic_concurrency_gate, which is
# parametrized over max_in_flight alone, and test_oracle_agrees_with_validator — gating on kind
# `procedure` instead of on `reads` fails both.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_query_free_procedure_group_may_hold_many_in_flight(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: expand
                max_in_flight: 64
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []


def test_validate_one_reader_serialises_a_group_of_query_free_peers(tmp_path: Path) -> None:
    """The gate is per-element, not per-group-kind: one reader is enough to force serial."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                workers: 8
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        queries=[{"name": "get_recipe", "description": "d", "model": "catalog"}],
        procedures=[
            {"name": "ingest_recipe", "command": "ingest"},
            {"name": "open_batch", "command": "open", "queries": ["get_recipe"]},
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    # It names the reader, never the query-free peer that is not the problem.
    assert "open_batch" in errors[0] and "ingest_recipe" not in errors[0]


def test_validate_reader_error_names_every_query_it_reads(tmp_path: Path) -> None:
    """The message has to say WHICH reads block the scaling, or it is not actionable."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                workers: 4
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        queries=[
            {"name": "get_recipe", "description": "d", "model": "catalog"},
            {"name": "check_inventory", "description": "d", "model": "catalog"},
        ],
        procedures=[
            {
                "name": "open_batch",
                "command": "open",
                "queries": ["get_recipe", "check_inventory"],
            }
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "'get_recipe' and 'check_inventory'" in errors[0]


# SUBSUMED: test_metamorphic_concurrency_gate and
# test_oracle_agrees_with_validator — pinning every policy-holding group fails both.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_policies_only_group_may_scale(tmp_path: Path) -> None:
    """Rule 2: policies may be partitioned freely."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                workers: 8
                include:
                  - kind: policies
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
        policies=[
            {"name": "index_recipe", "event": "recipe_ingested"},
            {"name": "notify_owner", "event": "recipe_ingested"},
        ],
    )
    assert validate_topology(topology, feat) == []


def test_validate_workers_with_querying_policy_rejected(tmp_path: Path) -> None:
    """Rule 3 keys on `queries:`, not on the kind that declares it.

    Rule 2 exonerates policies for observing events out of order, and says nothing about
    the model one reads: a policy declaring `queries:` decides on state a peer may be
    changing, exactly as a procedure does, and the generated policy context carries a real
    `query:` handle to do it with. The shipped recipes example has such a policy.
    """
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                workers: 32
                include:
                  - kind: policies
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        queries=[{"name": "find_blocked", "description": "d", "model": "catalog"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
        policies=[
            {"name": "advance_ready", "event": "recipe_ingested", "queries": ["find_blocked"]},
            {"name": "notify_owner", "event": "recipe_ingested"},
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    # Named as a policy, and never the query-free peer that is not the problem.
    assert "policy 'advance_ready'" in errors[0] and "notify_owner" not in errors[0]
    assert "'find_blocked'" in errors[0] and "aggregate key" in errors[0]


def test_validate_max_in_flight_with_querying_policy_rejected(tmp_path: Path) -> None:
    """The other knob reaches a reading policy too — one worker, 64 decisions at once."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                max_in_flight: 64
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        queries=[{"name": "find_blocked", "description": "d", "model": "catalog"}],
        policies=[
            {"name": "advance_ready", "event": "recipe_ingested", "queries": ["find_blocked"]}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("advance_ready" in e and "max_in_flight" in e for e in errors)


def test_validate_query_free_policy_group_still_scales(tmp_path: Path) -> None:
    """Rule 2 survives the change: a policy that reads nothing partitions as before, and a
    reading procedure of the same name elsewhere must not be mistaken for it."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                workers: 8
                include:
                  - kind: policies
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        queries=[{"name": "get_recipe", "description": "d", "model": "catalog"}],
        procedures=[{"name": "audit", "command": "ingest", "queries": ["get_recipe"]}],
        policies=[{"name": "audit", "event": "recipe_ingested"}],
    )
    assert validate_topology(topology, feat) == []


def test_validate_workers_inherited_from_defaults(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            defaults:
              workers: 4
            groups:
              - name: work
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert any("defaults.workers" in e for e in errors)


def test_validate_max_in_flight_with_projection_rejected(tmp_path: Path) -> None:
    """One worker holding 64 units folds 64 events at once — the same interleaving."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                catch_all: true
                workers: 1
                max_in_flight: 64
            """,
        )
    )
    errors = validate_topology(topology, _catalog_feat())
    assert len(errors) == 1
    assert "max_in_flight is 64" in errors[0] and "log order" in errors[0]


def test_validate_max_in_flight_with_querying_procedure_rejected(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: expand
                catch_all: true
                max_in_flight: 64
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert len(errors) == 1
    assert "max_in_flight is 64" in errors[0] and "aggregate key" in errors[0]


def test_validate_max_in_flight_inherited_from_defaults(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            defaults:
              max_in_flight: 4
            groups:
              - name: work
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert len(errors) == 1
    assert "defaults.max_in_flight" in errors[0]


def test_validate_empty_group_may_not_scale(tmp_path: Path) -> None:
    """An empty group is not a scalable one: there is no work in it to spread."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: everything
                include:
                  - kind: procedures
                  - kind: policies
                  - kind: projections
              - name: idle
                catch_all: true
                workers: 32
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
        policies=[{"name": "index_recipe", "event": "recipe_ingested"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1
    assert "idle" in errors[0] and "claims no element" in errors[0]


# SUBSUMED: test_metamorphic_concurrency_gate, which is
# parametrized over max_in_flight alone, and test_oracle_agrees_with_validator — pinning every
# policy-holding group fails both.
# Candidate for deletion once the property tests have soaked. Not deleted yet.
def test_validate_policies_only_group_may_hold_many_in_flight(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                max_in_flight: 64
                include:
                  - kind: policies
              - name: rest
                catch_all: true
            """,
        )
    )
    feat = _feat(
        procedures=[{"name": "ingest_recipe", "command": "ingest"}],
        policies=[{"name": "index_recipe", "event": "recipe_ingested"}],
    )
    assert validate_topology(topology, feat) == []


def test_validate_catch_all_group_gated_on_swept_membership(tmp_path: Path) -> None:
    """The gate reads resolved membership: the group declares no projection, it sweeps one."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: reactions
                include:
                  - kind: policies
              - name: rest
                workers: 4
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        policies=[{"name": "index_recipe", "event": "recipe_ingested"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any("rest" in e and "recipe_catalog" in e and "log order" in e for e in errors)


# --- happy path -----------------------------------------------------------------------------


def test_validate_recipe_topology_clean(tmp_path: Path, recipe_feat: FeatureDefinition) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                include:
                  - model: recipes
              - name: reactions
                include:
                  - kind: policies
              - name: rest
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, recipe_feat) == []


def test_validate_single_catch_all_group(tmp_path: Path, recipe_feat: FeatureDefinition) -> None:
    """The degenerate topology — equivalent to shipping no file at all."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, recipe_feat) == []


# --- property testing: the oracle -----------------------------------------------------------
#
# What follows is a second, independent implementation of the placement rules, written
# from the rule text in `docs/explanation/topology.md` rather than from
# `topology_loader.py`, and kept deliberately naive: one block per rule, in spec order,
# nested loops, no early returns, a `legal` flag ANDed down so every rule is evaluated on
# every input. Its obviousness is the whole point — it is here to disagree with the real
# validator when the real validator is wrong, so it must be readable by someone who has
# only read the rules. Do not "clean it up" into something that shares structure with the
# implementation it exists to check.
#
# Ambiguity rulings — each place the rule text does not decide, the reading taken, why:
#
# A-UNBOUNDED — an unstated `max_in_flight` contributes 1 to a group's concurrency. Only
#     reading that keeps the default topology (which holds reading procedures) legal.
# A-AUTHOR-WRITTEN — an inherited value gates exactly as a written one; rule text says
#     defaults are inherited, and the oracle decides legality only, so error attribution
#     text is moot here.
# A-GLOB-DEAD — a glob matching nothing is not an error by itself; only a selector that
#     claims nothing is. A literal unknown name IS an error even when siblings in the same
#     `names` list match, because the unknown-name rule quantifies over every literal.
# A-NAMESPACING — a `names`-only selector matching both a procedure and a like-named
#     policy claims both; no rule forbids it.
# A-MODEL-WITH-KIND — `{kind: policies, model: X}` gets no coherence check; it matches
#     nothing and dies as an empty selector, which is the same verdict either way.
# A-MULTIPLICATIVE-CONCURRENCY — concurrency is max(workers, max_in_flight), as the rule
#     text literally states, not the product.
# A-RETRY-COHERENCE / A-TRANSPORT-URL / A-UNKNOWN-FIELDS — accepted silently; no rule
#     makes `initial_delay_ms` or `url` required, and parsed objects reach us, so an
#     unknown key never does.
# A-COMMAND-LOG — the placeable universe is procedures + policies + projections. Command
#     requests are not placeable elements today, whatever the log ends up recording.
#
# Three rulings were revised against the repo after the first differential run. Each was
# genuinely undetermined by the rule text and is settled — by the tests above, by the
# schema, or by rule 1's own justification — rather than by the implementation:
#
# Four readings this oracle took were, on review, not determined by the rule text at
# all — the text was simply silent, and both implementations had quietly agreed on an
# answer neither could cite. Rather than leave them as rulings the oracle invented and
# the validator happened to share, the sentences that settle them were written into
# `docs/explanation/topology.md`, and the blocks below cite them like any other rule:
#
#   * a concurrent group may hold no projection (now rule 1's second half),
#   * a group stating neither `include` nor `catch_all` is an error,
#   * a group claiming no element may not ask for concurrency,
#   * rule 4's exemption applies to an exact name that resolves to an element, and to
#     nothing else — no glob, and no literal that resolves to nothing.
#
# The last of those is the one worth naming: the exemption is a real judgement call about
# a name-space the feature-file does not police, it was reachable by construction (the
# generator aliases a query name onto a procedure 30% of the time), and until it was
# written down an oracle agreeing with the validator here proved nothing.
#
# Two disagreements this oracle won outright, both fixed in `topology_loader.py` rather
# than reconciled here: a group scaled while holding a POLICY that declares `queries:`
# was accepted, because rule 3's gate had been keyed on the kind instead of on the read;
# and `names: []` widened a selector to everything its other fields allowed instead of
# claiming nothing. Neither was reachable by the generators as they stood, which is why
# both now have a draw of their own.

_PLACEABLE_KINDS = ("procedures", "policies", "projections")
_TRANSPORT_KINDS = ("inprocess", "zmq-ipc", "nats", "postgres-notify", "redis")
_CURSOR_KINDS = ("memory", "sqlite", "postgres")
_BACKOFF_KINDS = ("none", "fixed", "exponential")


def _enum_value(v: object) -> object:
    """Enum members and their `use_enum_values` string form both reach us."""
    return getattr(v, "value", v)


def _oracle_elements(feat: FeatureDefinition) -> list[tuple[str, str, str | None]]:
    """E, as (kind, name, model-or-None) triples."""
    out: list[tuple[str, str, str | None]] = []
    for procedure in feat.procedures or []:
        out.append(("procedures", procedure.name, None))
    for policy in feat.policies or []:
        out.append(("policies", policy.name, None))
    for projection in feat.projections or []:
        out.append(("projections", projection.name, projection.model))
    return out


def _is_literal(entry: str) -> bool:
    return not any(ch in entry for ch in ("*", "?", "["))


def _selector_matches(sel: Selector, element: tuple[str, str, str | None]) -> bool:
    """Every field the selector states must hold; omitted fields impose nothing, so a
    bare selector matches all of E."""
    kind, name, model = element
    ok = True
    sel_kind = _enum_value(sel.kind)
    if sel_kind is not None:
        if sel_kind != kind:
            ok = False
    if sel.names is not None:
        any_name = False
        for entry in sel.names:
            if entry == name:
                any_name = True
            elif fnmatchcase(name, entry):
                any_name = True
        if not any_name:
            ok = False
    if sel.model is not None:
        if kind != "projections":
            ok = False
        elif model != sel.model:
            ok = False
    return ok


def oracle_is_legal(topology: Topology | None, feat: FeatureDefinition) -> bool:  # noqa: C901, PLR0912, PLR0915
    legal = True

    # T-ABSENT-OK: absence of a topology file is legal and denotes the default topology,
    # which no later rule may reject.
    if topology is None:
        return True

    elements = _oracle_elements(feat)
    groups = list(topology.groups or [])

    # T-SCHEMA-SHAPE: required fields present, enumerated values in range, types correct.
    # The objects arrive parsed, so this re-asserts rather than discovers — but a
    # hand-built object can still be out of range.
    for g in groups:
        if not isinstance(g.name, str):
            legal = False
    for tr in [topology.transport] + [g.transport for g in groups]:
        if tr is not None:
            if _enum_value(tr.kind) not in _TRANSPORT_KINDS:
                legal = False
    if topology.cursors is not None:
        if _enum_value(topology.cursors.kind) not in _CURSOR_KINDS:
            legal = False
    retries = [g.retry for g in groups]
    if topology.defaults is not None:
        retries.append(topology.defaults.retry)
    for r in retries:
        if r is not None and r.backoff is not None:
            if _enum_value(r.backoff) not in _BACKOFF_KINDS:
                legal = False
    for g in groups:
        for sel in g.include or []:
            if sel.kind is not None:
                if _enum_value(sel.kind) not in _PLACEABLE_KINDS:
                    legal = False

    # T-CONCURRENCY-DOMAIN: workers and max_in_flight are integers >= 1 wherever they
    # appear; max_attempts >= 1; initial_delay_ms >= 0.
    knob_holders: list[object] = list(groups)
    if topology.defaults is not None:
        knob_holders.append(topology.defaults)
    for holder in knob_holders:
        for value in (getattr(holder, "workers", None), getattr(holder, "max_in_flight", None)):
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool):
                    legal = False
                elif value < 1:
                    legal = False
    for r in retries:
        if r is not None:
            if r.max_attempts is not None:
                if r.max_attempts < 1:
                    legal = False
            if r.initial_delay_ms is not None:
                if r.initial_delay_ms < 0:
                    legal = False

    # T-GROUPS-PRESENT: a topology with no groups places nothing.
    if not groups:
        legal = False

    # T-GROUP-NAME-UNIQUE: for distinct entries g_i, g_j: g_i.name != g_j.name.
    for i in range(len(groups)):
        for j in range(len(groups)):
            if i < j:
                if groups[i].name == groups[j].name:
                    legal = False

    # T-GROUP-CLAIMS-SOMETHING: a group must state `include` or `catch_all`. Stating
    # neither is unclaimable under every feature-file — the first of the three smaller
    # conditions the rules document lists after rule 4.
    for g in groups:
        if not (g.include or []) and not g.catch_all:
            legal = False

    # T-CATCHALL-AT-MOST-ONE: at most one group with catch_all true; absent means false.
    catch_all_groups = []
    for g in groups:
        if g.catch_all is True:
            catch_all_groups.append(g)
    if len(catch_all_groups) > 1:
        legal = False

    # T-CATCHALL-ADDITIVE: explicit(g) comes from the selectors; the catch_all group
    # additionally claims everything no other group explicitly claimed. Defined by
    # subtraction, so it is independent of where in the list the sweeper sits.
    explicit: dict[int, list[tuple[str, str, str | None]]] = {}
    for idx, g in enumerate(groups):
        matched: list[tuple[str, str, str | None]] = []
        for e in elements:
            hit = False
            for sel in g.include or []:
                if _selector_matches(sel, e):
                    hit = True
            if hit:
                matched.append(e)
        explicit[idx] = matched

    claimed: dict[int, list[tuple[str, str, str | None]]] = {}
    for idx in range(len(groups)):
        claimed[idx] = list(explicit[idx])
    if len(catch_all_groups) == 1:
        c_idx = groups.index(catch_all_groups[0])
        for e in elements:
            claimed_elsewhere = False
            for idx in range(len(groups)):
                if idx != c_idx and e in explicit[idx]:
                    claimed_elsewhere = True
            if not claimed_elsewhere and e not in claimed[c_idx]:
                claimed[c_idx].append(e)

    # T-NO-DOUBLE-CLAIM: explicit(g_i) ∩ explicit(g_j) = ∅ for distinct groups. Overlap
    # within one group is fine — selectors are additive.
    for i in range(len(groups)):
        for j in range(len(groups)):
            if i < j:
                for e in explicit[i]:
                    if e in explicit[j]:
                        legal = False

    # T-COVERAGE: every element of E is claimed by some group.
    for e in elements:
        found = False
        for idx in range(len(groups)):
            if e in claimed[idx]:
                found = True
        if not found:
            legal = False

    # T-SELECTOR-EMPTY: every selector must match at least one element of E.
    for g in groups:
        for sel in g.include or []:
            hits = 0
            for e in elements:
                if _selector_matches(sel, e):
                    hits += 1
            if hits == 0:
                legal = False

    # T-SELECTOR-UNKNOWN-NAME: every glob-free `names` entry must name a declared
    # placeable element, and one the rest of its own selector does not exclude — a
    # literal is a specific element the author meant to place, so a sibling pattern
    # matching is no excuse for this one matching nothing. Query, model and command
    # names do not count.
    for g in groups:
        for sel in g.include or []:
            for entry in sel.names or []:
                if _is_literal(entry):
                    resolves = False
                    for e in elements:
                        if e[1] == entry and _selector_matches(sel, e):
                            resolves = True
                    if not resolves:
                        legal = False

    # T-SELECTOR-UNKNOWN-MODEL: a stated `model:` must name a declared model. This can
    # only ever fire where T-SELECTOR-EMPTY already has — an undeclared model matches
    # nothing by construction — so it is kept for fidelity to the rules, not because a
    # differential case turns on it.
    model_names = []
    for m in feat.models or []:
        model_names.append(m.name)
    for g in groups:
        for sel in g.include or []:
            if sel.model is not None:
                if sel.model not in model_names:
                    legal = False

    # T-NO-QUERIES (rule 4): a selector reaching a query name is refused. A selector
    # stating a kind or a model provably cannot reach one, so only bare `names` patterns
    # are examined. Rule 4's exemption, taken exactly as written and no further: an exact
    # entry that is ALSO a declared placeable element claims that element. A glob gets no
    # exemption even when it matches elements too, and neither does a literal that
    # resolves to no element.
    query_names = [q.name for q in feat.queries or []]
    for g in groups:
        for sel in g.include or []:
            if sel.kind is None and sel.model is None:
                for entry in sel.names or []:
                    reaches_query = False
                    for q in query_names:
                        if q == entry or fnmatchcase(q, entry):
                            reaches_query = True
                    exempt = False
                    if _is_literal(entry):
                        for e in elements:
                            if e[1] == entry:
                                exempt = True
                    if reaches_query and not exempt:
                        legal = False

    # T-RULE1-PROJECTIONS-UNSPLIT: for each declared model, at most one group may hold
    # any of its projections.
    for m_name in model_names:
        holders = 0
        for idx in range(len(groups)):
            touches = False
            for kind, _name, model in claimed[idx]:
                if kind == "projections" and model == m_name:
                    touches = True
            if touches:
                holders += 1
        if holders > 1:
            legal = False

    # T-RULE2-POLICIES-FREE: policies partition freely, and a group holding only policies
    # scales freely. This rule can only exonerate, so there is nothing to reject here.

    # T-RULE3-SERIAL-GROUPS: a group whose effective concurrency exceeds 1 may hold
    # neither a projection (rule 1's second half) nor any element declaring `queries:`,
    # and must hold something. Rule 3 names the `queries:` list as the discriminator and
    # says explicitly that the kind declaring it is not — so a reading POLICY pins its
    # group exactly as a reading procedure does, rule 2 having exonerated policies for
    # event order alone. Inherited values count (ruling A-AUTHOR-WRITTEN); an unstated
    # knob contributes 1 (A-UNBOUNDED).
    default_workers = None
    default_in_flight = None
    if topology.defaults is not None:
        default_workers = topology.defaults.workers
        default_in_flight = topology.defaults.max_in_flight

    reading_elements = []
    for p in feat.procedures or []:
        if p.queries:
            reading_elements.append(("procedures", p.name))
    for y in feat.policies or []:
        if y.queries:
            reading_elements.append(("policies", y.name))

    for idx, g in enumerate(groups):
        if g.workers is not None:
            workers_eff = g.workers
        elif default_workers is not None:
            workers_eff = default_workers
        else:
            workers_eff = 1
        if g.max_in_flight is not None:
            in_flight_eff = g.max_in_flight
        elif default_in_flight is not None:
            in_flight_eff = default_in_flight
        else:
            in_flight_eff = 1
        concurrency = max(workers_eff, in_flight_eff)
        if concurrency > 1:
            if not claimed[idx]:
                legal = False
            for kind, name, _model in claimed[idx]:
                if kind == "projections":
                    legal = False
                if (kind, name) in reading_elements:
                    legal = False

    # T-KNOB-INDEPENDENCE / T-DEFAULTS-INHERITANCE: no independent check — both are
    # consumed by the concurrency computation above.

    return legal


# --- property testing: the generators --------------------------------------------------------
#
# Both take a `random.Random` and touch no other entropy, so a seed pins the draw exactly.
# They aim at REALISTIC artifacts — the kind a person would author — not at the pydantic
# models' full input space: a generator that mostly emits topologies rejected in Phase A
# never reaches the placement rules, which are the part worth testing.

# Selectors state the plural kind; the loader's universe reports the singular one.
_PLURAL = {"procedure": "procedures", "policy": "policies", "projection": "projections"}

# The concurrency knobs are drawn from this pool INDEPENDENTLY of each other: the shape
# worth reaching is workers=1 with max_in_flight=64 — a group that reads as serial and is
# not — and it is unreachable if the two knobs move together.
_KNOBS = (None, 1, 1, 2, 8, 64)
_SERIAL_KNOBS = (None, 1, 1)


def _make_feat(rng: random.Random) -> FeatureDefinition:
    """A random feature-file whose internal references all resolve.

    Sizes are chosen so the placement rules are reachable: several projections over few
    models (rule 1 needs a model with a cohort), and roughly half the procedures AND
    policies reading (rule 3 discriminates on exactly that, so both sides must be common
    for both kinds — a generator that only ever gave `queries:` to procedures is what let
    a scaled group of reading policies pass unnoticed).
    """
    models = [f"m{i}" for i in range(rng.choice([1, 1, 2, 2, 3]))]
    commands = [f"c{i}" for i in range(rng.randint(1, 3))]
    events = [f"e{i}" for i in range(rng.randint(1, 3))]

    procedure_names = [f"p{i}" for i in range(rng.randint(1, 5))]
    query_names = [f"q{i}" for i in range(rng.randint(1, 3))]
    # Nothing enforces cross-kind name uniqueness, and rule 4 treats an exact name that is
    # both a query and an element differently from a glob that sweeps one up.
    if rng.random() < 0.3:
        query_names[rng.randrange(len(query_names))] = rng.choice(procedure_names)

    queries = [
        {"name": name, "description": "d", "model": rng.choice(models)} for name in query_names
    ]

    def _reads() -> list[str]:
        if rng.random() >= 0.5:
            return []
        return rng.sample(query_names, rng.randint(1, min(2, len(query_names))))

    procedures: list[dict[str, object]] = [
        {"name": name, "command": rng.choice(commands), "queries": _reads()}
        for name in procedure_names
    ]

    policies: list[dict[str, object]] = [
        {"name": f"y{i}", "event": rng.choice(events), "queries": _reads()}
        for i in range(rng.randint(0, 4))
    ]

    projections: list[dict[str, object]] = []
    for i in range(rng.randint(0, 6)):
        # A projection folding nothing is legal and sits in the universe folding no model,
        # so no `model:` selector can reach it and rule 1 never applies to it.
        model = None if rng.random() < 0.1 else rng.choice(models)
        projections.append(
            {"name": f"j{i}", "description": "d", "event": rng.choice(events), "model": model}
        )

    return FeatureDefinition.model_validate(
        {
            "description": "generated feat",
            "models": [{"name": m} for m in models],
            "commands": [{"name": c} for c in commands],
            "events": [{"name": e} for e in events],
            "queries": queries,
            "procedures": procedures,
            "policies": policies,
            "projections": projections,
        }
    )


def _gen_universe(feat: FeatureDefinition) -> list[tuple[str, str]]:
    """Every placeable element as ``(singular kind, name)``, mirroring the loader."""
    sections = [
        ("procedure", feat.procedures or []),
        ("policy", feat.policies or []),
        ("projection", feat.projections or []),
    ]
    return [(kind, element.name) for kind, elements in sections for element in elements]


def _partition(
    rng: random.Random, universe: list[tuple[str, str]], n: int
) -> list[list[tuple[str, str]]]:
    """Split the universe across *n* groups, either by kind or at random.

    Partitioning by kind is what an author actually writes, and it is the only cheap way
    to produce a procedure-only group — without one, every concurrent group holds a
    projection and rule 3 is shadowed by rule 1's serial gate forever.
    """
    buckets: list[list[tuple[str, str]]] = [[] for _ in range(n)]
    if not universe:
        return buckets
    if rng.random() < 0.45:
        for offset, kind in enumerate(("procedure", "policy", "projection")):
            buckets[offset % n].extend(e for e in universe if e[0] == kind)
    else:
        shuffled = list(universe)
        rng.shuffle(shuffled)
        for i, element in enumerate(shuffled):
            buckets[i % n].append(element)
    return buckets


def _exact_glob(names: list[str], pool: list[str]) -> str | None:
    """A prefix pattern matching exactly *names* out of *pool*, or None if there is none.

    Membership is recomputed from the pattern rather than assumed: the partition has to
    stay exact, so a pattern is used only where fnmatch agrees it claims that bucket and
    nothing else. Without this the topology a group is drawn to hold and the topology it
    actually holds would silently diverge, and every disagreement would read as a bug.
    """
    prefix = names[0]
    for name in names[1:]:
        while prefix and not name.startswith(prefix):
            prefix = prefix[:-1]
    pattern = f"{prefix}*"
    return pattern if {n for n in pool if fnmatchcase(n, pattern)} == set(names) else None


def _selectors(
    rng: random.Random, bucket: list[tuple[str, str]], universe: list[tuple[str, str]]
) -> list[dict[str, object]]:
    """Render a bucket as `names:` selectors, sometimes as a glob that covers it exactly.

    Globs are the shape the rules treat differently — rule 4's exemption turns on
    literal-vs-glob, and a stated `kind` excluding a matching pattern has its own message —
    so a corpus of literals alone leaves that whole surface to the example tests.
    """
    if not bucket:
        return []
    if rng.random() < 0.5:
        by_kind: dict[str, list[str]] = {}
        for kind, name in bucket:
            by_kind.setdefault(kind, []).append(name)
        out: list[dict[str, object]] = []
        for kind, names in by_kind.items():
            # A stated kind confines the match to that kind, so exactness is judged there.
            pool = [n for k, n in universe if k == kind]
            glob = _exact_glob(names, pool) if rng.random() < 0.35 else None
            out.append({"kind": _PLURAL[kind], "names": [glob] if glob else names})
        return out
    names = [name for _, name in bucket]
    # A bare `names:` glob is the only shape that can reach a query, which is what makes
    # rule 4's glob half reachable at all — the query-name aliasing in `_make_feat` puts a
    # query under such a pattern whenever the alias and the bucket line up.
    glob = _exact_glob(names, [n for _, n in universe]) if rng.random() < 0.35 else None
    return [{"names": [glob] if glob else names}]


def _make_topology(rng: random.Random, feat: FeatureDefinition) -> Topology:
    """A random topology over *feat*, legal about one draw in seven.

    The perturbations below are drawn independently of one another and of the knobs, so a
    single draw can carry several defects at once — that is the case where the validator's
    phase ordering and its sole-membership rule actually interact.
    """
    universe = _gen_universe(feat)
    n_groups = rng.randint(1, 4)
    names = [f"g{i}" for i in range(n_groups)]
    buckets = _partition(rng, universe, n_groups)

    catch_all_at = rng.randrange(n_groups) if rng.random() < 0.45 else None
    if catch_all_at is not None:
        # Leave most of the sweeper's own elements unenumerated, so the sweep does real
        # work instead of being a no-op sitting next to a full enumeration.
        buckets[catch_all_at] = [e for e in buckets[catch_all_at] if rng.random() < 0.3]

    if universe and n_groups >= 2 and rng.random() < 0.3:
        element = rng.choice(universe)
        stealer = rng.randrange(n_groups)
        if element not in buckets[stealer]:
            buckets[stealer].append(element)

    if catch_all_at is None and universe and rng.random() < 0.3:
        orphan = rng.choice(universe)
        buckets = [[e for e in bucket if e != orphan] for bucket in buckets]

    if n_groups >= 2 and rng.random() < 0.15:
        victim = rng.randrange(1, n_groups)
        names[victim] = names[victim - 1]

    # Half the draws are serial throughout. Drawing every knob from the full pool would
    # make an ordering error near-certain in any topology with more than one group, and a
    # corpus with no legal member says nothing about what the validator accepts.
    pool = _SERIAL_KNOBS if rng.random() < 0.5 else _KNOBS

    # A second sweeper, rarely. Rule 5's at-most-one check is otherwise unreachable from
    # this generator, and the verdict-invariance properties never otherwise see a topology
    # whose error names two groups — which is exactly where a reordering could show.
    sweepers = {catch_all_at} if catch_all_at is not None else set()
    if catch_all_at is not None and n_groups >= 2 and rng.random() < 0.06:
        sweepers.add((catch_all_at + 1) % n_groups)

    includes = [_selectors(rng, bucket, universe) for bucket in buckets]

    # Reference draws. Everything above renders names that resolve, so none of it can
    # reach the unknown-name, unknown-model, dead-glob, empty-`names` or rule-4 branches —
    # before these, several of those checks could be deleted from the oracle without a
    # single differential seed noticing. Each is drawn independently, so one topology can
    # carry several, and each lands on a group the partition already filled.
    extras: list[dict[str, object]] = []
    if universe:
        # One edit off a real name: the typo an author actually makes. It goes BESIDE the
        # name it fat-fingered, in the group that already holds it — alone the selector
        # would claim nothing, and the empty-selector rule, not the unknown-name rule,
        # would be what rejected the draw.
        typo_kind, typo_name = rng.choice(universe)
        owner = next(
            (i for i, bucket in enumerate(buckets) if (typo_kind, typo_name) in bucket),
            catch_all_at,
        )
        if owner is not None and rng.random() < 0.06:
            includes[owner].append({"names": [typo_name, f"{typo_name}_"]})
    if rng.random() < 0.06:
        model_names = [m.name for m in feat.models or []]
        extras.append({"model": rng.choice([*model_names, "nosuch"])})
    if rng.random() < 0.06:
        extras.append({"kind": rng.choice(list(_PLURAL.values())), "names": []})
    if rng.random() < 0.06:
        extras.append({"names": ["zz*"]})
    if universe and rng.random() < 0.06:
        # A glob a stated kind excludes, beside kinds it does match — the shape that would
        # otherwise drift into the sweeper with nothing said.
        kind, name = rng.choice(universe)
        wrong = rng.choice([k for k in _PLURAL.values() if k != _PLURAL[kind]])
        extras.append({"kind": wrong, "names": [f"{name[0]}*"]})
    query_names = [q.name for q in feat.queries or []]
    if query_names and rng.random() < 0.08:
        # Both halves of rule 4: an exact query name (exempt only if it aliases an
        # element) and a glob that sweeps one up (never exempt).
        query = rng.choice(query_names)
        extras.append({"names": [query if rng.random() < 0.5 else f"{query[0]}*"]})
    if extras:
        includes[rng.randrange(n_groups)].extend(extras)

    groups: list[dict[str, object]] = []
    for i in range(n_groups):
        groups.append(
            {
                "name": names[i],
                "include": includes[i],
                "catch_all": i in sweepers,
                "workers": rng.choice(pool),
                "max_in_flight": rng.choice(pool),
            }
        )

    topology: dict[str, object] = {"groups": groups}
    if rng.random() < 0.3:
        # Inheritance has its own message-attribution path (`defaults.workers`), and a
        # default only bites a group that stated nothing, so both knobs stay low here.
        topology["defaults"] = {
            "workers": rng.choice((None, 1, 2)),
            "max_in_flight": rng.choice((None, 1, 2)),
        }
    return Topology.model_validate(topology)


def _dump(feat: FeatureDefinition, topology: Topology, errors: list[str]) -> str:
    """A failing property is worthless without the artifact that failed it."""
    return (
        "\n--- feat ---\n"
        + yaml.safe_dump(feat.model_dump(exclude_none=True, mode="json"), sort_keys=False)
        + "\n--- topology ---\n"
        + yaml.safe_dump(topology.model_dump(exclude_none=True, mode="json"), sort_keys=False)
        + "\n--- validator errors ---\n"
        + ("\n".join(errors) or "(none)")
    )


# --- property testing: the differential -------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_oracle_agrees_with_validator(seed: int) -> None:
    """The real validator and the naive oracle must agree on legality, always.

    A disagreement is not a tuning problem. One of the two has misread the rules, and
    which one is a judgement call to be made by reading them again — never by relaxing
    whichever side is easier to edit.
    """
    rng = random.Random(seed)
    for _ in range(100):
        feat = _make_feat(rng)
        topology = _make_topology(rng, feat)
        errors = validate_topology(topology, feat)
        expected = oracle_is_legal(topology, feat)
        assert (errors == []) == expected, (
            f"validator says {'legal' if not errors else 'illegal'}, oracle says "
            f"{'legal' if expected else 'illegal'}" + _dump(feat, topology, errors)
        )


# --- property testing: metamorphic relations ---------------------------------------------------
#
# Each of these asserts how the VERDICT MUST CHANGE (or must not) under a transformation
# of a valid input, so none of them needs to know what the right answer was to begin with.
# That is what makes them survive a change to the rules that would invalidate the oracle.


def _catch_all_topology(**knobs: object) -> Topology:
    """The degenerate topology: one group, claiming everything."""
    return Topology.model_validate({"groups": [{"name": "everything", "catch_all": True, **knobs}]})


@pytest.mark.parametrize("seed", range(40))
def test_metamorphic_splitting_a_cohort_breaks_rule_1(seed: int) -> None:
    """Pull one projection of a multi-projection model into its own group and the fold
    interleaves. The topology was clean; it must not stay clean."""
    rng = random.Random(seed)
    checked = 0
    # Draw until enough draws QUALIFY, not until enough are drawn: a fixed draw count
    # makes the guard below flaky (one seed in ~600 produces no cohort at all), and a
    # guard that trips at random teaches a reader to ignore it.
    for _ in range(60):
        if checked == 8:
            break
        feat = _make_feat(rng)
        cohorts = [
            [p.name for p in feat.projections or [] if p.model == m.name] for m in feat.models or []
        ]
        multi = [c for c in cohorts if len(c) >= 2]
        if not multi:
            continue
        assert validate_topology(_catch_all_topology(), feat) == []

        moved = rng.choice(rng.choice(multi))
        split = Topology.model_validate(
            {
                "groups": [
                    {"name": "hot", "include": [{"kind": "projections", "names": [moved]}]},
                    {"name": "rest", "catch_all": True},
                ]
            }
        )
        errors = validate_topology(split, feat)
        assert any("rule 1" in e for e in errors), (
            f"moving projection '{moved}' out of its cohort must break rule 1"
            + _dump(feat, split, errors)
        )
        checked += 1
    assert checked, "no draw produced a model with two projections — generator has drifted"


@pytest.mark.parametrize("seed", range(30))
@pytest.mark.parametrize(
    ("workers", "max_in_flight"),
    # Both knobs, independently: the interesting shape is a group that reads as serial
    # (workers: 1) and is not, because it holds 64 units in flight.
    [(2, None), (32, None), (None, 2), (None, 64), (1, 64), (64, 1), (8, 8)],
)
def test_metamorphic_concurrency_gate(
    seed: int, workers: int | None, max_in_flight: int | None
) -> None:
    """Scaling a clean group must error exactly when that group holds something that has
    to see the log in order: a projection, or any element that queries a model."""
    rng = random.Random(seed)
    kind = rng.choice(["procedures", "policies", "projections"])
    checked = 0
    # Draw until enough draws actually declare the chosen kind — policies and projections
    # are both optional, so a fixed draw count can leave the assertion never reached.
    for _ in range(40):
        if checked == 6:
            break
        feat = _make_feat(rng)
        universe = _gen_universe(feat)
        if not any(_PLURAL[k] == kind for k, _ in universe):
            continue
        checked += 1
        # All of one kind in the target group keeps rule 1 satisfied either way: a model's
        # projections are together in `target` or together in `rest`.
        groups: list[dict[str, object]] = [
            {"name": "target", "include": [{"kind": kind}]},
            {"name": "rest", "catch_all": True},
        ]
        serial = Topology.model_validate({"groups": groups})
        assert validate_topology(serial, feat) == [], "baseline must be clean"

        scaled = Topology.model_validate(
            {
                "groups": [
                    {**groups[0], "workers": workers, "max_in_flight": max_in_flight},
                    groups[1],
                ]
            }
        )
        # Rule 3 keys on `queries:` and not on the kind, so a group of policies is
        # refused on exactly the same evidence a group of procedures is.
        readers = {
            "procedures": any(p.queries for p in feat.procedures or []),
            "policies": any(y.queries for y in feat.policies or []),
        }
        must_serialise = kind == "projections" or readers.get(kind, False)
        errors = validate_topology(scaled, feat)
        offenders = [e for e in errors if "'target'" in e]
        assert bool(offenders) == must_serialise, (
            f"group of {kind} at workers={workers} max_in_flight={max_in_flight} "
            f"{'must' if must_serialise else 'must not'} be refused" + _dump(feat, scaled, errors)
        )
    assert checked, f"no draw declared a {kind} — generator has drifted"


@pytest.mark.parametrize("seed", range(40))
def test_metamorphic_group_rename_preserves_verdict(seed: int) -> None:
    """Group names are labels the selectors never reference. Renaming every group must
    change only which names the messages quote — never how many there are."""
    rng = random.Random(seed)
    for _ in range(10):
        feat = _make_feat(rng)
        topology = _make_topology(rng, feat)
        before = validate_topology(topology, feat)

        raw = topology.model_dump(exclude_none=True, mode="json")
        # Over the distinct names, so a topology that duplicates a name still does.
        mapping = {
            name: f"renamed{i}"
            for i, name in enumerate(dict.fromkeys(g["name"] for g in raw["groups"]))
        }
        for group in raw["groups"]:
            group["name"] = mapping[group["name"]]
        after = validate_topology(Topology.model_validate(raw), feat)

        assert (before == []) == (after == []) and len(before) == len(after), (
            "renaming groups changed the verdict" + _dump(feat, topology, before)
        )


@pytest.mark.parametrize("seed", range(40))
def test_metamorphic_group_reorder_preserves_verdict(seed: int) -> None:
    """`catch_all` is defined by subtraction precisely so declaration order cannot matter.
    If a group's position ever changes what it claims, this is where it shows up."""
    rng = random.Random(seed)
    for _ in range(10):
        feat = _make_feat(rng)
        topology = _make_topology(rng, feat)
        before = validate_topology(topology, feat)

        raw = topology.model_dump(exclude_none=True, mode="json")
        rng.shuffle(raw["groups"])
        after = validate_topology(Topology.model_validate(raw), feat)

        assert (before == []) == (after == []) and len(before) == len(after), (
            "reordering groups changed the verdict" + _dump(feat, topology, before)
        )


@pytest.mark.parametrize("seed", range(40))
def test_metamorphic_catch_all_equals_writing_out_the_sweep(seed: int) -> None:
    """`catch_all` is sugar, not semantics: replacing the sweeper with an enumeration of
    exactly what it would have swept must land on the same verdict.

    The enumeration states a `kind`, which the sweep does not — so this also pins that a
    kind-qualified selector claims the same elements the sweep does.
    """
    rng = random.Random(seed)
    checked = 0
    # As above: draw until enough draws carry exactly one sweeper.
    for _ in range(60):
        if checked == 8:
            break
        feat = _make_feat(rng)
        topology = _make_topology(rng, feat)
        groups = topology.groups or []
        sweepers = [g for g in groups if g.catch_all]
        if len(sweepers) != 1:
            continue
        sweeper = sweepers[0]
        elements = _oracle_elements(feat)
        swept = [
            e
            for e in elements
            if not any(
                any(_selector_matches(s, e) for s in g.include or [])
                for g in groups
                if g is not sweeper
            )
        ]
        raw = topology.model_dump(exclude_none=True, mode="json")
        target = raw["groups"][groups.index(sweeper)]
        by_kind: dict[str, list[str]] = {}
        for kind, name, _model in swept:
            by_kind.setdefault(kind, []).append(name)
        target["include"] = list(target.get("include") or []) + [
            {"kind": kind, "names": names} for kind, names in by_kind.items()
        ]
        target["catch_all"] = False
        if not target["include"]:
            # An empty enumeration is a group stating nothing, which is its own error —
            # a different topology, not the same one written out.
            continue

        before = validate_topology(topology, feat)
        after = validate_topology(Topology.model_validate(raw), feat)
        assert (before == []) == (after == []) and len(before) == len(after), (
            "writing out the sweep changed the verdict" + _dump(feat, topology, before)
        )
        checked += 1
    assert checked, "no draw produced a single-sweeper topology — generator has drifted"


@pytest.mark.parametrize("seed", range(30))
def test_metamorphic_validation_is_pure(seed: int) -> None:
    """Two calls, one answer, and neither input touched. Generation reads a topology more
    than once (validate, then emit), so a validator that resolved membership in place
    would produce a different wiring than it validated."""
    rng = random.Random(seed)
    for _ in range(10):
        feat = _make_feat(rng)
        topology = _make_topology(rng, feat)
        feat_before = feat.model_dump(mode="json")
        topology_before = topology.model_dump(mode="json")

        first = validate_topology(topology, feat)
        second = validate_topology(topology, feat)

        assert first == second, "validate_topology is not deterministic" + _dump(
            feat, topology, first
        )
        assert feat.model_dump(mode="json") == feat_before, "validate_topology mutated the feat"
        assert topology.model_dump(mode="json") == topology_before, (
            "validate_topology mutated the topology"
        )


# --- the single-machine placement vocabulary: executor and priority ------------------------


def test_load_executor_and_priority(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: paid
                include:
                  - kind: procedures
                    names: ["*_completion"]
                executor: pool
                workers: 4
              - name: default
                catch_all: true
            priority:
              interactive: [send_notification, "approve_*"]
            """,
        )
    )
    assert topology.groups is not None
    # use_enum_values: the loaded value is the string, which is what every consumer
    # compares against and what the generator writes into the wiring.
    assert topology.groups[0].executor == "pool"
    assert topology.groups[1].executor is None
    assert topology.priority is not None
    assert topology.priority.interactive == ["send_notification", "approve_*"]


def test_default_topology_states_neither_knob(tmp_path: Path) -> None:
    """The absent-file default is one inline lane and every command batch. A topology
    that says nothing about either must validate exactly as it did before they existed."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            """,
        )
    )
    assert topology.priority is None
    assert topology.groups is not None
    assert topology.groups[0].executor is None
    assert validate_topology(topology, _one_procedure_feat()) == []


def test_validate_pool_executor_with_projection_rejected(tmp_path: Path) -> None:
    """`executor: pool` is a concurrency claim, so rule 1 refuses it for a fold.

    The background lane runs while the main lane runs; a projection served there folds
    out of log order exactly as a second worker would. The message is rule 1's own —
    eligibility for the pool is not a second question, so it does not get a second
    answer — and only the field it names changes.
    """
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                executor: pool
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert any(
        "folds" in e and "recipe_catalog" in e and "executor is pool" in e for e in errors
    ), errors
    # The repair a pool group needs is not the one a worker count needs.
    assert any("executor: inline" in e for e in errors), errors


def test_validate_pool_executor_with_querying_procedure_rejected(tmp_path: Path) -> None:
    """Rule 3 already decides pool eligibility: an element that reads a model may not
    leave the lane that folds it, whatever took it off."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: paid
                executor: pool
                catch_all: true
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert any(
        "open_batch" in e and "'get_recipe'" in e and "aggregate key" in e for e in errors
    ), errors


def test_validate_pool_executor_with_query_free_procedures_is_clean(tmp_path: Path) -> None:
    """The whole point: a paid model call reads nothing, so it comes off the lock."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: paid
                executor: pool
                workers: 4
                catch_all: true
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []


def test_validate_pool_executor_reported_over_a_worker_count(tmp_path: Path) -> None:
    """Both knobs are set and both are illegal here; the message names the executor,
    because dropping to one pool worker would still not put the group back on the lane
    that folds."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: folds
                executor: pool
                workers: 4
                catch_all: true
            """,
        )
    )
    feat = _feat(
        models=[{"name": "catalog"}],
        projections=[
            {"name": "recipe_catalog", "description": "d", "event": "e", "model": "catalog"}
        ],
    )
    errors = validate_topology(topology, feat)
    assert len(errors) == 1, errors
    assert "executor is pool" in errors[0] and "workers is 4" not in errors[0]


def test_validate_inline_executor_is_the_default_and_scales_by_workers(tmp_path: Path) -> None:
    """Stating `executor: inline` must not change a verdict, or the default would be a
    third thing rather than the absence of a choice."""
    stated = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: work
                executor: inline
                workers: 4
                catch_all: true
            """,
        )
    )
    errors = validate_topology(stated, _reading_procedure_feat())
    assert any("workers is 4" in e and "open_batch" in e for e in errors), errors


def test_validate_priority_naming_an_undeclared_command_rejected(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            priority:
              interactive: [ingest, ingset]
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1, errors
    assert "'ingset'" in errors[0] and "priority.interactive" in errors[0]


def test_validate_priority_naming_a_procedure_instead_of_its_command_rejected(
    tmp_path: Path,
) -> None:
    """The name-space trap. `include` selectors name procedures; `priority` names the
    commands they consume, and `_reading_procedure_feat` has procedure `open_batch`
    handling command `open`, so guessing wrong is silently a no-op unless refused."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            priority:
              interactive: [open_batch]
            """,
        )
    )
    errors = validate_topology(topology, _reading_procedure_feat())
    assert any("'open_batch'" in e and "not on the names of the procedures" in e for e in errors), (
        errors
    )


def test_validate_priority_matching_nothing_at_all_rejected(tmp_path: Path) -> None:
    """A glob is tolerated on its own — matching by property is what lets a topology
    survive the feature-file gaining a command — but a whole section that prioritises
    nothing is an author believing they made something interactive."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            priority:
              interactive: ["zz*"]
            """,
        )
    )
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1, errors
    assert "matches no command" in errors[0] and "'zz*'" in errors[0]


def test_validate_priority_glob_survives_beside_a_live_one(tmp_path: Path) -> None:
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: default
                catch_all: true
            priority:
              interactive: ["ing*", "zz*"]
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []


def test_validate_priority_needs_no_procedure_to_be_placed_anywhere_special(
    tmp_path: Path,
) -> None:
    """Priority and placement are orthogonal: an interactive command may perfectly well
    be run by a procedure on a pool lane, and neither knob constrains the other."""
    topology = load_topology(
        _write(
            tmp_path,
            """
            groups:
              - name: paid
                executor: pool
                catch_all: true
            priority:
              interactive: [ingest]
            """,
        )
    )
    assert validate_topology(topology, _one_procedure_feat()) == []
