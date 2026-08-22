# The `gen` extra, not a bare `.`: typer and linkml moved behind it when the engine
# shells landed, and cli.py imports typer at module scope — a core-only install puts a
# `dizzy` on PATH that dies on ModuleNotFoundError before it can print --help.
install:
    uv tool install --editable ".[gen]"

gen-feat-pydantic:
    uv run gen-pydantic dizzy/src/dizzy/def/feat.yaml > dizzy/src/dizzy/feat_schema.py

gen-libconfig-pydantic:
    uv run gen-pydantic dizzy/src/dizzy/def/libconfig.yaml > dizzy/src/dizzy/libconfig_schema.py

test:
    uv run pytest

test-update:
    uv run pytest --snapshot-update

check:
    uv run ty check dizzy/src/dizzy dizzy/tests

# --- Code quality (lint + format) ---

lint:
    uv run ruff check dizzy/src/dizzy dizzy/tests

fmt:
    uv run ruff format dizzy/src/dizzy dizzy/tests

fmt-check:
    uv run ruff format --check dizzy/src/dizzy dizzy/tests

# Everything CI runs, in the same order. Tests are the gate; the rest are advisory.
ci: lint fmt-check check test

# Regenerate a committed example and fail if any tracked file drifted. The compiled
# type packages (gen_def/gen_int) are gitignored, so this checks the element packages
# and authored sources only. (The guestbook/library/agent features are end-to-end
# checked by tutorials-check.)
examples-check:
    uv run dizzy generate static examples/recipes/recipes.feat.yaml examples/recipes
    uv run dizzy generate libraries examples/recipes/recipes.feat.yaml examples/recipes
    uv run dizzy generate wiring examples/recipes/recipes.feat.yaml examples/recipes --dizzy-source ../../../..
    git diff --exit-code examples/recipes

# --- Build ---

# Build sdist + wheel into dist/ (version comes from the current git tag via hatch-vcs).
build:
    rm -rf dist
    uv build

whitepaper:
    typst compile docs/whitepaper.typ

whitepaper-watch:
    typst watch docs/whitepaper.typ

# --- Documentation site (mkdocs) ---
# Regenerate the interactive Seeds dependency graph (docs/seeds-dag.html) from `sd`.
seeds-dag:
    uv run --group docs docs/seeds-graph/gen-seeds-dag.py

# Serve the Diátaxis docs site locally with live reload.
docs-serve: seeds-dag
    uv run --group docs mkdocs serve

# Build the docs site into site/ and fail on any warning.
docs-build: seeds-dag
    uv run --group docs mkdocs build --strict


# Run every tutorial under docs/tutorials/ end-to-end in a throwaway sandbox and check
# that each command + file matches the documented output (via byexample).
tutorials-check:
    #!/usr/bin/env bash
    set -euo pipefail
    repo="$(pwd)"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    for doc in "$repo"/docs/tutorials/*.md; do
        [ "$(basename "$doc")" = "index.md" ] && continue
        echo "▶ ${doc#$repo/}"
        ( cd "$work" && rm -rf ./* ./.[!.]* 2>/dev/null || true ) || true
        # Stage any per-tutorial assets (e.g. edits/*.diff) that the steps apply.
        assets="${doc%.md}"
        [ -d "$assets" ] && cp -r "$assets/." "$work/"
        # LinkML compilation + uv sync are slow; give every step a generous timeout.
        # Drop VIRTUAL_ENV so a tutorial's own `uv run --project ...` doesn't warn about
        # a mismatch with this runner's environment.
        ( cd "$work" && uv run --group docs --project "$repo" \
            env -u VIRTUAL_ENV byexample -l shell --timeout 300 "$doc" )
    done


install-completions:
    mkdir -p $HOME/.local/share/bash-completion
    just --completions bash > $HOME/.local/share/bash-completion/just.bash

tag tag:
    git tag {{tag}}; git push origin {{tag}}