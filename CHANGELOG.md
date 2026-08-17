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
- LinkML floor is `>=1.11.1` (the current latest). 1.9.5 mapped `range: decimal`
  to `Column(Integer())` in `gen-sqla` — silent data loss; 1.11.x emits
  `Column(Numeric())`. Two further behaviour changes come with it: `gen-pydantic`
  now defaults optional multivalued slots to `None` rather than `[]` (and no
  longer emits the `treat_empty_lists_as_none` model serializer), and generated
  modules carry a real `metamodel_version` instead of `"None"`.
- `load_libconfig` now materialises absent element sections as `[]` rather than relying
  on LinkML's pydantic default, which changed to `None` in 1.11.

[Unreleased]: https://github.com/PNNL/dizzy/compare/HEAD
