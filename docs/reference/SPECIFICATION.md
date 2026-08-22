# Dizzy Feature File Specification

## Overview

A `.feat.yaml` file is the primary authoring surface in Dizzy. It describes a single feature's
domain model and processing logic at a high level of abstraction — without specifying databases,
frameworks, or infrastructure.

The generator pipeline reads a `.feat.yaml` and produces:
- LinkML schema files (`def/`) for each section
- Generated Python models and interfaces (`lib/<runtime>/gen_def/`, `lib/<runtime>/gen_int/`)
- One package per declared element under `lib/<runtime>/<kind>/<name>/`, each carrying an
  implementation stub for you to fill in
- The generated `lib/<runtime>/wiring/` package, which binds those elements to a
  `dizzy.engine` engine — the only generated package that depends on DIZZY itself
- Runtime-neutral JSON Schema contracts (`gen_schema/`), when `libconfig.yaml` asks for
  them — see [JSON Schema contracts](#json-schema-contracts)

---

## Top-Level Structure

```yaml
description: <string>   # Human-readable description of the feature (optional)

models:      <map>       # Named database schemas + their adapter bindings
queries:     <map>       # Read interfaces (input + output)
commands:    <map>       # Write intents
events:      <map>       # Immutable facts (what happened)
procedures:  <map>       # Command handlers (do work, emit events)
policies:    <map>       # Event handlers (react, issue commands)
projections: <map>       # Read-model builders (event → queryable state)
environment: <map>       # Injected constants/variables (in place of os env)
telemetry:   <map>       # Injected observation sinks (callables)
```

All sections are optional. The generator skips sections not present. Every entry is keyed by a
snake_case name; a bare string value is shorthand for `description`. The feat schema forbids
keys it does not declare, so a typo or an invented field is a load error, not a silent no-op.

---

## Section Definitions

### `models`
Named database schemas — each entry represents a logical grouping of related classes (tables)
for a single database. The feat file declares schema names, optional descriptions, and the
adapters through which the schema is reached. The actual classes are defined in the
corresponding `def/models/<schema_name>.yaml` LinkML file, which is authored separately and
may grow over time without touching the feat file.

The generator creates a stub `def/models/<schema_name>.yaml` if one does not already exist, then
generates one output file per schema per target backend. Use plural, lowercase names.

```yaml
models:
  recipes:
    description: Full recipe database — recipes, steps, and ingredients
    adapters: [sqla]
```

Fields:
- `description` (optional): what this schema holds. A bare string entry
  (`recipes: Full recipe database`) is shorthand for a map with only this field.
- `adapters` (optional): named adapter bindings for this schema. A query or projection that
  reads or writes the model names one of them in its own `adapter:` field, and referencing an
  adapter the model does not declare is a validation error. The generator emits one dataclass
  per named adapter at `gen_int/python/adapters/<adapter>.py`; the registry in
  `dizzy/src/dizzy/generators/adapters.py` currently knows `sqla` (carrying a SQLAlchemy
  `Session`) and `relative_filesystem` (carrying a root `Path`).

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

**`generate definitions` generates** (stub, never overwritten):
- `def/models/<schema_name>.yaml` — stub LinkML schema

**`generate static` generates** (by running the LinkML toolchain on the authored stub):
- `gen_def/pydantic/models/<schema_name>.py` — Pydantic models (via `linkml gen-pydantic`)
- `gen_def/sqla/models/<schema_name>.py` — SQLAlchemy models (via `linkml gen-sqla`)

---

### `queries`
Named read operations. A query may declare the single model schema it reads from and the
adapter it reaches it through, but IO types are not specified in the feat file — those are
defined in authored LinkML stubs and fleshed out when the implementation is written.

Each query decomposes into **three composable elements**:

- **`QueryInput`** — a LinkML-defined data shape for the query's input parameters
- **`QueryOutput`** — a LinkML-defined data shape for the query's return value
- **`QueryProcess`** — a Protocol for the callable that accepts a `QueryInput` and a context
  (holding the declared adapter) and returns a `QueryOutput`

```yaml
queries:
  get_recipe_text:
    description: Retrieves raw recipe text given a source reference
    model: recipes
    adapter: sqla
  get_recipe:
    description: Retrieves a structured recipe by ID
    model: recipes
    adapter: sqla
```

Fields:
- `description` (required): what this query does
- `model` (optional): the schema name from `models` that this query reads from
- `adapter` (required when `model` is set, and rejected without it): which of that model's
  declared `adapters` this query reaches it through
- `environment` (optional): list of `environment` entry names injected as `context.env.<name>`
- `telemetry` (optional): list of `telemetry` sink names callable as `context.telemetry.<name>(payload)`

**`generate definitions` generates** (stub, never overwritten):
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

**`generate static` generates** (by running `linkml gen-pydantic` on the authored def stub, then deriving the Protocol from the feat file):
- `gen_def/pydantic/query/<query_name>.py` — Pydantic models for both `<QueryName>Input` and `<QueryName>Output` (via linkml)
- `gen_int/python/query/<query_name>.py` — `QueryProcess` Protocol + context dataclass:

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Protocol

from gen_def.pydantic.query.get_recipe_text import GetRecipeTextInput, GetRecipeTextOutput
from gen_int.python.adapters.sqla import SqlaAdapter


@dataclass
class get_recipe_text_context:
    adapter: SqlaAdapter


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
Named write intents. Value is either a short description string, or a map carrying `name` and
`description`. A command entry declares no fields: the payload shape is authored afterwards in
the generated `def/commands.yaml` LinkML stub. Anything else in the entry is rejected — the
feat schema forbids extra keys.

```yaml
commands:
  ingest_recipe_text: Initiates ingestion of a recipe from a raw text source

  upload_blob_using_manifest:
    description: Uploads a blob using manifest information
```

**`generate definitions` generates** (stub, never overwritten):
- `def/commands.yaml` — LinkML stub with one empty class per command, where you author the
  attributes

**`generate static` generates** (by running `linkml gen-pydantic` on the authored stub):
- `gen_def/pydantic/commands.py` — Pydantic models for all commands, PascalCase-named
  (`ingest_recipe_text` → `IngestRecipeText`)

---

### `events`
Immutable domain facts, named in the past tense. As with commands, an event entry carries only
`name` and `description`; its payload shape is authored afterwards in the generated
`def/events.yaml` LinkML stub.

```yaml
events:
  recipe_ingested: A recipe was successfully ingested and validated

  scan_item_found:
    description: Found a file while scanning
```

**`generate definitions` generates** (stub, never overwritten):
- `def/events.yaml` — LinkML stub with one empty class per event, where you author the
  attributes

**`generate static` generates** (by running `linkml gen-pydantic` on the authored stub):
- `gen_def/pydantic/events.py` — Pydantic models for all events, PascalCase-named
  (`recipe_ingested` → `RecipeIngested`)

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

**`generate static` generates:**
- `gen_int/python/procedure/<procedure_name>_context.py` — context dataclass with `_emitters` and `_queries` nested dataclasses:

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Callable

from gen_def.pydantic.events import RecipeIngested
from gen_def.pydantic.query.get_recipe_text import GetRecipeTextInput, GetRecipeTextOutput


@dataclass
class extract_and_transform_recipe_emitters:
    recipe_ingested: Callable[[RecipeIngested], None]


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

from gen_def.pydantic.commands import IngestRecipeText
from gen_int.python.procedure.extract_and_transform_recipe_context import (
    extract_and_transform_recipe_context,
)


class extract_and_transform_recipe_protocol(Protocol):
    """Queries raw recipe text via source_ref, then uses an LLM to extract a structured recipe."""

    def __call__(
        self,
        context: extract_and_transform_recipe_context,
        command: IngestRecipeText,
    ) -> None:
        ...
```

When the procedure declares `environment` or `telemetry`, the same module also carries
`<procedure_name>_env` and `<procedure_name>_telemetry` dataclasses and the matching `env` /
`telemetry` fields on the context.

**`generate libraries` generates:**
- `lib/<runtime>/procedure/<procedure_name>/` — a package holding `pyproject.toml` and
  `src/<procedure_name>.py`, an implementation stub raising `NotImplementedError`
  (skipped if the source file already exists)

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

**`generate static` generates:**
- `gen_int/python/policy/<policy_name>_context.py` — context dataclass with emitters and (when declared) queries nested dataclasses (mirrors procedure context):

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Callable

from gen_def.pydantic.commands import CreateImagePriorityManifest
from gen_def.pydantic.query.get_pending_scan_count import (
    GetPendingScanCountInput,
    GetPendingScanCountOutput,
)


@dataclass
class trigger_priority_manifest_emitters:
    create_image_priority_manifest: Callable[[CreateImagePriorityManifest], None]


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

from gen_def.pydantic.events import ScanComplete
from gen_int.python.policy.trigger_priority_manifest_context import (
    trigger_priority_manifest_context,
)


class trigger_priority_manifest_protocol(Protocol):
    """Issues command to create image priority manifest when scan completes"""

    def __call__(
        self, event: ScanComplete, context: trigger_priority_manifest_context
    ) -> None:
        ...
```

**`generate libraries` generates:**
- `lib/<runtime>/policy/<policy_name>/` — a package holding `pyproject.toml` and
  `src/<policy_name>.py`, an implementation stub (skipped if the source file already exists)

---

### `projections`
Build queryable read models in response to a single event. Each projection listens to exactly
one event and writes into at most one model schema.

A projection is structurally similar to a procedure: it receives an **event** and a **context**
object, then persists state through the adapter the context carries. It emits nothing — a
projection is the only element that writes a read model, and the read model commits before any
policy dispatches.

```yaml
projections:
  recipe_library:
    description: Adds ingested recipe to the recipe library
    event: recipe_ingested
    model: recipes
    adapter: sqla
```

Fields:
- `description` (required): what this projection does
- `event` (required): the single event that triggers this projection
- `model` (optional): the schema name from `models` that this projection writes into
- `adapter` (required when `model` is set, and rejected without it): which of that model's
  declared `adapters` this projection writes through
- `environment` (optional): list of `environment` entry names injected as `context.env.<name>`
- `telemetry` (optional): list of `telemetry` sink names callable as `context.telemetry.<name>(payload)`

**`generate static` generates:** `gen_int/python/projection/<projection_name>_projection.py` —
a context dataclass and a Protocol stub:

```python
# AUTO-GENERATED — do not edit
from dataclasses import dataclass
from typing import Protocol

from gen_def.pydantic.events import RecipeIngested
from gen_int.python.adapters.sqla import SqlaAdapter


@dataclass
class recipe_library_context:
    adapter: SqlaAdapter


class recipe_library_projection(Protocol):
    """Adds ingested recipe to the recipe library"""

    def __call__(self, event: RecipeIngested, context: recipe_library_context) -> None:
        """Apply the projection — mutate model state in response to the event."""
        ...
```

A projection that declares no `model` gets a context whose body is `pass`; it reaches nothing
the feat file knows about.

**`generate libraries` generates:**
- `lib/<runtime>/projection/<projection_name>/` — a package holding `pyproject.toml` and
  `src/<projection_name>.py`, an implementation stub (skipped if the source file already exists)

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

`generate definitions` scaffolds `def/environment.yaml`.

**`generate static` generates:** `gen_def/pydantic/environment.py` (compiled from
`def/environment.yaml`, one PascalCase class per entry), and — for any function that lists the
entry — a `<function_name>_env` dataclass with one field per declared entry, plus an `env`
field on that function's context.

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

`generate definitions` scaffolds `def/telemetry.yaml`.

**`generate static` generates:** `gen_def/pydantic/telemetry.py` (compiled from
`def/telemetry.yaml`), and — for any function that lists the entry — a
`<function_name>_telemetry` dataclass of `Callable[[Payload], None]` sinks plus a `telemetry`
field on that function's context.

---

## Full Example

This feature validates and generates as-is.

```yaml
description: Recipe App

models:
  recipes:
    description: Full recipe database — recipes, steps, and ingredients
    adapters: [sqla]

queries:
  get_recipe_text:
    description: Retrieves raw recipe text given a source reference
    model: recipes
    adapter: sqla
  get_recipe:
    description: Retrieves a structured recipe by ID
    model: recipes
    adapter: sqla

commands:
  ingest_recipe_text: Initiates ingestion of a recipe from a raw text source
  reindex_recipe: Rebuilds the search index entry for one recipe

events:
  recipe_ingested: A recipe was successfully extracted and validated
  recipe_indexed: A recipe's search index entry was rebuilt

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
    environment:
      - model
    telemetry:
      - stream_chunk

  rebuild_recipe_index:
    description: Rebuilds the search index entry for the named recipe
    command: reindex_recipe
    queries:
      - get_recipe
    emits:
      - recipe_indexed

policies:
  reindex_on_ingest:
    description: Dispatches a reindex once a recipe has been ingested
    event: recipe_ingested
    emits:
      - reindex_recipe

projections:
  recipe_library:
    description: Adds ingested recipe to the recipe library
    event: recipe_ingested
    model: recipes
    adapter: sqla

environment:
  model: The LLM model configuration injected in place of an os env var.

telemetry:
  stream_chunk: Sink for live LLM token chunks forwarded to the SSE transport.
```

---

## Generator Output Layout

Given the example above at `app/my_feature/my_feature.feat.yaml` and `app/my_feature/` as the
output directory, the four stages produce:

```
app/my_feature/
  my_feature.feat.yaml            # authored
  libconfig.yaml                  # generate definitions — which runtime builds each element
  def/                            # generate definitions scaffolds, you author
    models/
      recipes.yaml
    queries/
      get_recipe_text.yaml
      get_recipe.yaml
    commands.yaml
    events.yaml
    environment.yaml
    telemetry.yaml
  gen_schema/                     # generate static — runtime-neutral JSON Schema contracts
    commands.schema.json
    queries/
      get_recipe_text.schema.json
      get_recipe.schema.json
  lib/
    python-uv/
      pyproject.toml              # the uv workspace tying the packages below together
      gen_def/                    # generate static — compiled LinkML types
        pyproject.toml
        gen_def/
          pydantic/
            models/recipes.py
            query/get_recipe_text.py
            query/get_recipe.py
            commands.py
            events.py
            environment.py
            telemetry.py
          sqla/
            models/recipes.py
      gen_int/                    # generate static — protocols, contexts, adapters
        pyproject.toml
        gen_int/
          python/
            adapters/sqla.py
            query/get_recipe_text.py
            query/get_recipe.py
            procedure/extract_and_transform_recipe_context.py
            procedure/extract_and_transform_recipe_protocol.py
            policy/reindex_on_ingest_context.py
            policy/reindex_on_ingest_protocol.py
            projection/recipe_library_projection.py
      query/                      # generate libraries — one package per element
        get_recipe_text/{pyproject.toml, src/get_recipe_text.py}
        get_recipe/{pyproject.toml, src/get_recipe.py}
      procedure/
        extract_and_transform_recipe/{pyproject.toml, src/extract_and_transform_recipe.py}
        rebuild_recipe_index/{pyproject.toml, src/rebuild_recipe_index.py}
      policy/
        reindex_on_ingest/{pyproject.toml, src/reindex_on_ingest.py}
      projection/
        recipe_library/{pyproject.toml, src/recipe_library.py}
      wiring/                     # generate wiring — elements bound to a dizzy.engine engine
        pyproject.toml
        src/wiring.py
        src/my_feature.feat.yaml
```

def: definitions
gen_def: generated definitions
gen_int: generated interfaces

`dizzy generate static` also emits an empty `__init__.py` in every generated directory so that
each type package is importable and root-relative imports resolve correctly.

`wiring/` is the only generated package that depends on DIZZY itself; the workspace root's
`[tool.uv.sources]` names where to resolve it from, since DIZZY is not published to a package
index. Everything else under `lib/python-uv/` depends only on `gen_def` and `gen_int`.

Sections with no content in the feat file produce no output, and an element bound to no runtime
in `libconfig.yaml` gets no package.

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

All generated files import by package name, rooted at `gen_def` and `gen_int`. The intent is
that the whole `lib/<runtime>/` workspace is portable — it can be lifted out and built
elsewhere without rewriting a single import, because every element package depends on those two
packages by name and the workspace resolves them locally.

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
| `query/<name>/src/` | Query Protocol + context | `from gen_int.python.query.<name> import ...` |
| `procedure/<name>/src/` | Procedure Protocol + context | `from gen_int.python.procedure.<name>_protocol import ...` |
| `policy/<name>/src/` | Policy Protocol + context | `from gen_int.python.policy.<name>_protocol import ...` |
| `projection/<name>/src/` | Projection Protocol + context | `from gen_int.python.projection.<name>_projection import ...` |

The element paths in the last four rows are relative to `lib/<runtime>/`.

---

## CLI Workflow

Generation is a four-stage pipeline. The stages are separate because human authorship sits
between them: `def/` schemas and element implementations cannot be derived from the feat file
alone. Files you author — `def/` schemas, `libconfig.yaml`, and the implementation stubs under
`lib/` — are never clobbered by a re-run.

### Step 1 — `dizzy generate definitions <feat_file> <output_dir>`

Reads the feat file and scaffolds everything that requires human schema authorship before code
can be generated, plus the runtime assignment file:

- `def/models/<schema_name>.yaml` — stub LinkML schema per model
- `def/queries/<query_name>.yaml` — stub with `<QueryName>Input` and `<QueryName>Output` classes
- `def/commands.yaml`, `def/events.yaml` — one empty class per declared name
- `def/environment.yaml`, `def/telemetry.yaml` — when the feat declares those sections
- `libconfig.yaml` — which runtime builds each element, plus the `json_schema` section

Each is skipped if it already exists. Dizzy then prints:

```
Generated def/ stubs and libconfig.yaml. Next steps:
  1. Fill in class definitions in def/models/*.yaml
  2. Add input/output shapes in def/queries/*.yaml
  3. Add attributes to def/commands.yaml and def/events.yaml
  4. Review runtimes in libconfig.yaml
  5. Run: dizzy generate static <feat_file> <output_dir>
  6. Run: dizzy generate libraries <feat_file> <output_dir>
```

### Step 2 — author the definition files

Add classes, attributes, and relationships to each model schema; add typed attributes to
commands, events, query inputs and outputs. These files are yours — Dizzy will never overwrite
them. Review `libconfig.yaml` while you are here: an element bound to no runtime gets no
package in stage 3.

### Step 3 — `dizzy generate static <feat_file> <output_dir>`

Reads both the feat file and the authored `def/` files, then generates the two type packages
under `lib/python-uv/` — `gen_def/` by running `linkml gen-pydantic` (and `linkml gen-sqla` for
models) on the authored schemas, and `gen_int/` by deriving protocols, contexts, and adapters
from the feat file. It also emits the JSON Schema contracts named by `libconfig.yaml`'s
`json_schema` section:

```
Generated 3 JSON Schema contract(s).
Generated lib/python-uv/gen_def and lib/python-uv/gen_int type packages.
  Run: dizzy generate libraries <feat_file> <output_dir> to generate element packages.
```

### Step 4 — `dizzy generate libraries <feat_file> <output_dir>`

Emits one package per element bound to a runtime in `libconfig.yaml`, each with its own
`pyproject.toml` and an implementation stub under `src/` that raises `NotImplementedError`.
Existing implementations are left alone, so the command is safe to re-run.

```
Generated lib/ packages. Implement the stubs in lib/<runtime>/<kind>/<name>/src/
```

### Step 5 — `dizzy generate wiring <feat_file> <output_dir>`

Emits `lib/python-uv/wiring/`: every element resolved and bound to a `dizzy.engine` engine,
plus the `HostApp` a scheduling shell resolves through `$DIZZY_HOST_APP`. This is the only
generated package that depends on DIZZY itself, so the command writes a `[tool.uv.sources]`
entry naming where to resolve DIZZY from — a git URL by default, or a local checkout with
`--dizzy-source <path>`. Today wiring is emitted for `python-uv` only, and the command exits
nonzero if `libconfig.yaml` binds no element to that runtime.

```
Generated lib/python-uv/wiring/. Build a HostApp with wiring.host_app(...) and point $DIZZY_HOST_APP at it.
```

### Summary

| Step | Command | You do next |
|-------|---------|-------------|
| 1 | `dizzy generate definitions` | Author the `def/` schemas |
| 2 | — | Review the runtime bindings in `libconfig.yaml` |
| 3 | `dizzy generate static` | Nothing — the type packages are fully derived |
| 4 | `dizzy generate libraries` | Implement each element's `src/` stub |
| 5 | `dizzy generate wiring` | Write a host that builds a `HostApp` from the generated wiring |

`def`, `gen`, and `lib` survive as hidden deprecated aliases for `generate definitions`,
`generate static`, and `generate libraries`. New work should use the full names.
