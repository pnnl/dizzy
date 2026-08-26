"""Feat file loader — YAML ingestion, Pydantic validation, and cross-reference checks."""

from pathlib import Path

import yaml

from dizzy.feat_schema import (
    FeatureDefinition,
)
from dizzy.logger import logger


def _normalize_section(raw: dict, section: str) -> None:
    """Mutate *raw* in-place so every entry in *section* is a dict with a ``name`` key."""
    if section not in raw or not isinstance(raw[section], dict):
        return
    normalised: list[dict] = []
    for key, value in raw[section].items():
        if isinstance(value, str):
            entry = {"name": key, "description": value}
        else:
            entry = {**value, "name": key}
        normalised.append(entry)
    raw[section] = normalised


_SECTIONS = (
    "models",
    "queries",
    "commands",
    "events",
    "procedures",
    "policies",
    "projections",
    "environment",
    "telemetry",
)


def _normalize(raw: dict) -> dict:
    """Convert the user-facing YAML dict-key format into the list format expected by Pydantic."""
    result = dict(raw)
    for section in _SECTIONS:
        _normalize_section(result, section)
    return result


def load_feat(path: str | Path) -> FeatureDefinition:
    """Load a ``.feat.yaml`` file, validate it, and return a FeatureDefinition."""
    raw = yaml.safe_load(Path(path).read_text())
    logger.debug("loaded feat", extra={"path": str(path)})
    normalised = _normalize(raw)
    return FeatureDefinition.model_validate(normalised)


def validate_feat(feat: FeatureDefinition) -> list[str]:
    """Validate cross-references within a FeatureDefinition.

    Returns a list of error strings; empty list means the feat is valid.
    """
    errors: list[str] = []

    # Names are unique within a section, and nothing downstream can recover if they are
    # not: every consumer keys on (kind, name) — the topology loader's placement maps, the
    # generated package's symbols — so a repeat silently shadows one of the two elements.
    # The dict-key YAML form makes it unrepresentable; the list form does not.
    for section in _SECTIONS:
        seen_names: set[str] = set()
        for item in getattr(feat, section, None) or []:
            if item.name in seen_names:
                errors.append(
                    f"{section}: '{item.name}' is declared more than once "
                    "— names must be unique within a section"
                )
            seen_names.add(item.name)

    command_names = {c.name for c in feat.commands or []}
    event_names = {e.name for e in feat.events or []}
    query_names = {q.name for q in feat.queries or []}
    model_names = {m.name for m in feat.models or []}
    models_by_name = {m.name: m for m in feat.models or []}
    environment_names = {e.name for e in feat.environment or []}
    telemetry_names = {t.name for t in feat.telemetry or []}

    def _check_extras(kind: str, item) -> None:
        """Validate environment/telemetry references on a function definition."""
        for name in getattr(item, "environment", None) or []:
            if name not in environment_names:
                errors.append(
                    f"{kind} '{item.name}': environment '{name}' not declared in environment"
                )
        for name in getattr(item, "telemetry", None) or []:
            if name not in telemetry_names:
                errors.append(f"{kind} '{item.name}': telemetry '{name}' not declared in telemetry")

    for proc in feat.procedures or []:
        if proc.command not in command_names:
            errors.append(
                f"procedure '{proc.name}': command '{proc.command}' not declared in commands"
            )
        for q in proc.queries or []:
            if q not in query_names:
                errors.append(f"procedure '{proc.name}': query '{q}' not declared in queries")
        for e in proc.emits or []:
            if e not in event_names:
                errors.append(f"procedure '{proc.name}': emits '{e}' not declared in events")
        _check_extras("procedure", proc)

    for policy in feat.policies or []:
        if policy.event not in event_names:
            errors.append(f"policy '{policy.name}': event '{policy.event}' not declared in events")
        for q in policy.queries or []:
            if q not in query_names:
                errors.append(f"policy '{policy.name}': query '{q}' not declared in queries")
        for e in policy.emits or []:
            if e not in command_names:
                errors.append(f"policy '{policy.name}': emits '{e}' not declared in commands")
        _check_extras("policy", policy)

    for proj in feat.projections or []:
        if proj.event not in event_names:
            errors.append(f"projection '{proj.name}': event '{proj.event}' not declared in events")
        if proj.model is not None and proj.model not in model_names:
            errors.append(f"projection '{proj.name}': model '{proj.model}' not declared in models")
        _check_extras("projection", proj)

    for query in feat.queries or []:
        _check_extras("querier", query)
        if query.model is not None and query.model not in model_names:
            errors.append(f"query '{query.name}': model '{query.model}' not declared in models")
        if query.model is not None and query.adapter is None:
            errors.append(f"query '{query.name}': model declared without adapter")
        if query.adapter is not None and query.model is None:
            errors.append(f"query '{query.name}': adapter declared without model")
        if (
            query.model is not None
            and query.adapter is not None
            and query.model in models_by_name
            and query.adapter not in (models_by_name[query.model].adapters or [])
        ):
            errors.append(
                f"query '{query.name}': adapter '{query.adapter}' "
                f"not declared on model '{query.model}'"
            )

    for proj in feat.projections or []:
        if proj.model is not None and proj.adapter is None:
            errors.append(f"projection '{proj.name}': model declared without adapter")
        if proj.adapter is not None and proj.model is None:
            errors.append(f"projection '{proj.name}': adapter declared without model")
        if (
            proj.model is not None
            and proj.adapter is not None
            and proj.model in models_by_name
            and proj.adapter not in (models_by_name[proj.model].adapters or [])
        ):
            errors.append(
                f"projection '{proj.name}': adapter '{proj.adapter}' "
                f"not declared on model '{proj.model}'"
            )

    logger.debug("validated feat", extra={"errors": len(errors)})
    return errors
