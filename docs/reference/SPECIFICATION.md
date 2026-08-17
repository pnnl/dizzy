# Dizzy Feature File Specification

## Overview

A `.feat.yaml` file is the primary authoring surface in Dizzy. It describes a single feature's
domain model and processing logic at a high level of abstraction — without specifying databases,
frameworks, or infrastructure.

The generator pipeline reads a `.feat.yaml` and produces:
- LinkML schema files (`def/`) for each section
- Generated Python models and interfaces (`gen_def/`, `gen_int/`)
- Runtime-neutral JSON Schema contracts (`gen_schema/`), when `libconfig.yaml` asks for
  them — see [JSON Schema contracts](#json-schema-contracts)

---

## Top-Level Structure

```yaml
description: <string>   # Human-readable description of the feature (optional)

models:      <map>       # Domain value objects / data shapes
queries:     <map>       # Read interfaces (input + output)
commands:    <map>       # Write intents
events:      <map>       # Immutable facts (what happened)
procedures:  <map>       # Command handlers (do work, emit events)
policies:    <map>       # Event handlers (react, issue commands)
projections: <map>       # Read-model builders (event → queryable state)
environment: <map>       # Injected constants/variables (in place of os env)
telemetry:   <map>       # Injected observation sinks (callables)
```

All sections are optional. The generator skips sections not present.

---

## Section Definitions

### `models`
Named database schemas — each entry represents a logical grouping of related classes (tables)
for a single database. The feat file declares schema names and optional descriptions only.
The actual classes are defined in the corresponding `def/models/<schema_name>.yaml` LinkML file,
which is authored separately and may grow over time without touching the feat file.

The generator creates a stub `def/models/<schema_name>.yaml` if one does not already exist, then
generates one output file per schema per target backend. Use plural, lowercase names.

```yaml
models:
  recipes: Full recipe database — recipes, steps, and ingredients
```

`def/models/recipes.yaml` (hand-authored) then defines all classes in that schema:

```yaml
classes:
  Recipe:
    attributes:
      title: ...
  Step:
    attributes:
      body: ...
  Ingredient:
    attributes:
      name: ...
      quantity: ...
```

**Scaffold generates** (stub, never overwritten):
- `def/models/<schema_name>.yaml` — stub LinkML schema

**Gen generates** (by running the LinkML toolchain on the authored stub):
- `gen_def/pydantic/models/<schema_name>.py` — Pydantic models (via `linkml gen-pydantic`)
- `gen_def/sqla/models/<schema_name>.py` — SQLAlchemy models (via `linkml gen-sqla`)

---

### `queries`
Named read operations. Each query must declare the single model schema it reads from, but IO
types are not specified in the feat file — those are defined in authored LinkML stubs and
fleshed out when the implementation is written.

Each query decomposes into **three composable elements**:

- **`QueryInput`** — a LinkML-defined data shape for the query's input parameters
- **`QueryOutput`** — a LinkML-defined data shape for the query's return value
- **`QueryProcess`** — a Protocol for the callable that accepts a `QueryInput` and a context
  (holding a SQLAlchemy session for the referenced model schema) and returns a `QueryOutput`

```yaml
queries:
  get_recipe_text:
    description: Retrieves raw recipe text given a source reference
    model: recipes
  get_recipe:
    description: Retrieves a structured recipe by ID
    model: recipes
```

Fields:
- `description` (required): what this query does
- `model` (required): the schema name from `models` that this query reads from

**Scaffold generates** (stub, never overwritten):
- `def/queries/<query_name>.yaml` — single LinkML stub containing both `<QueryName>Input` and `<QueryName>Output` class stubs

For example, `def/queries/get_recipe_text.yaml`:

```yaml
id: https://example.org/queries/get_recipe_text
name: get_recipe_text
description: Retrieves raw recipe text given a source reference
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
imports:
  - linkml:types
classes:
  GetRecipeTextInput:
    description: Input for get_recipe_text
    attributes: {}
  GetRecipeTextOutput:
    description: Output for get_recipe_text
    attributes: {}
```

**Gen generates** (by running `linkml gen-pydantic` on the authored def stub, then deriving the Protocol from the feat file):
- `gen_def/pydantic/query/<query_name>.py` — Pydantic models for both `<QueryName>Input` and `<QueryName>Output` (via linkml)
- `gen_int/python/query/<query_name>.py` — `QueryProcess` Protocol + context dataclass:

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Protocol, Any

from gen_def.pydantic.query.get_recipe_text import GetRecipeTextInput, GetRecipeTextOutput


@dataclass
class get_recipe_text_context:
    """SQLAlchemy session for the schema read by this query."""
    recipes: Any  # SQLAlchemy session for the recipes schema


class get_recipe_text_query(Protocol):
    """Retrieves raw recipe text given a source reference"""

    def __call__(
        self, input: GetRecipeTextInput, context: get_recipe_text_context
    ) -> GetRecipeTextOutput:
        ...
```

Queries declared in a procedure's `queries:` list are injected into that procedure's
`_queries` context dataclass as typed fields:

```python
@dataclass
class extract_and_transform_recipe_queries:
    get_recipe_text: Callable[[GetRecipeTextInput], GetRecipeTextOutput]
```

Each query field is a **host-bound callable** — the host injects the read adapter and
supplies a closure that takes only the query input and returns its output (symmetric
with how `emit` fields are bound). The handler calls `context.query.get_recipe_text(input)`
without needing the query's own adapter context.

---

### `commands`
Named write intents. Value is either a short description string, or a map with description and optional attributes.

```yaml
commands:
  ingest_recipe_text: Initiates ingestion of a recipe from a raw text source

  upload_blob_using_manifest:
    description: Uploads a blob using manifest information
    attributes:
      manifest_id:
        type: string
        required: true
```

**Scaffold generates** (stub, never overwritten):
- `def/commands.yaml` — LinkML stub listing all commands with attributes

**Gen generates** (by running `linkml gen-pydantic` on the authored stub):
- `gen_def/pydantic/commands.py` — Pydantic models for all commands

---

### `events`
Immutable domain facts. Value is either a description string, or a map with description and optional attributes.

```yaml
events:
  recipe_ingested: A recipe was successfully ingested and validated

  scan_item_found:
    description: Found a file while scanning
    attributes:
      file_path:
        type: string
        required: true
      file_hash:
        type: string
```

**Scaffold generates** (stub, never overwritten):
- `def/events.yaml` — LinkML stub listing all events with attributes

**Gen generates** (by running `linkml gen-pydantic` on the authored stub):
- `gen_def/pydantic/events.py` — Pydantic models for all events

---

### `procedures`
Command handlers. Each procedure is bound to one command, declares queries it uses, and events it may emit.

```yaml
procedures:
  extract_and_transform_recipe:
    description: >
      Queries raw recipe text via source_ref, then uses an LLM to extract a structured
      recipe, validated against the recipe model schema.
    command: ingest_recipe_text
    queries:
      - get_recipe_text
    emits:
      - recipe_ingested
```

Fields:
- `command` (required): the command this procedure handles
- `queries` (optional): list of query names this procedure needs access to
- `emits` (optional): list of event names this procedure may emit
- `environment` (optional): list of `environment` entry names injected as `context.env.<name>`
- `telemetry` (optional): list of `telemetry` sink names callable as `context.telemetry.<name>(payload)`

**Gen generates:**
- `gen_int/python/procedure/<procedure_name>_context.py` — context dataclass with `_emitters` and `_queries` nested dataclasses:

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Callable

from gen_def.pydantic.events import recipe_ingested
from gen_def.pydantic.query.get_recipe_text import GetRecipeTextInput, GetRecipeTextOutput


@dataclass
class extract_and_transform_recipe_emitters:
    recipe_ingested: Callable[[recipe_ingested], None]


@dataclass
class extract_and_transform_recipe_queries:
    get_recipe_text: Callable[[GetRecipeTextInput], GetRecipeTextOutput]


@dataclass
class extract_and_transform_recipe_context:
    emit: extract_and_transform_recipe_emitters
    query: extract_and_transform_recipe_queries
```

- `gen_int/python/procedure/<procedure_name>_protocol.py` — Protocol stub:

```python
# AUTO-GENERATED — do not edit
from typing import Protocol

from gen_def.pydantic.commands import ingest_recipe_text
from gen_int.python.procedure.extract_and_transform_recipe_context import (
    extract_and_transform_recipe_context,
)


class extract_and_transform_recipe_protocol(Protocol):
    """Queries raw recipe text via source_ref, then uses an LLM to extract a structured recipe."""

    def __call__(
        self,
        context: extract_and_transform_recipe_context,
        command: ingest_recipe_text,
    ) -> None:
        ...
```

- `src/procedure/<procedure_name>.py` — empty implementation stub (skipped if already exists)

---

### `policies`
Event-driven reaction handlers. Each policy listens to one event, may declare queries
it consults, and **dispatches commands only** (never events). A query informs *which*
command a policy dispatches, and with what arguments — the decision lives in read state,
not in the policy's hard-coded logic. To change state, a policy emits a command, which
flows through the normal command → procedure → event chain.

```yaml
policies:
  trigger_priority_manifest:
    description: Issues command to create image priority manifest when scan completes
    event: scan_complete
    queries:
      - get_pending_scan_count
    emits:
      - create_image_priority_manifest
```

Fields:
- `event` (required): the event that triggers this policy
- `queries` (optional): list of query names this policy consults to decide what to dispatch
- `emits` (optional): list of command names this policy may dispatch
- `environment` (optional): list of `environment` entry names injected as `context.env.<name>`
- `telemetry` (optional): list of `telemetry` sink names callable as `context.telemetry.<name>(payload)`

**Gen generates:**
- `gen_int/python/policy/<policy_name>_context.py` — context dataclass with emitters and (when declared) queries nested dataclasses (mirrors procedure context):

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Callable

from gen_def.pydantic.commands import create_image_priority_manifest
from gen_def.pydantic.query.get_pending_scan_count import (
    GetPendingScanCountInput,
    GetPendingScanCountOutput,
)


@dataclass
class trigger_priority_manifest_emitters:
    create_image_priority_manifest: Callable[[create_image_priority_manifest], None]


@dataclass
class trigger_priority_manifest_queries:
    get_pending_scan_count: Callable[[GetPendingScanCountInput], GetPendingScanCountOutput]


@dataclass
class trigger_priority_manifest_context:
    emit: trigger_priority_manifest_emitters
    query: trigger_priority_manifest_queries
```

For policies with no `emits`, the emitters dataclass has `pass`. The `query` field and
its `_queries` dataclass appear only when the policy declares `queries`. As with
procedures, each query field is a host-bound `Callable[[Input], Output]` closure.

- `gen_int/python/policy/<policy_name>_protocol.py` — Protocol stub:

```python
# AUTO-GENERATED — do not edit
from typing import Protocol

from gen_def.pydantic.events import scan_complete
from gen_int.python.policy.trigger_priority_manifest_context import (
    trigger_priority_manifest_context,
)


class trigger_priority_manifest_protocol(Protocol):
    """Issues command to create image priority manifest when scan completes"""

    def __call__(
        self, event: scan_complete, context: trigger_priority_manifest_context
    ) -> None:
        ...
```

- `src/policy/<policy_name>.py` — implementation stub (skipped if already exists)

---

### `projections`
Build queryable read models in response to a single event. Each projection listens to exactly
one event and may update one or more model schemas.

A projection is structurally similar to a procedure: it receives an **event** and a **context**
object, then uses SQLAlchemy to persist state into the referenced model schemas. One SQLAlchemy
session is injected per declared model schema.

```yaml
projections:
  recipe_library:
    description: Adds ingested recipe to the recipe library
    event: recipe_ingested
    models:
      - recipes
```

Fields:
- `description` (required): what this projection does
- `event` (required): the single event that triggers this projection
- `models` (required): list of schema names from `models` that this projection writes into
- `environment` (optional): list of `environment` entry names injected as `context.env.<name>`
- `telemetry` (optional): list of `telemetry` sink names callable as `context.telemetry.<name>(payload)`

**Gen generates:** `gen_int/python/projection/<projection_name>_projection.py` — a context dataclass
and a Protocol stub, plus `src/projection/<projection_name>.py` (skipped if already exists):

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Protocol, Any

from gen_def.pydantic.events import recipe_ingested


@dataclass
class recipe_library_context:
    """SQLAlchemy sessions for schemas written by this projection."""
    recipes: Any  # SQLAlchemy session for the recipes schema


class recipe_library_projection(Protocol):
    """Adds ingested recipe to the recipe library"""

    def __call__(self, event: recipe_ingested, context: recipe_library_context) -> None:
        """Apply the projection — mutate model state in response to the event."""
        ...
```

The `Any` session type is a placeholder; the implementor binds it to a concrete SQLAlchemy
`Session` when wiring up the projection.

---

### `environment`

Named injected constants/variables, acquired from the host in place of reading `os.environ`.
The feat file declares names and descriptions only; each entry's shape is authored in
`def/environment.yaml` (one LinkML class per entry). A function references entries via its
`environment:` list, and the value surfaces as `context.env.<name>`.

```yaml
environment:
  model: The LLM model configuration injected in place of an os env var.
```

**Gen generates:** `gen_def/pydantic/environment.py` (compiled from `def/environment.yaml`),
and — for any function that lists the entry — a `<name>_env` dataclass plus an `env` field on
that function's context.

### `telemetry`

Named host-injected observation sinks. Each entry is a callable the function invokes with a
typed payload — the emitters pattern, but for *observation* (streamed tokens, progress, metrics)
rather than durable facts. A telemetry call is a transport concern and is **never** recorded as
an event. The payload shape is authored in `def/telemetry.yaml` (one LinkML class per entry).
A function references entries via its `telemetry:` list, surfacing as
`context.telemetry.<name>(payload)`.

```yaml
telemetry:
  stream_chunk: Sink for live LLM token chunks forwarded to the SSE transport.
```

**Gen generates:** `gen_def/pydantic/telemetry.py` (compiled from `def/telemetry.yaml`), and —
for any function that lists the entry — a `<name>_telemetry` dataclass of
`Callable[[Payload], None]` sinks plus a `telemetry` field on that function's context.

---

## Full Example

```yaml
description: Recipe App

models:
  recipes: Full recipe database — recipes, steps, and ingredients

queries:
  get_recipe_text:
    description: Retrieves raw recipe text given a source reference
    model: recipes
  get_recipe:
    description: Retrieves a structured recipe by ID
    model: recipes

commands:
  ingest_recipe_text: Initiates ingestion of a recipe from a raw text source

events:
  recipe_ingested:
    description: A recipe was successfully extracted and validated
    attributes:
      recipe_id:
        type: string
        required: true
      source_ref:
        type: string
        required: true

procedures:
  extract_and_transform_recipe:
    description: >
      Queries raw recipe text via source_ref, then uses an LLM to extract a structured
      recipe (title, ingredients, steps, time, cost), validated against the recipe model.
    command: ingest_recipe_text
    queries:
      - get_recipe_text
    emits:
      - recipe_ingested

policies:
  index_recipe_on_ingest:
    description: Adds recipe to the library projection when ingested
    event: recipe_ingested

projections:
  recipe_library:
    description: Adds ingested recipe to the recipe library
    event: recipe_ingested
    models:
      - recipes
```

---

## Generator Output Layout

Given a feature at `app/my_feature/my_feature.feat.yaml`, the generator produces:

```
app/my_feature/
  def/
    models/
      recipes.yaml
    queries/
      get_recipe_text.yaml
      get_recipe.yaml
    commands.yaml
    events.yaml
  gen_def/
    pydantic/
      models/
        recipes.py
      query/
        get_recipe_text.py
        get_recipe.py
      commands.py
      events.py
    sqla/
      models/
        recipes.py
  gen_int/
    python/
      query/
        get_recipe_text.py
        get_recipe.py
      procedure/
        extract_and_transform_recipe_context.py
        extract_and_transform_recipe_protocol.py
      policy/
        index_recipe_on_ingest_protocol.py
      projection/
        recipe_library_projection.py
  src/
    query/
      get_recipe_text.py
      get_recipe.py
    procedure/
      extract_and_transform_recipe.py
    policy/
      index_recipe_on_ingest.py
    projection/
      recipe_library.py
```

def: definitions
gen_def: generated definitions
gen_int: generated interfaces

`dizzy gen` also emits an empty `__init__.py` in every generated directory so that the output
tree is a valid Python package and root-relative imports resolve correctly.

Sections with no content in the feat file produce no output.

---

## JSON Schema contracts

`dizzy generate static` compiles `def/` sources to JSON Schema via LinkML's
`gen-json-schema`, driven by the `json_schema` section of `libconfig.yaml`:

```yaml
json_schema:
  contracts: [commands, queries]   # any of: commands | events | queries | models
  output_dir: gen_schema           # relative to <output_dir>; default `gen_schema`
```

- **Absent section → nothing is emitted.** A `libconfig.yaml` written before the section
  existed produces byte-identical output, so this is a purely additive change.
- **`json_schema: {}` → opts in with the defaults** above: `contracts: [commands, queries]`,
  `output_dir: gen_schema`. Commands and queries are the outward-facing contracts — the
  shapes an HTTP edge or a UI actually posts and receives.
- `generate definitions` writes the section into *new* libconfig stubs. It never
  overwrites an existing `libconfig.yaml`.

One document is emitted per `def/` source, mirroring the `def/` layout:

```
gen_schema/
  commands.schema.json          # from def/commands.yaml
  events.schema.json            # from def/events.yaml
  queries/<name>.schema.json    # from def/queries/<name>.yaml
  models/<name>.schema.json     # from def/models/<name>.yaml
```

Every class in a source lands under `$defs` in the emitted document, keyed by its
LinkML-normalised (PascalCase) class name. Validate a single payload by pointing a
validator at a `$ref` into `$defs`:

```python
import json
from jsonschema import Draft201909Validator

doc = json.loads(open("gen_schema/queries/get_projects.schema.json").read())
validator = Draft201909Validator({**doc, "$ref": "#/$defs/GetProjectsOutput"})
validator.validate(api_response)
```

`gen_schema/` is a sibling of `def/` and `lib/`, not a child of `lib/python-uv/`: JSON
Schema is consumed by every runtime, and by consumers that are not a runtime at all
(HTTP edges, docs sites, contract tests), so filing it inside one language tree would
misplace it.

---

## Import Path Convention

All generated files use **relative Python imports**. The intent is that the entire output
directory is portable — it can be copied or symlinked into any project structure without
changing import paths.

Generated files assume the feature output directory is a Python package root (i.e. there is
an `__init__.py` at each level). Imports between generated layers use dot-notation relative
to that root:

| From | Importing | Import |
|------|-----------|--------|
| `gen_int/python/query/` | Query input/output models | `from gen_def.pydantic.query.<name> import <Name>Input, <Name>Output` |
| `gen_int/python/procedure/` | Pydantic events | `from gen_def.pydantic.events import ...` |
| `gen_int/python/procedure/` | Pydantic commands | `from gen_def.pydantic.commands import ...` |
| `gen_int/python/procedure/` | Query input/output models | `from gen_def.pydantic.query.<name> import <Name>Input, <Name>Output` |
| `gen_int/python/policy/` | Pydantic events | `from gen_def.pydantic.events import ...` |
| `gen_int/python/policy/` | Pydantic commands | `from gen_def.pydantic.commands import ...` |
| `gen_int/python/policy/` | Query input/output models | `from gen_def.pydantic.query.<name> import <Name>Input, <Name>Output` |
| `gen_int/python/projection/` | Pydantic events | `from gen_def.pydantic.events import ...` |
| `src/query/` | Query Protocol + context | `from gen_int.python.query.<name> import ...` |
| `src/procedure/` | Procedure Protocol + context | `from gen_int.python.procedure.<name>_protocol import ...` |
| `src/policy/` | Policy Protocol | `from gen_int.python.policy.<name>_protocol import ...` |
| `src/projection/` | Projection Protocol + context | `from gen_int.python.projection.<name>_projection import ...` |

The feature output directory must be on `sys.path` (or be a package reachable from it) for
these imports to resolve at runtime.

---

## CLI Workflow

Dizzy is a two-step generator. The split exists because `def/` files require human authorship
between the two steps — they cannot be fully derived from the feat file alone.

### Step 1 — Author the feat file

Write `my_feature.feat.yaml` by hand. Declare models, commands, events, procedures, policies,
and projections at the intent level. No types, no schemas, no implementation details yet.

---

### Step 2 — Scaffold definition stubs

```
dizzy scaffold <feat_file> <output_dir>
```

Reads the feat file and generates empty `def/` stub files for everything that requires
human schema authorship before code can be generated:

- `def/models/<schema_name>.yaml` — stub LinkML schema per model (skipped if already exists)
- `def/queries/<query_name>.yaml` — stub LinkML schema with `<QueryName>Input` and `<QueryName>Output` class stubs (skipped if already exists)
- `def/commands.yaml` — stub LinkML schema listing all commands (skipped if already exists)
- `def/events.yaml` — stub LinkML schema listing all events (skipped if already exists)

After running, Dizzy prints:

```
Scaffolded def/ stubs. Next steps:
  1. Fill in class definitions in def/models/*.yaml
  2. Add input/output shapes in def/queries/*.yaml
  3. Add attributes to def/commands.yaml and def/events.yaml
  4. Run: dizzy gen <feat_file> <output_dir>
```

---

### Step 3 — Author the definition files

Edit the generated `def/` stubs:
- Add classes, attributes, and relationships to each model schema
- Add typed attributes to commands and events as needed

These files are yours — Dizzy will never overwrite them.

---

### Step 4 — Generate interfaces and source stubs

```
dizzy gen <feat_file> <output_dir>
```

Reads both the feat file and the authored `def/` files, then generates:

**`gen_def/`** — produced by running `linkml gen-pydantic` (and `linkml gen-sqla` for models)
on the authored `def/` schemas

**`gen_int/`** — Protocol stubs derived from the feat file structure (queries, procedures,
policies, projections), using the `gen_def/` types as references in imports

**`src/`** — implementation stubs, one per interface, for the developer to fill in. Each stub imports its Protocol and raises `NotImplementedError`:

```
src/
  query/
    <query_name>.py
  procedure/
    <procedure_name>.py
  policy/
    <policy_name>.py
  projection/
    <projection_name>.py
```

Source stubs are only created if the file does not already exist, so they are safe to
re-run after editing.

After running, Dizzy prints a per-section summary and:

```
Generated interfaces and source stubs. Next steps:
  Implement the src/ files to complete your feature.
```

---

### Summary

| Step | Command | You do next |
|------|---------|-------------|
| 1 | — | Write `my_feature.feat.yaml` |
| 2 | `dizzy scaffold` | Edit `def/` schema stubs |
| 3 | — | Author class definitions and attributes |
| 4 | `dizzy gen` | Implement `src/` stubs |

---

## Testing Strategy

### Architecture: split render from write

Every generator module exposes two layers:

1. **`render_*(feat, ...) -> str`** — pure function, takes parsed feat data and returns the
   file content as a string. No filesystem access. Fully unit-testable in isolation.

2. **`write_*(feat, output_dir, ...)`** — thin wrapper that calls `render_*` and writes the
   result to the correct path under `output_dir`. This layer is covered by integration tests.

This split means the majority of tests never touch the filesystem and run fast.

### Unit tests — render functions

Each `render_*` function gets direct unit tests that assert on the returned string. Tests live
in `dizzy/tests/generators/test_<section>.py`. A representative feat fixture is defined once
in `dizzy/tests/conftest.py` and shared across all generator tests.

```python
def test_render_procedure_context(recipe_feat):
    result = render_procedure_context("extract_and_transform_recipe", recipe_feat)
    assert "class extract_and_transform_recipe_context" in result
    assert "get_recipe_text: get_recipe_text_query" in result
```

### CLI tests — typer commands as plain functions

Typer commands are plain Python functions. Tests call them directly without subprocess or
`CliRunner`. The `tmp_path` pytest fixture provides the output directory.

```python
from dizzy.cli import scaffold

def test_scaffold_creates_def_stubs(tmp_path: Path) -> None:
    scaffold(feat_file=FIXTURES_DIR / "recipe.feat.yaml", output_dir=tmp_path)
    assert (tmp_path / "def" / "commands.yaml").exists()
    assert (tmp_path / "def" / "events.yaml").exists()
```

This keeps CLI tests fast (no subprocess overhead) and avoids install-time coupling. End-to-end
smoke testing against the installed binary is handled manually in Phase 7.

### Integration tests — snapshot tests (syrupy)

End-to-end tests call the full generator pipeline against a known feat fixture, write output
to a `tmp_path` directory, and compare every generated file against a saved snapshot.

Snapshots live in `dizzy/tests/snapshots/` and are committed to version control. They serve
as living documentation of exactly what each generator produces.

```python
def test_gen_full_example(tmp_path, snapshot):
    feat = load_feat("tests/fixtures/recipe.feat.yaml")
    gen(feat, tmp_path)
    for path in sorted(tmp_path.rglob("*.py")):
        assert path.read_text() == snapshot(name=str(path.relative_to(tmp_path)))
```

To update snapshots after an intentional template change:

```
pytest --snapshot-update
```

### Dev dependency

Add `syrupy` to the `dev` dependency group in `pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "pytest>=8.4.2",
    "syrupy>=4.0",
]
```

### Test layout

```
dizzy/
  tests/
    conftest.py               # shared feat fixtures
    fixtures/
      recipe.feat.yaml        # full example feat used across tests
    snapshots/                # syrupy snapshot files (committed)
    generators/
      test_models.py
      test_commands.py
      test_events.py
      test_queries.py
      test_procedures.py
      test_policies.py
      test_projections.py
    test_cli.py               # end-to-end scaffold + gen integration tests
```
