"""Tests for topology_loader — load_topology and validate_topology."""

import textwrap
from pathlib import Path

import pytest
from dizzy.feat_schema import FeatureDefinition
from dizzy.topology_loader import load_topology, validate_topology
from dizzy.topology_schema import Topology
from pydantic import ValidationError


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "topology.yaml"
    p.write_text(textwrap.dedent(content))
    return p


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


def test_validate_workers_with_procedure_rejected(tmp_path: Path) -> None:
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
    errors = validate_topology(topology, _one_procedure_feat())
    assert any("ingest_recipe" in e and "aggregate key" in e for e in errors)


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
    errors = validate_topology(topology, _one_procedure_feat())
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


def test_validate_max_in_flight_with_procedure_rejected(tmp_path: Path) -> None:
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
    errors = validate_topology(topology, _one_procedure_feat())
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
    errors = validate_topology(topology, _one_procedure_feat())
    assert len(errors) == 1
    assert "defaults.max_in_flight" in errors[0]


def test_validate_empty_group_may_not_scale(tmp_path: Path) -> None:
    """Only a policies-only membership may scale, and no membership is not policies."""
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
