# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Runtime — the `dizzy.engine` kit

- **`dizzy.engine`** — the runtime kit, extracted from the example apps so an
  app no longer brings its own control loop:
  - `loop.Engine` — the control loop. Projections fold and the read model commits
    BEFORE policies dispatch.
  - `store.EventStore` over a vendored, stdlib-only content-addressed `dagstore/`.
  - `rebuild` / `replicate` — refold the stream; pull a peer's facts through the
    same projections.
  - `registry.FeatGraph` — the feat read as an app's topology.
  - `ports` — `HostApp` / `ShellServices` / `Runtime`, the seam an app publishes
    itself through.

  Nothing in the tree names a command, event, or environment field — it reads them.
- **Scheduling shells** `dizzy.engine.st` and `dizzy.engine.mp`. The engine hands
  every policy-dispatched command to the shell, so **the shell is part of the
  defined semantics**:
  - `st` — one lane, sequentially consistent.
  - `mp` — N workers, at-least-once, deliberately *not* sequentially consistent.

  `dizzy/tests/engine/test_conformance.py` is the contract and asserts which
  guarantee each one claims.
- `dizzy.engine.sqla` — SQLite read-model cache management (the completion
  marker, and why a crashed refold must not look current), covered by tests
  including the crash-then-retry path.

#### Generation

- **`dizzy generate wiring`** — a fourth pipeline stage emitting
  `lib/<runtime>/wiring/`: the declared elements bound to a `dizzy.engine` engine
  plus the `HostApp` a shell resolves from `$DIZZY_HOST_APP`. The wiring is a
  pure function of the feature-file, and is engine-mediated by construction:
  - A procedure's emitters bind to `engine.emit_event`, a policy's to
    `engine.dispatch_command` — so no element can call another.
  - `Resources.overrides` is the declared escape hatch.
  - It is the only generated package that depends on DIZZY itself.
  - `examples/recipes/kitchen.py` drops from 428 to 147 lines with byte-identical
    demo output.
- **`--dizzy-source`** on `dizzy generate wiring`: a generated workspace now
  always gets a `[tool.uv.sources]` entry at its root, so `dizzy` resolves to
  this project rather than the unrelated PyPI package of the same name.
- **JSON Schema contracts** — `dizzy generate static` now emits runtime-neutral JSON
  Schema into `gen_schema/`, one document per `def/` source, via LinkML's
  `gen-json-schema`:
  - Driven by a new optional `json_schema` section in `libconfig.yaml`
    (`contracts:` any of `commands | events | queries | models`, `output_dir:`
    defaulting to `gen_schema`).
  - Omitting the section emits nothing, so existing `libconfig.yaml` files produce
    byte-identical output.
  - `generate definitions` writes the section into new stubs with `[commands, queries]`.

#### Documentation and project tooling

- **[What DIZZY is (and why events)](docs/explanation/what-is-dizzy.md)** — an
  on-ramp for readers who have not done event sourcing. Opens on the design
  decision a `status` column makes for you, then names the seven element types as
  parts of a system the reader has already seen. Includes the guestbook
  feature-file by snippet so it cannot drift, and hands off to the tutorial.
- `just churn [ref]` — how much of the current branch is new since the last tagged
  release, for scoping review before a cut. Naive by design: every tracked line,
  docs and tests and lockfiles included.

### Changed

#### Packaging and distribution

- **Dependencies restructured around what the runtime actually needs.** Core is
  `pyyaml` + `pydantic` (the store round-trips events through the generated
  classes); the generator tree (`linkml`, `openai`, `typer`) moved behind the
  `gen` extra, so a worker installing DIZZY for a scheduling shell does not
  inherit a code generator it will never call.
  - New extras: `gen` (the authoring install — the CLI and every generator),
    `st`, `mp` (dramatiq/redis + the OpenTelemetry *API* only), `sqla`, and `all`.
- **DIZZY is not published to a package index**, by decision — the git
  repository and GitHub Release wheels are the distribution. Dependents name the
  source: `dizzy[gen] @ git+https://github.com/PNNL/dizzy`. README,
  `pyproject.toml`, `dizzy.engine.mp` and `release.yml` all state this.

#### LinkML floor raised to `>=1.11.1`

The floor moved because 1.9.5 mapped `range: decimal` to `Column(Integer())` in
`gen-sqla` — silent data loss on every monetary or measured field. 1.11.x emits
`Column(Numeric())`. Three further behaviour changes ride along with the bump:

- `gen-pydantic` now defaults optional multivalued slots to `None` rather than
  `[]`, and no longer emits the `treat_empty_lists_as_none` model serializer.
  - Consequently, `load_libconfig` now materialises absent element sections as
    `[]` itself rather than relying on the generated default.
- Generated modules carry a real `metamodel_version` instead of `"None"`.
- The committed `gen` snapshots were refreshed to match the new output.

#### Behaviour

- `examples/recipes` runs on the canonical engine, engine-mediated rather than
  calling element-to-element. Its server now owns one path-backed event store
  instead of building a fresh in-memory one per request.
- `ty` reports zero diagnostics over the scope CI checks.

### Removed

- `Envelope.to_dict` and `Engine.registered()` — both added during the engine
  extraction, neither ever called. `registered()` was meant to catch wiring
  drift, which generated wiring makes structurally impossible.
