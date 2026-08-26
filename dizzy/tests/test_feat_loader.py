"""Tests for feat_loader — YAML loading, normalization, and validation."""

from pathlib import Path

from dizzy.feat_loader import load_feat, validate_feat
from dizzy.feat_schema import (
    CommandDef,
    EventDef,
    FeatureDefinition,
    ModelDef,
    PolicyDef,
    ProcedureDef,
    ProjectionDef,
    QueryDef,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _by_name(items: list | None, name: str):
    """Find a Def object in a list by its name field."""
    return next(item for item in (items or []) if item.name == name)


class TestLoadFeat:
    def test_loads_recipe(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        assert feat.description == "Recipe App"

    def test_recipe_models(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        model = _by_name(feat.models, "recipes")
        assert model.adapters == ["sqla", "relative_filesystem"]

    def test_recipe_queries(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        q = _by_name(feat.queries, "get_recipe_text")
        assert q.model == "recipes"
        assert q.adapter == "relative_filesystem"

    def test_recipe_query_different_adapter(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        q = _by_name(feat.queries, "get_recipe")
        assert q.model == "recipes"
        assert q.adapter == "sqla"

    def test_command_string_shorthand(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        cmd = _by_name(feat.commands, "ingest_recipe_text")
        assert isinstance(cmd, CommandDef)
        assert cmd.description == "Initiates ingestion of a recipe from a raw text source"

    def test_event_string_shorthand(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        evt = _by_name(feat.events, "recipe_ingested")
        assert isinstance(evt, EventDef)

    def test_procedure(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        proc = _by_name(feat.procedures, "extract_and_transform_recipe")
        assert proc.command == "ingest_recipe_text"
        assert "get_recipe_text" in proc.queries
        assert "recipe_ingested" in proc.emits

    def test_policy(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        policy = _by_name(feat.policies, "index_recipe_on_ingest")
        assert policy.event == "recipe_ingested"
        assert "get_recipe" in policy.queries

    def test_projection_with_adapter(self):
        feat = load_feat(FIXTURES_DIR / "recipe.feat.yaml")
        proj = _by_name(feat.projections, "recipe_library")
        assert proj.event == "recipe_ingested"
        assert proj.model == "recipes"
        assert proj.adapter == "sqla"

    def test_environment_and_telemetry_sections(self):
        feat = load_feat(FIXTURES_DIR / "agent.feat.yaml")
        assert _by_name(feat.environment, "model").description.startswith("The LLM model")
        assert _by_name(feat.telemetry, "stream_chunk")
        proc = _by_name(feat.procedures, "run_agent_turn")
        assert proc.environment == ["model"]
        assert proc.telemetry == ["stream_chunk"]
        querier = _by_name(feat.queries, "get_conversation")
        assert querier.environment == ["model"]
        assert querier.telemetry == ["stream_chunk"]

    def test_loads_partial(self):
        feat = load_feat(FIXTURES_DIR / "partial.feat.yaml")
        assert any(c.name == "do_thing" for c in feat.commands or [])
        assert any(q.name == "find_thing" for q in feat.queries or [])
        assert any(p.name == "run_thing" for p in feat.procedures or [])
        assert not feat.models
        assert not feat.events
        assert not feat.policies
        assert not feat.projections

    def test_partial_query_no_model(self):
        feat = load_feat(FIXTURES_DIR / "partial.feat.yaml")
        q = _by_name(feat.queries, "find_thing")
        assert q.model is None
        assert q.adapter is None


class TestValidateFeat:
    def test_valid_recipe(self, recipe_feat):
        assert validate_feat(recipe_feat) == []

    def test_valid_partial(self, partial_feat):
        assert validate_feat(partial_feat) == []

    def test_valid_agent(self, agent_feat):
        assert validate_feat(agent_feat) == []

    def test_procedure_unknown_environment(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="do_thing", description="does a thing")],
            procedures=[
                ProcedureDef(
                    name="my_proc",
                    description="proc",
                    command="do_thing",
                    environment=["missing_env"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("environment 'missing_env'" in e for e in errors)

    def test_procedure_unknown_telemetry(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="do_thing", description="does a thing")],
            procedures=[
                ProcedureDef(
                    name="my_proc",
                    description="proc",
                    command="do_thing",
                    telemetry=["missing_sink"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("telemetry 'missing_sink'" in e for e in errors)

    def test_querier_unknown_telemetry(self):
        feat = FeatureDefinition(
            queries=[
                QueryDef(
                    name="my_query",
                    description="query",
                    telemetry=["missing_sink"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("querier 'my_query': telemetry 'missing_sink'" in e for e in errors)

    def test_procedure_unknown_command(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="do_thing", description="does a thing")],
            procedures=[
                ProcedureDef(name="my_proc", description="proc", command="unknown_command")
            ],
        )
        errors = validate_feat(feat)
        assert any("command 'unknown_command'" in e for e in errors)

    def test_procedure_unknown_query(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="do_thing", description="does a thing")],
            queries=[QueryDef(name="real_query", description="real")],
            procedures=[
                ProcedureDef(
                    name="my_proc",
                    description="proc",
                    command="do_thing",
                    queries=["missing_query"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("query 'missing_query'" in e for e in errors)

    def test_procedure_emits_unknown_event(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="do_thing", description="does a thing")],
            events=[EventDef(name="real_event", description="real")],
            procedures=[
                ProcedureDef(
                    name="my_proc",
                    description="proc",
                    command="do_thing",
                    emits=["ghost_event"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("emits 'ghost_event'" in e for e in errors)

    def test_policy_unknown_event(self):
        feat = FeatureDefinition(
            events=[EventDef(name="real_event", description="real")],
            policies=[PolicyDef(name="my_policy", description="policy", event="unknown_event")],
        )
        errors = validate_feat(feat)
        assert any("event 'unknown_event'" in e for e in errors)

    def test_policy_unknown_query(self):
        feat = FeatureDefinition(
            events=[EventDef(name="some_event", description="event")],
            queries=[QueryDef(name="real_query", description="real")],
            policies=[
                PolicyDef(
                    name="my_policy",
                    description="policy",
                    event="some_event",
                    queries=["missing_query"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("query 'missing_query'" in e for e in errors)

    def test_policy_emits_unknown_command(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="real_cmd", description="real")],
            events=[EventDef(name="some_event", description="event")],
            policies=[
                PolicyDef(
                    name="my_policy",
                    description="policy",
                    event="some_event",
                    emits=["ghost_cmd"],
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("emits 'ghost_cmd'" in e for e in errors)

    def test_projection_unknown_event(self):
        feat = FeatureDefinition(
            events=[EventDef(name="real_event", description="real")],
            projections=[ProjectionDef(name="my_proj", description="proj", event="unknown_event")],
        )
        errors = validate_feat(feat)
        assert any("event 'unknown_event'" in e for e in errors)

    def test_projection_unknown_model(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="real_model", description="a real model", adapters=["sqla"])],
            events=[EventDef(name="some_event", description="event")],
            projections=[
                ProjectionDef(
                    name="my_proj",
                    description="proj",
                    event="some_event",
                    model="ghost_model",
                    adapter="sqla",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("model 'ghost_model'" in e for e in errors)

    def test_query_unknown_model(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="real_model", description="a real model", adapters=["sqla"])],
            queries=[
                QueryDef(
                    name="my_query",
                    description="query",
                    model="ghost_model",
                    adapter="sqla",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("model 'ghost_model'" in e for e in errors)

    def test_query_model_without_adapter(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="recipes", description="recipes", adapters=["sqla"])],
            queries=[QueryDef(name="my_query", description="query", model="recipes")],
        )
        errors = validate_feat(feat)
        assert any("model declared without adapter" in e for e in errors)

    def test_query_adapter_without_model(self):
        feat = FeatureDefinition(
            queries=[QueryDef(name="my_query", description="query", adapter="sqla")],
        )
        errors = validate_feat(feat)
        assert any("adapter declared without model" in e for e in errors)

    def test_query_adapter_not_in_model(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="recipes", description="recipes", adapters=["sqla"])],
            queries=[
                QueryDef(
                    name="my_query",
                    description="query",
                    model="recipes",
                    adapter="relative_filesystem",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any(
            "adapter 'relative_filesystem' not declared on model 'recipes'" in e for e in errors
        )

    def test_projection_model_without_adapter(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="recipes", description="recipes", adapters=["sqla"])],
            events=[EventDef(name="some_event", description="event")],
            projections=[
                ProjectionDef(
                    name="my_proj",
                    description="proj",
                    event="some_event",
                    model="recipes",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("model declared without adapter" in e for e in errors)

    def test_projection_adapter_without_model(self):
        feat = FeatureDefinition(
            events=[EventDef(name="some_event", description="event")],
            projections=[
                ProjectionDef(
                    name="my_proj",
                    description="proj",
                    event="some_event",
                    adapter="sqla",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any("adapter declared without model" in e for e in errors)

    def test_projection_adapter_not_in_model(self):
        feat = FeatureDefinition(
            models=[ModelDef(name="recipes", description="recipes", adapters=["sqla"])],
            events=[EventDef(name="some_event", description="event")],
            projections=[
                ProjectionDef(
                    name="my_proj",
                    description="proj",
                    event="some_event",
                    model="recipes",
                    adapter="relative_filesystem",
                )
            ],
        )
        errors = validate_feat(feat)
        assert any(
            "adapter 'relative_filesystem' not declared on model 'recipes'" in e for e in errors
        )


class TestDuplicateNames:
    """A name repeated within one section shadows an element every consumer keys on."""

    def test_duplicate_procedure_name(self):
        feat = FeatureDefinition(
            commands=[CommandDef(name="open", description="c")],
            queries=[QueryDef(name="get_recipe", description="q")],
            procedures=[
                ProcedureDef(name="handle", command="open", queries=["get_recipe"]),
                ProcedureDef(name="handle", command="open"),
            ],
        )
        errors = validate_feat(feat)
        assert any("procedures: 'handle' is declared more than once" in e for e in errors)

    def test_same_name_in_two_sections_is_fine(self):
        """Nothing enforces cross-kind uniqueness — (kind, name) is the identity."""
        feat = FeatureDefinition(
            commands=[CommandDef(name="open", description="c")],
            events=[EventDef(name="opened", description="e")],
            procedures=[ProcedureDef(name="ingest", command="open")],
            policies=[PolicyDef(name="ingest", event="opened")],
        )
        assert validate_feat(feat) == []
