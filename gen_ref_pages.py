"""Generate the code-reference pages (Reference > Code API) from the dizzy package.

Run automatically by the mkdocs ``gen-files`` plugin at build/serve time. Walks the
package source, emits one ``reference/api/<module>.md`` page per module containing a
``:::`` mkdocstrings directive, and a literate-nav ``SUMMARY.md``. The output lives
under ``docs/reference/api/`` (gitignored — it is a build artifact).
"""

from pathlib import Path

import mkdocs_gen_files

SRC = Path("dizzy/src")
PKG = SRC / "dizzy"

# Generated/auto-built modules — not hand-authored source, so skip them.
SKIP = {"_version", "feat_schema", "libconfig_schema", "topology_schema"}

nav = mkdocs_gen_files.Nav()

for path in sorted(PKG.rglob("*.py")):
    module_path = path.relative_to(SRC).with_suffix("")  # e.g. dizzy/generators/commands
    doc_path = path.relative_to(PKG).with_suffix(".md")  # e.g. generators/commands.md
    parts = tuple(module_path.parts)  # ("dizzy", "generators", "commands")

    if parts[-1] == "__init__":
        parts = parts[:-1]
        doc_path = doc_path.with_name("index.md")
    elif parts[-1] == "__main__":
        continue

    if parts and parts[-1] in SKIP:
        continue

    full_doc_path = Path("reference/api", doc_path)
    nav[parts] = doc_path.as_posix()

    with mkdocs_gen_files.open(full_doc_path, "w") as fd:
        ident = ".".join(parts)
        fd.write(f"# `{ident}`\n\n::: {ident}\n")

    mkdocs_gen_files.set_edit_path(full_doc_path, path)

with mkdocs_gen_files.open("reference/api/SUMMARY.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())
