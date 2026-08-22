# Contributing to DIZZY

Thanks for helping! This project is **trunk-based**: `main` is always
releasable, and all work lands through short-lived branches and pull requests.

## Development setup

DIZZY uses [`uv`](https://docs.astral.sh/uv/) and [`just`](https://just.systems/).

```sh
git clone https://github.com/PNNL/dizzy
cd dizzy
uv sync                # install deps + dev tools into .venv
uv run dizzy onboard   # read this before touching generators
```

That is enough to work in the repo: the dev group installs `dizzy[gen,mp,sqla,st]`, so
`uv run dizzy` has every generator. To put the CLI on your PATH instead, install it with
the `gen` extra — the CLI itself lives behind it, so a bare install has no `typer` and
cannot start:

```sh
uv tool install --editable ".[gen]"
```

DIZZY is not published to a package index; the git repository and the GitHub Release
wheels are the distribution.

## The workflow

1. **Branch off `main`** — keep branches short-lived and focused:
   `git switch -c my-change`.
2. **Make the change.** Match the surrounding code style.
3. **Run the gates locally** (see below).
4. **Open a PR into `main`.** CI runs automatically.
5. **Squash-merge once green.** Keep `main` linear and releasable.

We track issues with [Seeds](https://github.com/jayminwest/seeds). Run
`sd prime` at the start of a session and `sd ready` to find unblocked work.

## Quality gates

`just ci` runs everything CI runs, in the same order:

| Command          | Tool   | Blocks merge? |
| ---------------- | ------ | ------------- |
| `just test`      | pytest + syrupy snapshots | **Yes** |
| `just lint`      | ruff (lint) | No — advisory |
| `just fmt-check` | ruff (format) | No — advisory |
| `just check`     | `ty` (type check) | No — advisory |

- **Tests are the gate.** A PR must pass `pytest` on Python 3.11–3.13 to merge.
  In branch protection, mark the `test (py3.x)` checks as required.
- **Lint / format / type checks are advisory.** They run in the `quality` job
  and report status, but are *not* required checks, so they won't block a merge
  while we adopt them across the codebase. Please still fix what you can —
  `just fmt` auto-formats, and `just lint` shows lint findings.
- If you intentionally re-snapshot, use `just test-update` and review the diff.
- Touching generators? Two slower checks sit outside `just ci`: `just examples-check`
  regenerates `examples/recipes` and fails on any drift in tracked files, and
  `just tutorials-check` runs every tutorial under `docs/tutorials/` end to end in a
  throwaway sandbox (via byexample), checking each command's real output against the
  page. A change to the four generate stages usually needs both.

## Documentation

- `dizzy/src/dizzy/docs/cli.md`, `authoring.md` and `onboard.md` are the authoritative
  tool-shipped docs (printed by `dizzy docs` / `dizzy docs authoring` / `dizzy onboard`,
  shipped in the wheel — **edit them in the package**). When scope changes, change
  `cli.md` first, then the seeds.
- `docs/` is the mkdocs Diátaxis site (`just docs-serve` / `just docs-build`); its
  `reference/api/` pages are generated from the code via mkdocstrings. Every page under
  `docs/tutorials/` is executed by `just tutorials-check`, so its shell blocks must show
  the tool's real output — see `docs/how-to/add-a-validated-tutorial.md`.
- The whitepaper Typst files are maintainer-authored; you may fact-check them, but
  don't author them.

## Releasing (maintainers)

Versions come from git tags via `hatch-vcs` — there is **no** version number to
edit in source. To cut a release:

1. **Update the changelog.** In `CHANGELOG.md`, rename the `[Unreleased]`
   section to the new version with today's date, and start a fresh
   `[Unreleased]` block above it. Keep entries grouped under
   Added / Changed / Fixed / Removed.
2. **Land it on `main`** via PR, as usual.
3. **Tag and push** from `main`:
   ```sh
   git switch main && git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```
4. The **Release** workflow takes over: it verifies the tag matches the built
   version, builds the sdist + wheel, and creates a GitHub Release with those
   artifacts attached.

Tags are `vMAJOR.MINOR.PATCH` following [SemVer](https://semver.org/). To test a
build without releasing, run `just build` — artifacts land in `dist/`.