- `replicate.make_app` — the package's only FastAPI dependency, and a server,
  which belongs app-side. Its replacement is a documented two-endpoint contract
  on `http_transport`, so a host mounts it in the framework it already runs.
- Generated wirings no longer import their adapter class: a context receives an
  adapter *instance*, supplied by the host and looked up by name.

### Fixed

- `just install` installed a bare `.`, but `cli.py` imports `typer` at module scope
  and typer moved behind the `gen` extra — the recipe put a `dizzy` on PATH that
  died on `ModuleNotFoundError` before it could print `--help`. It now installs
  `".[gen]"`, which is what `pyproject.toml` already claimed it did.
- `examples/README.md` documented a three-stage sequence for an example whose
  `kitchen.py` consumes generated wiring. Because `generate libraries` rewrites the
  workspace manifest — dropping the `wiring` member and the `[tool.uv.sources]`
  entry that stage 4 writes — following it produced a workspace that synced
  successfully and installed nothing.
- `docs/reference/SPECIFICATION.md` documented an `attributes:` sub-map on commands
  and events, and a `models:` list on projections. `CommandDef` and `EventDef` carry
  only `name` and `description` under `extra="forbid"`, and `ProjectionDef` takes a
  singular optional `model`, so every example in those sections was a load error.
- README claimed generated deployment and tests (no such generator exists), called
  the repo a uv monorepo (it is one hatchling package), listed a `queriers:` section
  absent from the schema, and pinned an install example to a tag that was never cut.

## [0.1.1] - 2026-06-25

Documentation release. The tool's own docs moved into the package, the prose became
a Diátaxis site, and the tutorials became executable — checked on every PR rather
than trusted.

### Added
- **mkdocs Diátaxis site** (`mkdocs.yml`, `docs/`), deployed to GitHub Pages by
  `docs.yml`. API reference pages are generated from the source by
  `gen_ref_pages.py` via mkdocstrings.
- **Validated tutorials**, executed by [byexample](https://byexamples.github.io/):
  `guestbook` (a feature end to end), `library` (a policy that consults a query),
  and `agent` (a streaming agent turn). Every command and every line of output on
  those pages is run and compared, so they cannot drift from the tool.
- `just tutorials-check`, and a CI job gating it on every PR.
- **Tool-shipped documentation**: `cli.md`, `authoring.md` and `onboard.md` moved
  into `dizzy/src/dizzy/docs/` so they ship in the wheel and are printed by
  `dizzy docs` / `dizzy docs authoring` / `dizzy onboard`.
- `docs/how-to/add-a-validated-tutorial.md`, and `just tutorial-capture` to render
  schema edits as applied diffs rather than hand-transcribed ones.

### Changed
- The `guestbook`, `library` and `agent` examples were retired from `examples/` —
  they are validated tutorials now. `examples/` keeps `recipes` and `simulate`.
- Generated `gen_def`/`gen_int` packages are no longer committed for examples; they
  are gitignored and regenerated on demand.
- Dead specification documents were deleted in favour of the shipped docs.

### Fixed
- `ty` errors arising from Optional LinkML slots.

## [0.1.0] - 2026-06-25

First tagged release: the feature-file format, the generate pipeline, and the
release machinery to ship them.

### Added
- **Feature-file format** (`.feat.yaml`): a single artifact declaring a domain
  as commands, events, procedures, policies, projections, models, and queries —
  the reactivity loop (commands → procedures → events → policies) and the data
  loop (events → projections → models → queries).
- **`dizzy generate`** — the three-stage pipeline from a feature-file:
  `definitions` (LinkML `def/` schema stubs), `static` (the `gen_def/` and
  `gen_int/` typed-contract packages), and `libraries` (per-runtime
  implementation-stub packages driven by `libconfig.yaml`).
- **Runtime targets**: `python-uv` (most complete), plus experimental
  `rust-cargo` and `typescript-npm` generators; model adapters (e.g. `sqla`).
- **`dizzy simulate`** — LLM-driven execution of a feature-file against a
  scenario (level 0).
- **`dizzy onboard` / `docs` / `config`** — agent-facing project overview, the
  CLI + authoring documentation, and a config template.
- **Worked examples**: a fully implemented, runnable `guestbook`, plus
  `recipes`, `library`, and `agent` feature-files.
- Trunk-based CI (`ci.yml`): tests gate every PR; ruff lint/format and `ty`
  type checks run as advisory signal.
- Tag-driven release pipeline (`release.yml`): a `v*` tag builds the sdist +
  wheel and cuts a GitHub Release with those artifacts attached.
- `CONTRIBUTING.md` documenting the dev setup, quality gates, and release flow.
- `ruff` (lint + format) and `ty` added to the dev dependency group, with
  `just lint`, `just fmt`, `just fmt-check`, `just ci`, and `just build`
  recipes.

### Changed
- Package version is now derived from git tags via `hatch-vcs` instead of being
  hardcoded in `pyproject.toml` and `__init__.py`.
- `pyproject.toml` gained release metadata (license, authors, classifiers, URLs).

[Unreleased]: https://github.com/PNNL/dizzy/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/PNNL/dizzy/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PNNL/dizzy/releases/tag/v0.1.0
