"""End-to-end def + gen + lib integration tests."""

import logging
from pathlib import Path

import pytest
from click.exceptions import Exit as ClickExit
from dizzy.cli import DOC_PAGES, app, def_cmd, gen, lib, wiring
from syrupy.assertion import SnapshotAssertion

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_docs_pages_print_content() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()

    default = runner.invoke(app, ["docs"])
    assert default.exit_code == 0
    assert "DIZZY CLI — Manpage & Roadmap" in default.output

    for page in DOC_PAGES:
        result = runner.invoke(app, ["docs", page])
        assert result.exit_code == 0
        assert result.output.strip()

    unknown = runner.invoke(app, ["docs", "nope"])
    assert unknown.exit_code == 1


def test_onboard_prints_agent_overview() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()

    result = runner.invoke(app, ["onboard"])
    assert result.exit_code == 0
    assert "DIZZY Onboarding" in result.output
    # The opinionated-workflow checklist: components, feature-file role,
    # change taxonomy, exemplar events, command-per-lifecycle-step.
    assert "eight components" in result.output
    assert "Change taxonomy" in result.output
    assert "Lossy-to-truth" in result.output
    assert "Exemplar events" in result.output
    assert "Command per lifecycle step" in result.output


def test_generate_subcommands_registered() -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["generate", "--help"])
    assert result.exit_code == 0
    for sub in ("definitions", "static", "libraries"):
        assert sub in result.output


