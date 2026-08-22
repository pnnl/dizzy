"""Dizzy — feature file generator CLI."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from dizzy.config import CONFIG_TEMPLATE, load_config
from dizzy.feat_loader import load_feat, validate_feat
from dizzy.generators.adapters import write_adapter
from dizzy.generators.commands import write_scaffold_commands
from dizzy.generators.environment import write_scaffold_environment
from dizzy.generators.events import write_scaffold_events
from dizzy.generators.init_emitter import write_init_files
from dizzy.generators.json_schema import write_json_schemas
from dizzy.generators.lib_python_uv import (
    write_policy_python_uv,
    write_procedure_python_uv,
    write_projection_python_uv,
    write_query_python_uv,
    write_workspace_python_uv,
)
from dizzy.generators.lib_rust_cargo import (
    write_policy_rust_cargo,
    write_procedure_rust_cargo,
    write_projection_rust_cargo,
    write_query_rust_cargo,
    write_workspace_rust_cargo,
)
from dizzy.generators.lib_typescript_npm import (
    write_policy_typescript_npm,
    write_procedure_typescript_npm,
    write_projection_typescript_npm,
    write_query_typescript_npm,
    write_workspace_typescript_npm,
)
from dizzy.generators.libconfig import write_libconfig_stub
from dizzy.generators.linkml_runner import (
    run_linkml_pydantic,
    run_linkml_rust,
    run_linkml_sqla,
    run_linkml_typescript,
)
from dizzy.generators.models import write_scaffold_model
from dizzy.generators.paths import gen_def_root
from dizzy.generators.policies import (
    write_policy_context,
    write_policy_protocol,
)
from dizzy.generators.procedures import (
    write_procedure_context,
    write_procedure_protocol,
)
from dizzy.generators.projections import write_projection
from dizzy.generators.queries import (
    write_gen_query_protocol,
    write_scaffold_query,
)
from dizzy.generators.telemetry import write_scaffold_telemetry
from dizzy.generators.type_packages import write_type_packages
from dizzy.generators.wiring import write_wiring_python_uv
from dizzy.libconfig_loader import load_libconfig, validate_libconfig
from dizzy.logger import logger, setup_logging
from dizzy.simulate.session import Session
from dizzy.simulate.sim_executors import SimPolicyExecutor, SimProcedureExecutor

app = typer.Typer()

generate_app = typer.Typer(help="Feature-file → schemas → typed contracts → per-runtime libraries.")
app.add_typer(generate_app, name="generate")


@generate_app.command("definitions")
def def_cmd(
    feat_file: Annotated[Path, typer.Argument(help="Path to the .feat.yaml file")],
    output_dir: Annotated[Path, typer.Argument(help="Output directory for generated files")],
    default_runtime: Annotated[
        str,
        typer.Option(help="Default runtime assigned to all elements in libconfig.yaml"),
    ] = "python-uv",
) -> None:
    """Generate def/ stub files from a .feat.yaml feature definition."""
    feat = load_feat(feat_file)

    errors = validate_feat(feat)
    if errors:
        for err in errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    if feat.commands:
        write_scaffold_commands(feat.commands, output_dir)

    if feat.events:
        write_scaffold_events(feat.events, output_dir)

    if feat.environment:
        write_scaffold_environment(feat.environment, output_dir)

    if feat.telemetry:
        write_scaffold_telemetry(feat.telemetry, output_dir)

    for query in feat.queries or []:
        write_scaffold_query(query, output_dir)

    for model in feat.models or []:
        write_scaffold_model(model, output_dir)

    write_libconfig_stub(feat, output_dir, default_runtime=default_runtime)

    logger.info("Generated def/ stubs and libconfig.yaml. Next steps:")
    logger.info("  1. Fill in class definitions in def/models/*.yaml")
    logger.info("  2. Add input/output shapes in def/queries/*.yaml")
    logger.info("  3. Add attributes to def/commands.yaml and def/events.yaml")
    logger.info("  4. Review runtimes in libconfig.yaml")
    logger.info("  5. Run: dizzy generate static <feat_file> <output_dir>")
    logger.info("  6. Run: dizzy generate libraries <feat_file> <output_dir>")


@generate_app.command("static")
def gen(
    feat_file: Path = typer.Argument(..., help="Path to the .feat.yaml file"),
    output_dir: Path = typer.Argument(..., help="Output directory for generated files"),
) -> None:
    """Generate the gen_def/ and gen_int/ type packages under lib/python-uv/ from def/."""
    feat = load_feat(feat_file)

    errors = validate_feat(feat)
    if errors:
        for err in errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    # Guard: check that all required def/ stubs exist before proceeding
    missing: list[str] = []
    if feat.commands and not (output_dir / "def" / "commands.yaml").exists():
        missing.append("def/commands.yaml")
    if feat.events and not (output_dir / "def" / "events.yaml").exists():
        missing.append("def/events.yaml")
    if feat.environment and not (output_dir / "def" / "environment.yaml").exists():
        missing.append("def/environment.yaml")
    if feat.telemetry and not (output_dir / "def" / "telemetry.yaml").exists():
        missing.append("def/telemetry.yaml")
    for query in feat.queries or []:
        stub = output_dir / "def" / "queries" / f"{query.name}.yaml"
        if not stub.exists():
            missing.append(f"def/queries/{query.name}.yaml")
    for model in feat.models or []:
        stub = output_dir / "def" / "models" / f"{model.name}.yaml"
        if not stub.exists():
            missing.append(f"def/models/{model.name}.yaml")

    if missing:
        logger.error(
            "def/ stubs not found. Run `dizzy generate definitions <feat_file> <output_dir>` first."
        )
        for path in missing:
            logger.error("  missing: %s", path)
        raise typer.Exit(code=1)

    # Step 1 — run LinkML toolchain on def/ stubs → lib/python-uv/gen_def/
    gen_def = gen_def_root(output_dir)
    if feat.commands:
        run_linkml_pydantic(
            output_dir / "def" / "commands.yaml",
            gen_def / "pydantic" / "commands.py",
        )

    if feat.events:
        run_linkml_pydantic(
            output_dir / "def" / "events.yaml",
            gen_def / "pydantic" / "events.py",
        )

    if feat.environment:
        run_linkml_pydantic(
            output_dir / "def" / "environment.yaml",
            gen_def / "pydantic" / "environment.py",
        )

    if feat.telemetry:
        run_linkml_pydantic(
            output_dir / "def" / "telemetry.yaml",
            gen_def / "pydantic" / "telemetry.py",
        )

    for query in feat.queries or []:
        run_linkml_pydantic(
            output_dir / "def" / "queries" / f"{query.name}.yaml",
            gen_def / "pydantic" / "query" / f"{query.name}.py",
        )

    for model in feat.models or []:
        run_linkml_pydantic(
            output_dir / "def" / "models" / f"{model.name}.yaml",
            gen_def / "pydantic" / "models" / f"{model.name}.py",
        )
        if "sqla" in (model.adapters or []):
            run_linkml_sqla(
                output_dir / "def" / "models" / f"{model.name}.yaml",
                gen_def / "sqla" / "models" / f"{model.name}.py",
            )

    # Step 2 — generate adapter classes
    unique_adapters: set[str] = set()
    for model in feat.models or []:
        unique_adapters.update(model.adapters or [])
    for adapter_name in unique_adapters:
        write_adapter(adapter_name, output_dir)

    # Step 3 — generate gen_int/ Protocol files from feat structure
    for query in feat.queries or []:
        write_gen_query_protocol(query, output_dir)

    for proc in feat.procedures or []:
        write_procedure_context(proc, output_dir)
        write_procedure_protocol(proc, output_dir)

    for policy in feat.policies or []:
        write_policy_context(policy, output_dir)
        write_policy_protocol(policy, output_dir)

    for proj in feat.projections or []:
        write_projection(proj, output_dir)

    # Step 4 — write __init__.py in every generated directory
    write_init_files(output_dir)

    # Step 5 — write pyproject.toml for the gen_def / gen_int type packages
    write_type_packages(output_dir)

    # Step 6 — runtime-neutral JSON Schema contracts, if libconfig asks for them.
    # libconfig is optional here: `generate static` has never required it, and a
    # config without a `json_schema` section emits nothing.
    libconfig_path = output_dir / "libconfig.yaml"
    config = load_libconfig(libconfig_path) if libconfig_path.exists() else None
    try:
        schemas = write_json_schemas(feat, output_dir, config)
    except ValueError as err:
        logger.error("%s", err)
        raise typer.Exit(code=1) from err
    if schemas:
        logger.info("Generated %d JSON Schema contract(s).", len(schemas))

    logger.info("Generated lib/python-uv/gen_def and lib/python-uv/gen_int type packages.")
    logger.info(
        "  Run: dizzy generate libraries <feat_file> <output_dir> to generate element packages."
    )


@generate_app.command("libraries")
def lib(
    feat_file: Path = typer.Argument(..., help="Path to the .feat.yaml file"),
    output_dir: Path = typer.Argument(..., help="Output directory (must contain libconfig.yaml)"),
) -> None:
    """Generate lib/ runtime packages from libconfig.yaml."""
    feat = load_feat(feat_file)

    errors = validate_feat(feat)
    if errors:
        for err in errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    libconfig_path = output_dir / "libconfig.yaml"
    if not libconfig_path.exists():
        logger.error("libconfig.yaml not found. Run `dizzy generate definitions` first.")
        raise typer.Exit(code=1)

    config = load_libconfig(libconfig_path)
    config_errors = validate_libconfig(config, feat)
    if config_errors:
        for err in config_errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    # Determine which runtimes have at least one element assigned
    active_runtimes: set[str] = set()
    for section in [config.procedures, config.policies, config.queries, config.projections]:
        for binding in section or []:
            for rt in binding.runtimes or []:
                active_runtimes.add(str(rt))

    # Run LinkML type generation for each active non-Python runtime
    def_dir = output_dir / "def"

    def _run_rust_gen(schema: Path, category: str, name: str) -> None:
        if schema.exists():
            run_linkml_rust(
                schema,
                output_dir / "lib" / "rust-cargo" / "gen_def" / category / f"{name}.rs",
            )

    def _run_ts_gen(schema: Path, category: str, name: str) -> None:
        if schema.exists():
            run_linkml_typescript(
                schema,
                output_dir / "lib" / "typescript-npm" / "gen_def" / category / f"{name}.ts",
            )

    if "rust-cargo" in active_runtimes:
        _run_rust_gen(def_dir / "commands.yaml", ".", "commands")
        _run_rust_gen(def_dir / "events.yaml", ".", "events")
        for model in feat.models or []:
            _run_rust_gen(def_dir / "models" / f"{model.name}.yaml", "models", model.name)
        for query in feat.queries or []:
            _run_rust_gen(def_dir / "queries" / f"{query.name}.yaml", "queries", query.name)

    if "typescript-npm" in active_runtimes:
        _run_ts_gen(def_dir / "commands.yaml", ".", "commands")
        _run_ts_gen(def_dir / "events.yaml", ".", "events")
        for model in feat.models or []:
            _run_ts_gen(def_dir / "models" / f"{model.name}.yaml", "models", model.name)
        for query in feat.queries or []:
            _run_ts_gen(def_dir / "queries" / f"{query.name}.yaml", "queries", query.name)

    # Build element dispatch tables per runtime
    runtime_members: dict[str, list[tuple[str, str]]] = {
        "python-uv": [],
        "rust-cargo": [],
        "typescript-npm": [],
    }

    _writers: dict[str, dict[str, Callable[..., Any]]] = {
        "python-uv": {
            "procedure": write_procedure_python_uv,
            "policy": write_policy_python_uv,
            "query": write_query_python_uv,
            "projection": write_projection_python_uv,
        },
        "rust-cargo": {
            "procedure": write_procedure_rust_cargo,
            "policy": write_policy_rust_cargo,
            "query": write_query_rust_cargo,
            "projection": write_projection_rust_cargo,
        },
        "typescript-npm": {
            "procedure": write_procedure_typescript_npm,
            "policy": write_policy_typescript_npm,
            "query": write_query_typescript_npm,
            "projection": write_projection_typescript_npm,
        },
    }

    for kind, bindings, feat_items in [
        ("procedure", config.procedures or [], feat.procedures or []),
        ("policy", config.policies or [], feat.policies or []),
        ("query", config.queries or [], feat.queries or []),
        ("projection", config.projections or [], feat.projections or []),
    ]:
        feat_by_name = {item.name: item for item in feat_items}
        for binding in bindings:
            element_def = feat_by_name[binding.name]
            for rt in binding.runtimes or []:
                runtime = str(rt)
                runtime_members[runtime].append((kind, binding.name))
                _writers[runtime][kind](element_def, output_dir)

    # Write workspace manifests for each active runtime
    for runtime, members in runtime_members.items():
        if not members:
            continue
        if runtime == "python-uv":
            write_workspace_python_uv(members, output_dir)
        elif runtime == "rust-cargo":
            write_workspace_rust_cargo(members, output_dir)
        elif runtime == "typescript-npm":
            write_workspace_typescript_npm(members, output_dir)

    logger.info("Generated lib/ packages. Implement the stubs in lib/<runtime>/<kind>/<name>/src/")


@generate_app.command("wiring")
def wiring(
    feat_file: Path = typer.Argument(..., help="Path to the .feat.yaml file"),
    output_dir: Path = typer.Argument(..., help="Output directory (must contain libconfig.yaml)"),
    dizzy_source: str | None = typer.Option(
        None,
        "--dizzy-source",
        help=(
            "Where the generated workspace gets DIZZY: a checkout path (relative to "
            "lib/<runtime>/) or a git URL. DIZZY is not published to a package index, "
            "so this defaults to the canonical repository."
        ),
    ),
) -> None:
    """Generate lib/<runtime>/wiring/ — the binding from elements to the runtime."""
    feat = load_feat(feat_file)

    errors = validate_feat(feat)
    if errors:
        for err in errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    libconfig_path = output_dir / "libconfig.yaml"
    if not libconfig_path.exists():
        logger.error("libconfig.yaml not found. Run `dizzy generate definitions` first.")
        raise typer.Exit(code=1)

    config = load_libconfig(libconfig_path)
    config_errors = validate_libconfig(config, feat)
    if config_errors:
        for err in config_errors:
            logger.error("%s", err)
        raise typer.Exit(code=1)

    # The wiring imports every element package, so it can only be generated for a
    # runtime that has them. Today that is python-uv; rust/ts stop at types + stubs.
    members: list[tuple[str, str]] = []
    for kind, bindings in [
        ("procedure", config.procedures or []),
        ("policy", config.policies or []),
        ("query", config.queries or []),
        ("projection", config.projections or []),
    ]:
        for binding in bindings:
            if "python-uv" in [str(rt) for rt in binding.runtimes or []]:
                members.append((kind, binding.name))

    if not members:
        logger.error(
            "No python-uv elements in libconfig.yaml — nothing to wire. "
            "Run `dizzy generate libraries` first."
        )
        raise typer.Exit(code=1)

    write_wiring_python_uv(feat, feat_file, output_dir)
    # Re-emit the manifest with the wiring package as a member. Listing it before
    # it exists would make the whole workspace unresolvable, so `libraries` leaves
    # it out and this stage adds it.
    write_workspace_python_uv(members, output_dir, include_wiring=True, dizzy_source=dizzy_source)
    logger.info(
        "Generated lib/python-uv/wiring/. Build a HostApp with "
        "wiring.host_app(...) and point $DIZZY_HOST_APP at it."
    )


@app.command()
def simulate(
    feat_file: Annotated[Path, typer.Argument(help="Path to the .feat.yaml file")],
    scenario_file: Annotated[Path, typer.Argument(help="Path to the .scenario.yaml file")],
    session_path: Annotated[Path, typer.Argument(help="Output session JSONL file")] = Path(
        "session.jsonl"
    ),
    provider: Annotated[
        str, typer.Option(help="LLM provider: openrouter | ollama | unsloth")
    ] = "openrouter",
    model: Annotated[
        str | None, typer.Option(help="Model override (provider default if omitted)")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Stream LLM output")] = False,
) -> None:
    """LLM-driven execution of a feature-file against a scenario (level 0)."""
    session = Session(session_path).load_features(feat_file)
    procedure_executor = SimProcedureExecutor(
        session._feature, provider=provider, model=model, verbose_stream=verbose
    )
    policy_executor = SimPolicyExecutor(
        session._feature, provider=provider, model=model, verbose_stream=verbose
    )
    session.run_scenario(scenario_file, procedure_executor, policy_executor)
    findings = [e for e in session._session if e["type"] == "finding"]
    if findings:
        logger.info("%d finding(s) recorded — see %s", len(findings), session_path)
    else:
        logger.info("Run complete, no findings. Session: %s", session_path)


@app.command()
def config() -> None:
    """Print a template dizzy configuration file."""
    typer.echo(CONFIG_TEMPLATE, nl=False)


DOC_PAGES = {
    "cli": "cli.md",  # manpage + roadmap (canonical: dizzy/src/dizzy/docs/cli.md)
    "authoring": "authoring.md",  # agent guide (canonical: dizzy/src/dizzy/docs/authoring.md)
}


def _print_doc(filename: str) -> None:
    """Print a packaged markdown doc: rich-rendered on a tty, plain when piped."""
    import sys
    from importlib.resources import files

    content = files("dizzy").joinpath("docs", filename).read_text()
    if sys.stdout.isatty():
        from rich.console import Console
        from rich.markdown import Markdown

        Console().print(Markdown(content))
    else:
        typer.echo(content)


@app.command()
def docs(
    page: Annotated[
        str,
        typer.Argument(help=f"Page to print: {' | '.join(DOC_PAGES)}"),
    ] = "cli",
) -> None:
    """Print Dizzy documentation (default: the CLI manpage & roadmap)."""
    if page not in DOC_PAGES:
        logger.error("Unknown docs page %r. Available pages: %s", page, ", ".join(DOC_PAGES))
        raise typer.Exit(code=1)

    _print_doc(DOC_PAGES[page])


@app.command()
def onboard() -> None:
    """Print the agent-facing DIZZY overview: components, feature-file role,
    change taxonomy, exemplar events, and which command fits each lifecycle step."""
    _print_doc("onboard.md")


# Deprecated aliases, kept for compatibility with older docs and scripts.
app.command("def", hidden=True, help="(deprecated) Alias of `generate definitions`.")(def_cmd)
app.command("gen", hidden=True, help="(deprecated) Alias of `generate static`.")(gen)
app.command("lib", hidden=True, help="(deprecated) Alias of `generate libraries`.")(lib)
app.command("doc", hidden=True, help="Alias of `docs`.")(docs)


def main() -> None:
    config = load_config()
    setup_logging(
        log_dir=config.logging.log_dir,
        show_level=config.logging.show_level,
        gitignore=config.logging.gitignore,
    )
    app()


if __name__ == "__main__":
    main()
