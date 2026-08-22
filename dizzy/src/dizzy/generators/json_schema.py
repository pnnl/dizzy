"""JSON Schema generator — compiles ``def/`` LinkML sources into JSON Schema contracts.

Driven by the ``json_schema`` section of ``libconfig.yaml``:

.. code-block:: yaml

    json_schema:
      contracts: [commands, queries]   # commands | events | queries | models
      output_dir: gen_schema           # relative to the generate output directory

Omitting the section emits nothing, so a ``libconfig.yaml`` written before the
section existed keeps producing byte-identical output. Present-but-empty
(``json_schema: {}``) opts in with the defaults below.

One schema document is emitted per ``def/`` source, mirroring the ``def/`` layout —
so every class in that source (each command, a query's Input *and* Output, …) is
addressable as ``#/$defs/<ClassName>``.
"""

from pathlib import Path

from dizzy.feat_schema import FeatureDefinition
from dizzy.generators.linkml_runner import run_linkml_json_schema
from dizzy.generators.paths import DEFAULT_JSON_SCHEMA_DIR, gen_schema_root
from dizzy.libconfig_schema import LibConfig
from dizzy.logger import logger

#: Emitted when ``json_schema`` is present but ``contracts`` is omitted. Commands and
#: queries are the system's outward-facing contracts — the shapes an HTTP edge, a UI,
#: or another service actually posts and receives — so they are the sensible opt-in
#: default. Events and models are internal and available by asking for them.
DEFAULT_CONTRACTS: tuple[str, ...] = ("commands", "queries")

CONTRACT_KINDS: tuple[str, ...] = ("commands", "events", "queries", "models")


def resolve_json_schema_settings(config: LibConfig | None) -> tuple[list[str], str] | None:
    """Resolve ``(contracts, output_dir)`` from a LibConfig, or None if disabled.

    Returns None when *config* is None or carries no ``json_schema`` section — the
    backwards-compatible default of emitting nothing.
    """
    if config is None or config.json_schema is None:
        return None
    settings = config.json_schema
    contracts = (
        [str(c) for c in settings.contracts] if settings.contracts else list(DEFAULT_CONTRACTS)
    )
    unknown = [c for c in contracts if c not in CONTRACT_KINDS]
    if unknown:
        raise ValueError(
            f"unknown json_schema contract kind(s): {', '.join(sorted(unknown))} "
            f"(expected any of {', '.join(CONTRACT_KINDS)})"
        )
    return contracts, settings.output_dir or DEFAULT_JSON_SCHEMA_DIR


def json_schema_sources(
    feat: FeatureDefinition, output_dir: Path, contracts: list[str]
) -> list[tuple[Path, Path]]:
    """Map the selected contract kinds to ``(def_source, relative_output)`` pairs.

    Output paths are relative to the JSON Schema root and mirror ``def/``.
    """
    def_dir = output_dir / "def"
    pairs: list[tuple[Path, Path]] = []

    if "commands" in contracts and feat.commands:
        pairs.append((def_dir / "commands.yaml", Path("commands.schema.json")))
    if "events" in contracts and feat.events:
        pairs.append((def_dir / "events.yaml", Path("events.schema.json")))
    if "queries" in contracts:
        for query in feat.queries or []:
            pairs.append(
                (
                    def_dir / "queries" / f"{query.name}.yaml",
                    Path("queries") / f"{query.name}.schema.json",
                )
            )
    if "models" in contracts:
        for model in feat.models or []:
            pairs.append(
                (
                    def_dir / "models" / f"{model.name}.yaml",
                    Path("models") / f"{model.name}.schema.json",
                )
            )
    return pairs


def write_json_schemas(
    feat: FeatureDefinition, output_dir: Path, config: LibConfig | None
) -> list[Path]:
    """Emit JSON Schema for the contract kinds selected in libconfig.

    Returns the list of written files (empty when the ``json_schema`` section is absent).
    """
    resolved = resolve_json_schema_settings(config)
    if resolved is None:
        logger.debug("no json_schema section in libconfig — skipping JSON Schema generation")
        return []
    contracts, subdir = resolved

    root = gen_schema_root(output_dir, subdir)
    written: list[Path] = []
    for source, relative in json_schema_sources(feat, output_dir, contracts):
        if not source.exists():
            logger.warning("json_schema: %s not found — skipping", source)
            continue
        dest = root / relative
        run_linkml_json_schema(source, dest)
        written.append(dest)

    logger.debug(
        "generated JSON Schema",
        extra={"count": len(written), "contracts": contracts, "output_dir": str(root)},
    )
    return written
