"""LibConfig generator — generates libconfig.yaml stub from a FeatureDefinition."""

from pathlib import Path

from dizzy.feat_schema import FeatureDefinition
from dizzy.generators.json_schema import DEFAULT_CONTRACTS
from dizzy.logger import logger


def render_libconfig_stub(feat: FeatureDefinition, default_runtime: str = "python-uv") -> str:
    """Render libconfig.yaml stub content from a FeatureDefinition."""
    lines = [
        "# Dizzy library configuration — assign runtimes to each element",
        "# Supported runtimes: python-uv | rust-cargo | typescript-npm",
        "",
    ]
    for section, items in [
        ("procedures", feat.procedures or []),
        ("policies", feat.policies or []),
        ("queries", feat.queries or []),
        ("projections", feat.projections or []),
    ]:
        if items:
            lines.append(f"{section}:")
            for item in items:
                lines.append(f"  {item.name}:")
                lines.append(f"    runtimes: [{default_runtime}]")
            lines.append("")
    lines += [
        "# Runtime-neutral JSON Schema contracts, emitted by `dizzy generate static`",
        "# into <output_dir>/gen_schema/. Contract kinds: commands | events | queries |",
        "# models. Delete the section to stop emitting them.",
        "json_schema:",
        f"  contracts: [{', '.join(DEFAULT_CONTRACTS)}]",
        "",
    ]
    return "\n".join(lines)


def write_libconfig_stub(
    feat: FeatureDefinition,
    output_dir: Path,
    default_runtime: str = "python-uv",
) -> None:
    """Write libconfig.yaml to output_dir; skip if file already exists."""
    dest = output_dir / "libconfig.yaml"
    if dest.exists():
        logger.debug("skipping %s — already exists", dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_libconfig_stub(feat, default_runtime=default_runtime))
    logger.debug("wrote %s", dest)
