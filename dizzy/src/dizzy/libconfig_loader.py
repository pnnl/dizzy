"""LibConfig loader — YAML ingestion, Pydantic validation, and cross-reference checks."""

from pathlib import Path

import yaml

from dizzy.feat_schema import FeatureDefinition
from dizzy.libconfig_schema import LibConfig
from dizzy.logger import logger

_SECTIONS = ("procedures", "policies", "queries", "projections")


def _normalize_section(raw: dict, section: str) -> None:
    """Mutate *raw* in-place so every entry in *section* is a dict with a ``name`` key."""
    if section not in raw or not isinstance(raw[section], dict):
        return
    normalised: list[dict] = []
    for key, value in raw[section].items():
        if isinstance(value, dict):
            entry = {**value, "name": key}
        elif isinstance(value, list):
            entry = {"name": key, "runtimes": value}
        else:
            entry = {"name": key, "runtimes": []}
        normalised.append(entry)
    raw[section] = normalised


def _normalize(raw: dict) -> dict:
    """Convert the user-facing YAML dict-key format into the list format expected by Pydantic.

    Absent element sections are materialised as empty lists so callers never have to
    distinguish "omitted" from "empty" — LinkML's pydantic generator stopped defaulting
    optional multivalued slots to ``[]`` in 1.11, and this keeps the loader's contract
    independent of that.
    """
    result = dict(raw)
    for section in _SECTIONS:
        _normalize_section(result, section)
        result.setdefault(section, [])
    return result


def load_libconfig(path: str | Path) -> LibConfig:
    """Load a ``libconfig.yaml`` file, validate it, and return a LibConfig."""
    raw = yaml.safe_load(Path(path).read_text())
    logger.debug("loaded libconfig", extra={"path": str(path)})
    if raw is None:
        raw = {}
    normalised = _normalize(raw)
    return LibConfig.model_validate(normalised)


def validate_libconfig(config: LibConfig, feat: FeatureDefinition) -> list[str]:
    """Validate that all element names in config exist in feat.

    Returns a list of error strings; empty list means the config is valid.
    """
    errors: list[str] = []

    proc_names = {p.name for p in feat.procedures or []}
    policy_names = {p.name for p in feat.policies or []}
    query_names = {q.name for q in feat.queries or []}
    proj_names = {p.name for p in feat.projections or []}

    for binding in config.procedures or []:
        if binding.name not in proc_names:
            errors.append(f"libconfig procedure '{binding.name}' not declared in feat")
    for binding in config.policies or []:
        if binding.name not in policy_names:
            errors.append(f"libconfig policy '{binding.name}' not declared in feat")
    for binding in config.queries or []:
        if binding.name not in query_names:
            errors.append(f"libconfig query '{binding.name}' not declared in feat")
    for binding in config.projections or []:
        if binding.name not in proj_names:
            errors.append(f"libconfig projection '{binding.name}' not declared in feat")

    logger.debug("validated libconfig", extra={"errors": len(errors)})
    return errors