def test_def_creates_def_stubs(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    assert (tmp_path / "def" / "commands.yaml").exists()
    assert (tmp_path / "def" / "events.yaml").exists()
    assert (tmp_path / "def" / "queries" / "get_recipe_text.yaml").exists()
    assert (tmp_path / "def" / "queries" / "get_recipe.yaml").exists()
    assert (tmp_path / "def" / "models" / "recipes.yaml").exists()


def test_def_does_not_overwrite(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    commands_path = tmp_path / "def" / "commands.yaml"
    commands_path.write_text("# custom content\n")

    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    assert commands_path.read_text() == "# custom content\n"
    assert (tmp_path / "def" / "events.yaml").read_text() != "# custom content\n"


def test_gen_creates_all_outputs(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    # gen_def / gen_int are installable packages under lib/python-uv/; their
    # importable roots are nested one level (lib/python-uv/<pkg>/<pkg>/...).
    gen_def = tmp_path / "lib" / "python-uv" / "gen_def" / "gen_def"
    gen_int = tmp_path / "lib" / "python-uv" / "gen_int" / "gen_int"

    # gen_def/ — linkml outputs
    assert (gen_def / "pydantic" / "commands.py").exists()
    assert (gen_def / "pydantic" / "events.py").exists()
    assert (gen_def / "pydantic" / "query" / "get_recipe_text.py").exists()
    assert (gen_def / "pydantic" / "query" / "get_recipe.py").exists()
    assert (gen_def / "pydantic" / "models" / "recipes.py").exists()
    assert (gen_def / "sqla" / "models" / "recipes.py").exists()

    # gen_int/ — protocol outputs
    assert (gen_int / "python" / "query" / "get_recipe_text.py").exists()
    assert (gen_int / "python" / "query" / "get_recipe.py").exists()
    assert (gen_int / "python" / "procedure" / "extract_and_transform_recipe_context.py").exists()
    assert (gen_int / "python" / "procedure" / "extract_and_transform_recipe_protocol.py").exists()
    assert (gen_int / "python" / "policy" / "index_recipe_on_ingest_context.py").exists()
    assert (gen_int / "python" / "policy" / "index_recipe_on_ingest_protocol.py").exists()
    assert (gen_int / "python" / "projection" / "recipe_library_projection.py").exists()

    # No flat src/ tree is generated any more.
    assert not (tmp_path / "src").exists()

    # gen_def / gen_int carry pyproject.toml so they are installable packages.
    assert (tmp_path / "lib" / "python-uv" / "gen_def" / "pyproject.toml").exists()
    assert (tmp_path / "lib" / "python-uv" / "gen_int" / "pyproject.toml").exists()

    # __init__.py in every generated package directory
    assert (gen_def / "__init__.py").exists()
    assert (gen_def / "pydantic" / "__init__.py").exists()
    assert (gen_def / "pydantic" / "query" / "__init__.py").exists()
    assert (gen_def / "pydantic" / "models" / "__init__.py").exists()
    assert (gen_def / "sqla" / "__init__.py").exists()
    assert (gen_def / "sqla" / "models" / "__init__.py").exists()
    assert (gen_int / "__init__.py").exists()
    assert (gen_int / "python" / "__init__.py").exists()
    assert (gen_int / "python" / "query" / "__init__.py").exists()
    assert (gen_int / "python" / "procedure" / "__init__.py").exists()
    assert (gen_int / "python" / "policy" / "__init__.py").exists()
    assert (gen_int / "python" / "projection" / "__init__.py").exists()


def test_gen_error_when_def_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="dizzy"):
        with pytest.raises(ClickExit) as exc_info:
            gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    assert exc_info.value.exit_code == 1
    assert "dizzy generate definitions" in caplog.text
    assert "def/commands.yaml" in caplog.text


def test_def_creates_environment_and_telemetry_stubs(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "agent.feat.yaml", output_dir=tmp_path)

    assert (tmp_path / "def" / "environment.yaml").exists()
    assert (tmp_path / "def" / "telemetry.yaml").exists()


def test_def_omits_environment_telemetry_when_absent(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    assert not (tmp_path / "def" / "environment.yaml").exists()
    assert not (tmp_path / "def" / "telemetry.yaml").exists()


def test_gen_compiles_environment_and_telemetry(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "agent.feat.yaml", output_dir=tmp_path)
    gen(feat_file=FIXTURES_DIR / "agent.feat.yaml", output_dir=tmp_path)

    gen_def = tmp_path / "lib" / "python-uv" / "gen_def" / "gen_def"
    gen_int = tmp_path / "lib" / "python-uv" / "gen_int" / "gen_int"

    assert (gen_def / "pydantic" / "environment.py").exists()
    assert (gen_def / "pydantic" / "telemetry.py").exists()

    # The procedure context imports the compiled env/telemetry shapes.
    ctx = (gen_int / "python" / "procedure" / "run_agent_turn_context.py").read_text()
    assert "from gen_def.pydantic.environment import Model" in ctx
    assert "from gen_def.pydantic.telemetry import StreamChunk" in ctx
    assert "env: run_agent_turn_env" in ctx
    assert "telemetry: run_agent_turn_telemetry" in ctx


def test_def_partial_feat(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "partial.feat.yaml", output_dir=tmp_path)

    assert (tmp_path / "def" / "commands.yaml").exists()
    assert (tmp_path / "def" / "queries" / "find_thing.yaml").exists()
    assert not (tmp_path / "def" / "events.yaml").exists()
    assert not (tmp_path / "def" / "models").exists()


def test_gen_full_example_snapshot(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    generated: dict[str, str] = {}
    for f in sorted(tmp_path.rglob("*")):
        if f.is_file():
            content = f.read_text().replace(str(tmp_path), "<output_dir>")
            generated[str(f.relative_to(tmp_path))] = content

    assert generated == snapshot


def test_gen_emits_no_top_level_src_or_gen_trees(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    # Implementations live in lib/ now; no flat src/ tree, and the type packages
    # live under lib/python-uv/ rather than at the output-dir root.
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / "gen_def").exists()
    assert not (tmp_path / "gen_int").exists()


def test_def_creates_libconfig_yaml(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    libconfig = tmp_path / "libconfig.yaml"
    assert libconfig.exists()
    content = libconfig.read_text()
    assert "python-uv" in content
    assert "extract_and_transform_recipe" in content
    assert "index_recipe_on_ingest" in content


def test_def_does_not_overwrite_libconfig(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    libconfig = tmp_path / "libconfig.yaml"
    libconfig.write_text("# custom content\n")

    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    assert libconfig.read_text() == "# custom content\n"


def test_def_custom_default_runtime(tmp_path: Path) -> None:
    def_cmd(
        feat_file=FIXTURES_DIR / "recipe.feat.yaml",
        output_dir=tmp_path,
        default_runtime="rust-cargo",
    )

    content = (tmp_path / "libconfig.yaml").read_text()
    assert "runtimes: [rust-cargo]" in content
    assert "runtimes: [python-uv]" not in content


def test_lib_error_missing_libconfig(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").unlink()

    with caplog.at_level(logging.ERROR, logger="dizzy"):
        with pytest.raises(ClickExit) as exc_info:
            lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    assert exc_info.value.exit_code == 1
    assert "libconfig.yaml" in caplog.text


def test_lib_python_uv_structure(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    base = tmp_path / "lib" / "python-uv"
    workspace = (base / "pyproject.toml").read_text()
    # The workspace lists the generated type packages and the element packages.
    assert '"gen_def",' in workspace
    assert '"gen_int",' in workspace
    assert '"procedure/extract_and_transform_recipe",' in workspace

    proc_base = base / "procedure" / "extract_and_transform_recipe"
    element = (proc_base / "pyproject.toml").read_text()
    # Element packages declare the type packages as workspace dependencies.
    assert '"gen_def",' in element
    assert '"gen_int",' in element
    assert "gen_def = { workspace = true }" in element
    assert (proc_base / "src" / "extract_and_transform_recipe.py").exists()


def test_wiring_stage_emits_a_workspace_package(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    base = tmp_path / "lib" / "python-uv"
    # `libraries` must NOT list wiring: a member that does not exist yet makes the
    # whole workspace unresolvable.
    assert '"wiring",' not in (base / "pyproject.toml").read_text()

    wiring(
        feat_file=FIXTURES_DIR / "recipe.feat.yaml",
        output_dir=tmp_path,
        dizzy_source=None,  # typer defaults only apply through the CLI
    )

    assert (base / "wiring" / "src" / "wiring.py").exists()
    # The feat travels with the package, so a lifted-out lib/ can read its topology.
    assert (base / "wiring" / "src" / "recipe.feat.yaml").exists()
    workspace = (base / "pyproject.toml").read_text()
    assert '"wiring",' in workspace


def test_the_generated_workspace_names_where_dizzy_comes_from(tmp_path: Path) -> None:
    """DIZZY is not on a package index — the name there is an unrelated project —
    so a generated workspace must always name a source for it."""
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    wiring(
        feat_file=FIXTURES_DIR / "recipe.feat.yaml",
        output_dir=tmp_path,
        dizzy_source=None,  # typer defaults only apply through the CLI
    )

    workspace = (tmp_path / "lib" / "python-uv" / "pyproject.toml").read_text()
    assert "[tool.uv.sources]" in workspace
    assert 'dizzy = { git = "https://github.com/PNNL/dizzy" }' in workspace


def test_dizzy_source_can_point_at_a_checkout(tmp_path: Path) -> None:
    """The usual case when the generated lib lives inside the repo that made it."""
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    wiring(
        feat_file=FIXTURES_DIR / "recipe.feat.yaml",
        output_dir=tmp_path,
        dizzy_source="../../../..",
    )

    workspace = (tmp_path / "lib" / "python-uv" / "pyproject.toml").read_text()
    assert 'dizzy = { path = "../../../..", editable = true }' in workspace


def test_lib_rust_cargo_structure(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    base = tmp_path / "lib" / "rust-cargo"
    assert (base / "Cargo.toml").exists()
    proc_base = base / "procedure" / "extract_and_transform_recipe"
    assert (proc_base / "Cargo.toml").exists()
    assert (proc_base / "src" / "lib.rs").exists()
    assert (base / "gen_def" / "commands.rs").exists()
    assert (base / "gen_def" / "events.rs").exists()


def test_lib_typescript_npm_structure(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    base = tmp_path / "lib" / "typescript-npm"
    assert (base / "package.json").exists()
    assert (base / "tsconfig.json").exists()
    policy_base = base / "policy" / "index_recipe_on_ingest"
    assert (policy_base / "package.json").exists()
    assert (policy_base / "tsconfig.json").exists()
    assert (policy_base / "src" / "index.ts").exists()
    assert (base / "gen_def" / "commands.ts").exists()
    assert (base / "gen_def" / "events.ts").exists()


def test_lib_does_not_overwrite_stubs(tmp_path: Path) -> None:
    def_cmd(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    (tmp_path / "libconfig.yaml").write_text(
        (FIXTURES_DIR / "multiruntime.libconfig.yaml").read_text()
    )
    gen(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    stub = (
        tmp_path
        / "lib"
        / "python-uv"
        / "procedure"
        / "extract_and_transform_recipe"
        / "src"
        / "extract_and_transform_recipe.py"
    )
    stub.write_text("# my implementation\n")

    lib(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)

    assert stub.read_text() == "# my implementation\n"
