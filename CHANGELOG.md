# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Feature-file format** (`.feat.yaml`): a single artifact declaring a domain
  as commands, events, procedures, policies, projections, models, and queries —
  the reactivity loop (commands → procedures → events → policies) and the data
  loop (events → projections → models → queries).
- **`dizzy generate`** — the pipeline from a feature-file. Three stages produce
  the design: `definitions` (LinkML `def/` schema stubs), `static` (the
  `gen_def/` and `gen_int/` typed-contract packages), and `libraries`
  (per-runtime implementation-stub packages driven by `libconfig.yaml`). A
  fourth, `wiring`, produces the host — see below.
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
- **`dizzy.engine`** — the runtime kit, extracted from the example apps so an
  app no longer brings its own control loop: `loop.Engine` (projections fold and
  the read model commits BEFORE policies dispatch), `store.EventStore` over a
  vendored, stdlib-only content-addressed `dagstore/`, `rebuild`/`replicate`
  (refold the stream; pull a peer's facts through the same projections),
  `registry.FeatGraph` (the feat read as an app's topology), and `ports`
  (`HostApp`/`ShellServices`/`Runtime`, the seam an app publishes itself through).
  Nothing in the tree names a command, event, or environment field — it reads
  them.
- **Scheduling shells** `dizzy.engine.st` and `dizzy.engine.mp`. The engine hands
  every policy-dispatched command to the shell, so **the shell is part of the
  defined semantics**: `st` (one lane) is sequentially consistent, `mp` (N
  workers, at-least-once) deliberately is not.
  `tests/engine/test_conformance.py` is the contract and asserts which guarantee
  each one claims.
- **`dizzy generate wiring`** — a fourth pipeline stage emitting
  `lib/<runtime>/wiring/`: the declared elements bound to a `dizzy.engine` engine
  plus the `HostApp` a shell resolves from `$DIZZY_HOST_APP`. The wiring is a
  pure function of the feature-file, and is engine-mediated by construction — a
  procedure's emitters bind to `engine.emit_event`, a policy's to
  `engine.dispatch_command`, so no element can call another. `Resources.overrides`
  is the declared escape hatch. It is the only generated package that depends on
  DIZZY itself. `examples/recipes/kitchen.py` drops from 428 to 147 lines with
  byte-identical demo output.
- **`--dizzy-source`** on `dizzy generate wiring`: a generated workspace now
  always gets a `[tool.uv.sources]` entry at its root, so `dizzy` resolves to
  this project rather than the unrelated PyPI package of the same name.
- `dizzy.engine.sqla` — SQLite read-model cache management (the completion
  marker, and why a crashed refold must not look current), covered by tests
  including the crash-then-retry path.

- **JSON Schema contracts** — `dizzy generate static` now emits runtime-neutral JSON
  Schema into `gen_schema/`, one document per `def/` source, via LinkML's
  `gen-json-schema`. Driven by a new optional `json_schema` section in
  `libconfig.yaml` (`contracts:` any of `commands | events | queries | models`,
  `output_dir:` defaulting to `gen_schema`). Omitting the section emits nothing, so
  existing `libconfig.yaml` files produce byte-identical output; `generate definitions`
  writes the section into new stubs with `[commands, queries]`.

### Changed
- Package version is now derived from git tags via `hatch-vcs` instead of being
  hardcoded in `pyproject.toml` and `__init__.py`.
- `pyproject.toml` gained release metadata (license, authors, classifiers, URLs).
- **Dependencies restructured around what the runtime actually needs.** Core is
  `pyyaml` + `pydantic` (the store round-trips events through the generated
  classes); the generator tree (`linkml`, `openai`, `typer`) moved behind the
  `gen` extra, so a worker installing DIZZY for a scheduling shell does not
  inherit a code generator it will never call. New extras: `st`, `mp`
  (dramatiq/redis + the OpenTelemetry *API* only), `sqla`, and `all`.
  `dizzy[gen]` is the authoring install.
- **DIZZY is not published to a package index**, by decision — the git
  repository and GitHub Release wheels are the distribution. Dependents name the
  source: `dizzy[gen] @ git+https://github.com/PNNL/dizzy`. README,
  `pyproject.toml`, `dizzy.engine.mp` and `release.yml` all state this; the name
  `dizzy` on PyPI belongs to an unrelated network fuzzer.
- `examples/recipes` runs on the canonical engine, engine-mediated rather than
  calling element-to-element. Its server now owns one path-backed event store
  instead of building a fresh in-memory one per request.
- `ty` reports zero diagnostics over the scope CI checks.
- LinkML floor is `>=1.11.1` (the current latest). 1.9.5 mapped `range: decimal`
  to `Column(Integer())` in `gen-sqla` — silent data loss; 1.11.x emits
  `Column(Numeric())`. Two further behaviour changes come with it: `gen-pydantic`
  now defaults optional multivalued slots to `None` rather than `[]` (and no
  longer emits the `treat_empty_lists_as_none` model serializer), and generated
  modules carry a real `metamodel_version` instead of `"None"`.
- `load_libconfig` now materialises absent element sections as `[]` rather than relying
  on LinkML's pydantic default, which changed to `None` in 1.11.

### Removed
- `Envelope.to_dict` and `Engine.registered()` — both added during the engine
  extraction, neither ever called. `registered()` was meant to catch wiring
  drift, which generated wiring makes structurally impossible.
- `replicate.make_app` — the package's only FastAPI dependency, and a server,
  which belongs app-side. Its replacement is a documented two-endpoint contract
  on `http_transport`, so a host mounts it in the framework it already runs.
- Generated wirings no longer import their adapter class: a context receives an
  adapter *instance*, supplied by the host and looked up by name.

[Unreleased]: https://github.com/PNNL/dizzy/compare/v0.1.1...HEAD
