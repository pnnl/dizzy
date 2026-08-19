"""Python-uv runtime generator — generates lib/python-uv/ package structure."""

from pathlib import Path

from dizzy.feat_schema import PolicyDef, ProcedureDef, ProjectionDef, QueryDef
from dizzy.generators.policies import render_src_policy_stub
from dizzy.generators.procedures import render_src_procedure_stub
from dizzy.generators.projections import render_src_projection_stub
from dizzy.generators.queries import render_src_query_stub
from dizzy.logger import logger


def render_element_pyproject_toml(kind: str, name: str) -> str:
    """Element package manifest — depends on the gen_def/gen_int workspace packages.

    The implementation module lives at ``src/<name>.py`` and is shipped as the
    top-level module ``<name>`` (``[tool.hatch.build]`` strips the ``src`` prefix).
    """
    return "\n".join(
        [
            "[project]",
            f'name = "{kind}-{name}"',
            'version = "0.1.0"',
            'requires-python = ">=3.11"',
            "dependencies = [",
            '    "gen_def",',
            '    "gen_int",',
            "]",
            "",
            "[tool.uv.sources]",
            "gen_def = { workspace = true }",
            "gen_int = { workspace = true }",
            "",
            "[build-system]",
            'requires = ["hatchling"]',
            'build-backend = "hatchling.build"',
            "",
            "[tool.hatch.build.targets.wheel]",
            'sources = ["src"]',
            f'include = ["src/{name}.py"]',
            "",
        ]
    )


# Type packages emitted by `dizzy gen`; listed first so they resolve as workspace deps.
_TYPE_PACKAGE_MEMBERS = ["gen_def", "gen_int"]

DIZZY_GIT_URL = "https://github.com/PNNL/dizzy"
"""Where a generated workspace gets DIZZY from.

**DIZZY is not published to a package index, by decision.** A bare ``dizzy``
requirement would resolve to an unrelated project of the same name, so the
generated workspace always names a source explicitly — git by default, or a
local checkout via ``--dizzy-source``.
"""


def _dizzy_source_entry(spec: str | None) -> str:
    """Render the ``[tool.uv.sources]`` value for the DIZZY dependency.

    A *spec* that looks like a URL becomes a git source; anything else is taken
    as a path to a checkout (relative to this manifest) and made editable, which
    is what a lib generated inside the DIZZY repo itself wants.
    """
    if spec is None:
        return f'dizzy = {{ git = "{DIZZY_GIT_URL}" }}'
    if spec.startswith(("http://", "https://", "git+", "ssh://", "git@")):
        return f'dizzy = {{ git = "{spec.removeprefix("git+")}" }}'
    return f'dizzy = {{ path = "{spec}", editable = true }}'


def render_workspace_pyproject_toml(
    members: list[tuple[str, str]],
    include_wiring: bool = False,
    dizzy_source: str | None = None,
) -> str:
    """The uv workspace manifest.

    *include_wiring* adds the generated wiring package, which `dizzy generate
    wiring` emits as a fourth stage. It is opt-in because listing a member that
    has not been generated makes the whole workspace unresolvable. It also
    brings the DIZZY dependency with it, so the source below is written only
    alongside the package that actually needs it.

    *dizzy_source* overrides where DIZZY comes from: a checkout path (the usual
    case when the generated lib lives inside the repository that generated it)
    or a git URL. The default is the canonical repository, because DIZZY is not
    published to an index.
    """
    all_members = _TYPE_PACKAGE_MEMBERS + [f"{kind}/{name}" for kind, name in members]
    if include_wiring:
        all_members.append("wiring")
    member_lines = "\n".join(f'  "{member}",' for member in all_members)
    lines = [
        "[tool.uv.workspace]",
        "members = [",
        member_lines,
        "]",
        "",
    ]
    if include_wiring:
        # The wiring package is the one that depends on DIZZY itself. The source
        # goes at the workspace ROOT so it applies to every member, which keeps
        # the wiring package's own manifest portable.
        lines += [
            "[tool.uv.sources]",
            _dizzy_source_entry(dizzy_source),
            "",
        ]
    return "\n".join(lines)


def _write_if_absent(path: Path, content: str) -> None:
    if path.exists():
        logger.debug("skipped existing file", extra={"path": str(path)})
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    logger.debug("wrote file", extra={"path": str(path)})


def write_procedure_python_uv(proc: ProcedureDef, output_dir: Path) -> None:
    base = output_dir / "lib" / "python-uv" / "procedure" / proc.name
    _write_if_absent(base / "pyproject.toml", render_element_pyproject_toml("procedure", proc.name))
    _write_if_absent(base / "src" / f"{proc.name}.py", render_src_procedure_stub(proc))


def write_policy_python_uv(policy: PolicyDef, output_dir: Path) -> None:
    base = output_dir / "lib" / "python-uv" / "policy" / policy.name
    _write_if_absent(base / "pyproject.toml", render_element_pyproject_toml("policy", policy.name))
    _write_if_absent(base / "src" / f"{policy.name}.py", render_src_policy_stub(policy))


def write_query_python_uv(query: QueryDef, output_dir: Path) -> None:
    base = output_dir / "lib" / "python-uv" / "query" / query.name
    _write_if_absent(base / "pyproject.toml", render_element_pyproject_toml("query", query.name))
    _write_if_absent(base / "src" / f"{query.name}.py", render_src_query_stub(query.name))


def write_projection_python_uv(proj: ProjectionDef, output_dir: Path) -> None:
    base = output_dir / "lib" / "python-uv" / "projection" / proj.name
    _write_if_absent(
        base / "pyproject.toml", render_element_pyproject_toml("projection", proj.name)
    )
    _write_if_absent(base / "src" / f"{proj.name}.py", render_src_projection_stub(proj))


def write_workspace_python_uv(
    members: list[tuple[str, str]],
    output_dir: Path,
    include_wiring: bool = False,
    dizzy_source: str | None = None,
) -> None:
    dest = output_dir / "lib" / "python-uv" / "pyproject.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_workspace_pyproject_toml(members, include_wiring, dizzy_source))
    logger.debug("wrote file", extra={"path": str(dest)})
