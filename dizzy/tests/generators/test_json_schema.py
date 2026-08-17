"""Tests for the JSON Schema generator (libconfig-driven contract emission)."""

import json
import textwrap
from pathlib import Path

import pytest
from dizzy.generators.json_schema import (
    DEFAULT_CONTRACTS,
    json_schema_sources,
    resolve_json_schema_settings,
    write_json_schemas,
)
from dizzy.libconfig_loader import load_libconfig

from tests.conftest import FIXTURES_DIR


def _config(tmp_path: Path, content: str):
    p = tmp_path / "libconfig.yaml"
    p.write_text(textwrap.dedent(content))
    return load_libconfig(p)


# --- settings resolution -------------------------------------------------------


def test_absent_section_disables_generation(tmp_path: Path) -> None:
    """A libconfig predating the json_schema section emits nothing — no behaviour change."""
    config = _config(
        tmp_path,
        """
        procedures:
          my_proc:
            runtimes: [python-uv]
        """,
    )
    assert resolve_json_schema_settings(config) is None


def test_no_config_at_all_disables_generation() -> None:
    assert resolve_json_schema_settings(None) is None


def test_present_but_empty_uses_defaults(tmp_path: Path) -> None:
    config = _config(tmp_path, "json_schema: {}\n")
    resolved = resolve_json_schema_settings(config)
    assert resolved is not None
    contracts, output_dir = resolved
    assert contracts == list(DEFAULT_CONTRACTS)
    assert output_dir == "gen_schema"


def test_explicit_contracts_and_output_dir(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """
        json_schema:
          contracts: [events, models]
          output_dir: contracts/json
        """,
    )
    resolved = resolve_json_schema_settings(config)
    assert resolved is not None
    contracts, output_dir = resolved
    assert contracts == ["events", "models"]
    assert output_dir == "contracts/json"


def test_unknown_contract_kind_is_rejected(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _config(tmp_path, "json_schema:\n  contracts: [not-a-contract]\n")


# --- source mapping ------------------------------------------------------------


def test_sources_mirror_the_def_layout(recipe_feat, tmp_path: Path) -> None:
    pairs = json_schema_sources(recipe_feat, tmp_path, ["commands", "events", "queries", "models"])
    rel = {str(dest) for _, dest in pairs}
    assert "commands.schema.json" in rel
    assert "events.schema.json" in rel
    assert "queries/get_recipe.schema.json" in rel
    assert "models/recipes.schema.json" in rel
    for source, _ in pairs:
        assert source.is_relative_to(tmp_path / "def")


def test_sources_honour_the_selected_contracts(recipe_feat, tmp_path: Path) -> None:
    pairs = json_schema_sources(recipe_feat, tmp_path, ["commands"])
    assert [str(dest) for _, dest in pairs] == ["commands.schema.json"]


def test_partial_feat_skips_absent_kinds(partial_feat, tmp_path: Path) -> None:
    """partial.feat.yaml has no events/models — those emit nothing."""
    pairs = json_schema_sources(partial_feat, tmp_path, ["commands", "events", "queries", "models"])
    rel = {str(dest) for _, dest in pairs}
    assert "events.schema.json" not in rel
    assert not any(d.startswith("models/") for d in rel)


# --- end-to-end emission -------------------------------------------------------


@pytest.fixture
def generated(tmp_path: Path, recipe_feat):
    """Run `generate definitions` so def/ exists, then hand back (feat, output_dir)."""
    from dizzy.cli import def_cmd

    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    return recipe_feat, tmp_path


def test_write_json_schemas_emits_valid_json_schema(generated) -> None:
    """Every emitted document must be a JSON Schema a validator will accept."""
    # jsonschema arrives as a hard dependency of linkml itself.
    from jsonschema.validators import validator_for

    feat, out = generated
    config = _config(out, "json_schema:\n  contracts: [commands, queries, events, models]\n")
    written = write_json_schemas(feat, out, config)

    assert written, "expected schemas to be written"
    for path in written:
        doc = json.loads(path.read_text())
        assert doc["$schema"].startswith("https://json-schema.org/draft/")
        validator_for(doc).check_schema(doc)


def test_commands_land_in_defs(generated) -> None:
    feat, out = generated
    config = _config(out, "json_schema:\n  contracts: [commands]\n")
    (path,) = write_json_schemas(feat, out, config)

    assert path == out / "gen_schema" / "commands.schema.json"
    doc = json.loads(path.read_text())
    # One $def per command in the feature-file.
    assert len(doc["$defs"]) == len(feat.commands)


def test_query_schema_carries_input_and_output(generated) -> None:
    feat, out = generated
    config = _config(out, "json_schema:\n  contracts: [queries]\n")
    write_json_schemas(feat, out, config)

    doc = json.loads((out / "gen_schema" / "queries" / "get_recipe.schema.json").read_text())
    assert "GetRecipeInput" in doc["$defs"]
    assert "GetRecipeOutput" in doc["$defs"]


def test_a_payload_validates_against_a_def(generated) -> None:
    """The point of the schemas: validate a real payload via $ref into $defs."""
    from jsonschema import Draft201909Validator
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

    feat, out = generated
    config = _config(out, "json_schema:\n  contracts: [commands]\n")
    (path,) = write_json_schemas(feat, out, config)
    doc = json.loads(path.read_text())

    class_name = next(iter(doc["$defs"]))
    schema = {**doc, "$ref": f"#/$defs/{class_name}"}
    validator = Draft201909Validator(schema)
    validator.validate({})  # every recipe command slot is optional

    with pytest.raises(JsonSchemaValidationError):
        validator.validate({"definitely_not_a_declared_slot": 1})


def test_output_dir_is_configurable(generated) -> None:
    feat, out = generated
    config = _config(out, "json_schema:\n  contracts: [commands]\n  output_dir: contracts/json\n")
    (path,) = write_json_schemas(feat, out, config)
    assert path == out / "contracts" / "json" / "commands.schema.json"


def test_disabled_config_writes_nothing(generated) -> None:
    feat, out = generated
    config = _config(out, "procedures: {}\n")
    assert write_json_schemas(feat, out, config) == []
    assert not (out / "gen_schema").exists()
