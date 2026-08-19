# Dizzy Reference — AI Agent Guide

## What Is Dizzy

Dizzy is a code generator for event-driven architectures. You describe a feature in a
`.feat.yaml` file, then Dizzy scaffolds LinkML schemas and generates typed Python
protocols and implementation stubs.

The system has two feedback loops:

**Reactivity loop** — Commands flow into Procedures, which emit Events. Events trigger
Policies, which dispatch new Commands. This cycle drives all side effects.

**Data loop** — Events feed Projections, which build Models (read-optimized state).
Queries read from Models and are available to Procedures. This cycle makes data
queryable.

```
Commands → Procedures → Events → Policies → Commands  (reactivity)
Events → Projections → Models → Queries → Procedures  (data)
```

---

## The Seven Components

### Models
Named database schemas. Declare schema name + adapters in the feat file. Define actual
classes (tables/entities) in a separate LinkML file under `def/models/`.

```yaml
models:
  receipts:
    description: Schema for receipts
    adapters: [sqla, relative_filesystem]
```

Names are plural, lowercase, snake_case. Each model can have multiple adapters
(e.g., `sqla` for SQL, `relative_filesystem` for file storage).

### Commands
Write intents — requests for the system to do something. Become Pydantic data classes
after generation. Use imperative snake_case names.

```yaml
commands:
  ingest_receipt_image: Path to receipt image
```

String shorthand (`name: description`) expands to `{name, description}`.

### Events
Immutable domain facts — records of what happened. Become Pydantic data classes.
Use past-tense snake_case names.

```yaml
events:
  receipt_ingested: New Receipt Added
```

### Queries
Read operations. Each query has a typed Input, Output, and a Protocol callable.
Queries bind to one model + adapter for data access.

```yaml
queries:
  get_receipt:
    description: Retrieves a receipt by ID
    model: receipts
    adapter: sqla
```

Fields: `description` (required), `model` (optional), `adapter` (required if model set).

### Procedures
Command handlers — where business logic lives. Each procedure handles exactly one
command, may use queries for reads, and emits events as output.

```yaml
procedures:
  extract_receipt_data_from_image:
    description: Use an LLM to extract receipt data from an image
    command: ingest_receipt_image
    emits: [receipt_ingested, receipt_item_ingested]
```

Fields: `command` (required), `queries` (optional list), `emits` (optional list),
`environment` (optional list), `telemetry` (optional list).

### Policies
Event-driven reaction handlers. Each policy listens to one event, may consult queries
for reads, and **dispatches commands only** — never events. A query informs *which*
command a policy dispatches (and with what arguments); to change state, the policy emits
a command that flows through the normal command → procedure → event chain.

```yaml
policies:
  notify_next_on_return:
    description: When a book is returned, notify the next patron in the hold queue
    event: book_returned
    queries: [get_next_hold]
    emits: [notify_next_patron]
```

Fields: `event` (required), `queries` (optional list of query names), `emits` (optional
list of command names), `environment` (optional list), `telemetry` (optional list).
See the "A policy that consults a query" tutorial (`docs/tutorials/library.md`) for a
runnable policy-with-query feature.

### Projections
Read-model builders. Respond to one event and write denormalized state into a model
via an adapter. Queries read what projections write.

```yaml
projections:
  receipt_store:
    description: Stores ingested receipt in the receipts model
    event: receipt_ingested
    model: receipts
    adapter: sqla
```

Fields: `description` (required), `event` (required), `model` (optional),
`adapter` (required if model set), `environment` (optional list),
`telemetry` (optional list).

### Environment and Telemetry (context inputs)

Two optional top-level sections supply extra inputs to a function's `context`.
Neither is a component — they are injected dependencies, declared once and
referenced by name from procedures, policies, projections, and queriers.

**Environment** declares injected constants/variables — acquired from the host
in place of reading `os.environ`. Each entry names a shape you author in
`def/environment.yaml`; it surfaces as `context.env.<name>`.

**Telemetry** declares host-injected observation sinks — callables the function
invokes with a typed payload (the emitters pattern, but for observation rather
than durable facts). A telemetry call is a *transport* concern: streamed tokens,
progress, metrics. It is **never** recorded as an event. Each entry names the
payload shape you author in `def/telemetry.yaml`; it surfaces as
`context.telemetry.<name>(payload)`.

```yaml
environment:
  model: |
    The LLM model configuration (provider, model name, decoding params),
    injected by the host in place of an os env var.

telemetry:
  stream_chunk: Sink for live LLM token chunks forwarded to the SSE transport.

procedures:
  run_agent_turn:
    description: Stream an agent reply, then record the completed fact.
    command: send_message
    emits: [agent_replied]
    environment: [model]
    telemetry: [stream_chunk]
```

Any of `procedures`, `policies`, `projections`, and `queries` (the querier)
may carry `environment` and `telemetry` lists. Each referenced name must be
declared in the top-level `environment` / `telemetry` sections.

---

## Descriptions Are Design

A component's `description` is not a comment — it is the design. Downstream tooling
treats it as executable prose: `dizzy simulate` uses descriptions as the *prompts* for
component activations, and `scaffold` embeds them as docstrings. A one-line description
simulates as a one-line brain.

Use YAML block scalars to give descriptions room — `|` preserves line breaks
(pseudocode, mermaid), `>` folds prose:

```yaml
procedures:
  lend_book:
    description: |
      Decide whether a member may borrow a book, and record the outcome.

      Check availability via get_book_availability. If no copies are on the
      shelf, emit borrow_rejected with the reason. If the book has a
      reservation queue, only the member at the head of the queue may borrow.

      ```mermaid
      flowchart TD
        cmd[borrow_book] --> avail{copies available?}
        avail -- no --> rej[borrow_rejected]
        avail -- yes --> head{reserved by someone else?}
        head -- yes --> rej
        head -- no --> ok[book_borrowed]
      ```
    command: borrow_book
    queries: [get_book_availability]
    emits: [book_borrowed, borrow_rejected]
```

Rule of thumb: write the description an LLM (or a new engineer) could execute without
asking you anything. Missing branches and vague conditions become `simulate` findings.

---

## Complete .feat.yaml Example

```yaml
description: Receipts App

events:
  receipt_ingested: New Receipt Added
  receipt_item_ingested: New Receipt Item Added

commands:
  ingest_receipt_image: Path to receipt image

models:
  receipts:
    description: Schema for receipts
    adapters:
      - sqla
      - relative_filesystem

queries:
  get_receipt:
    description: Retrieves a receipt by ID
    model: receipts
    adapter: sqla
  get_receipt_image:
    description: Retrieves a raw receipt image from the filesystem
    model: receipts
    adapter: relative_filesystem

procedures:
  extract_receipt_data_from_image:
    description: Use a Large Language Model to extract receipt data from an image
    command: ingest_receipt_image
    emits:
      - receipt_ingested
      - receipt_item_ingested

projections:
  receipt_store:
    description: Stores ingested receipt in the receipts model
    event: receipt_ingested
    model: receipts
    adapter: sqla
  receipt_file_archive:
    description: Archives the ingested receipt to the filesystem
    event: receipt_ingested
    model: receipts
    adapter: relative_filesystem
```

---

## Three-Step Workflow

### Step 1: Scaffold definitions

```
uv run dizzy generate definitions <feat_file> <output_dir>
```

Reads the feat file and writes LinkML stub files into `def/`, plus a `libconfig.yaml`
that assigns a runtime to each element. These are **never overwritten** on re-run —
they are yours to edit.

### Step 2: Generate code

```
uv run dizzy generate static <feat_file> <output_dir>
```

Reads the feat file + your authored `def/` stubs. Runs the LinkML toolchain and
generates the `gen_def` and `gen_int` type packages under `lib/python-uv/` (each an
installable uv package). Requires that `def/` stubs exist; run `def` first.

### Step 3: Package per runtime

```
uv run dizzy generate libraries <feat_file> <output_dir>
```

Reads `libconfig.yaml` and emits `lib/<runtime>/`, splitting each element into its
own redistributable package (a uv workspace member for `python-uv`), and writes the
workspace manifest. Each `python-uv` element package depends on the `gen_def`/`gen_int`
packages and carries a real-signature implementation stub in `src/<name>.py` for you to
fill in. Requires that `libconfig.yaml` exists; run `def` first. The Python path is the
most complete; `rust-cargo` and `typescript-npm` are experimental.

The **Build a guestbook** tutorial (`docs/tutorials/guestbook.md`) walks the whole
pipeline end to end, and `examples/` holds further worked features.

---

## What You Author After `def`

After running `dizzy generate definitions`, fill in the LinkML stubs in `def/`:

### def/commands.yaml
Add attributes to each command class. These become typed fields on the generated
Pydantic model.

```yaml
classes:
  ingest_receipt_image:
    description: Path to receipt image
    attributes:
      image_path:
        range: string
        required: true
```

### def/events.yaml
Same pattern — add attributes to each event class.

```yaml
classes:
  receipt_ingested:
    description: New Receipt Added
    attributes:
      receipt_id:
        range: string
        required: true
      total:
        range: decimal
```

### def/models/<name>.yaml
Define the classes (entities/tables) in each model schema. Each class becomes a
Pydantic model and optionally a SQLAlchemy model.

```yaml
classes:
  Receipt:
    attributes:
      id:
        range: string
        required: true
      total:
        range: decimal
      vendor:
        range: string
```

### def/queries/<name>.yaml
Fill in `<Name>Input` and `<Name>Output` class attributes. Input defines query
parameters; Output defines the return shape.

```yaml
classes:
  GetReceiptInput:
    description: Input for get_receipt
    attributes:
      receipt_id:
        range: string
        required: true
  GetReceiptOutput:
    description: Output for get_receipt
    attributes:
      id:
        range: string
      total:
        range: decimal
      vendor:
        range: string
```

### def/environment.yaml
Present only when the feature declares an `environment` section. Fill in the
attributes of each injected-constant class. The class compiles to a Pydantic
shape surfaced as `context.env.<name>`.

```yaml
classes:
  model:
    description: The LLM model configuration injected in place of an os env var.
    attributes:
      provider:
        range: string
        required: true
      name:
        range: string
        required: true
```

### def/telemetry.yaml
Present only when the feature declares a `telemetry` section. Fill in the
attributes of each sink-payload class — the shape the function passes into
`context.telemetry.<name>(payload)`.

```yaml
classes:
  stream_chunk:
    description: A single streamed LLM token chunk.
    attributes:
      delta:
        range: string
        required: true
```

All def files use `linkml:types` imports. The key authoring surface is the
`attributes:` map on each class, using `range:` for types and `required:` for
required fields.

---

## What You Implement After `lib`

After running `dizzy generate libraries`, implement the function bodies in each element package at
`lib/python-uv/<kind>/<name>/src/<name>.py`. Each stub imports its generated types and
raises `NotImplementedError`.

### lib/python-uv/procedure/<name>/src/<name>.py
You receive `context` and `command`. Use `context.emit.<event_name>(event)` to
emit events, and `context.query.<query_name>(input)` for reads — each query field is a
host-bound closure taking just the query input (the host injects the read adapter).

```python
def extract_receipt_data_from_image(
    context: extract_receipt_data_from_image_context,
    command: IngestReceiptImage,
) -> None:
    # Use command fields (e.g., command.image_path)
    # Emit events via context.emit.receipt_ingested(event)
    raise NotImplementedError
```

If the procedure declares `environment` / `telemetry`, read injected constants
from `context.env.<name>` and push observations to `context.telemetry.<name>(payload)`.
Telemetry calls are transport-only — emit an event to record any durable fact.

### lib/python-uv/policy/<name>/src/<name>.py
You receive `event` and `context`. Use `context.emit.<command_name>(cmd)` to
dispatch new commands, and `context.query.<query_name>(input)` to consult read state
when deciding *which* command to dispatch. Policies dispatch commands only — never events.

```python
def notify_next_on_return(
    event: BookReturned,
    context: notify_next_on_return_context,
) -> None:
    # Consult read state to decide what to do...
    result = context.query.get_next_hold(GetNextHoldInput(book_id=event.book_id))
    if result.patron is None:
        return  # no one waiting -> dispatch nothing
    # ...then dispatch a command (which flows through its procedure to an event).
    context.emit.notify_next_patron(
        NotifyNextPatron(book_id=event.book_id, patron=result.patron)
    )
```

### lib/python-uv/projection/<name>/src/<name>.py
You receive `event` and `context`. Use `context.adapter` (e.g., a SQLAlchemy
session or filesystem path) to persist state.

```python
def receipt_store(
    event: ReceiptIngested,
    context: receipt_store_context,
) -> None:
    # context.adapter is a SqlaAdapter (has .session)
    # Write denormalized state for queries to read
    raise NotImplementedError
```

**House rule: a projection writes, it never commits.** The engine owns the
read-model transaction boundary — it commits once per event, after that event's
projections have folded and before its policies dispatch. A `session.commit()`
inside a fold breaks that: an event folding through two projections can be
half-committed on a crash, and a host can no longer choose a different boundary
for a different runtime.

**Write folds that can run twice.** The same event reaches a projection more
than once by design — a rebuild refolds the whole stream, replication folds a
peer's facts, and the multiprocess shell delivers at-least-once. So fold with
`merge`/assignment/set-insertion, not `+= 1`, and make status changes
one-directional. A fold that is not idempotent is not a projection bug that
shows up under load; it is one that shows up the first time anyone rebuilds.

### lib/python-uv/query/<name>/src/<name>.py
You receive `input` (typed Input model) and `context` (with adapter access).
Return the typed Output model.

```python
def get_receipt(
    input: GetReceiptInput,
    context: get_receipt_context,
) -> GetReceiptOutput:
    # context.adapter is a SqlaAdapter (has .session)
    # Query and return typed output
    raise NotImplementedError
```

Element implementation stubs are **skip-if-exists** — `dizzy generate libraries` never overwrites them.

---

## Adapter System

Models declare adapters in the feat file. Each adapter gets a generated dataclass:

| Adapter | Class | Field | Type |
|---------|-------|-------|------|
| `sqla` | `SqlaAdapter` | `session` | `sqlalchemy.orm.Session` |
| `relative_filesystem` | `RelativeFilesystemAdapter` | `root` | `pathlib.Path` |

Generated at: `lib/python-uv/gen_int/gen_int/python/adapters/<adapter_name>.py`
(imported as `gen_int.python.adapters.<adapter_name>`)

Queries and projections declare `model:` + `adapter:` to bind to a specific adapter.
Their generated context receives the corresponding adapter dataclass as
`context.adapter`.

---

## Generated File Layout

```
<output_dir>/
├── def/                              # YOUR LinkML schemas (authored after `def`)
│   ├── commands.yaml
│   ├── events.yaml
│   ├── models/<name>.yaml
│   └── queries/<name>.yaml
├── libconfig.yaml                    # YOUR runtime assignments (authored after `def`)
└── lib/                              # one self-contained workspace per runtime
    └── python-uv/
        ├── pyproject.toml            # uv workspace (lists every member below)
        ├── gen_def/                  # installable package (auto-generated by `gen`)
        │   ├── pyproject.toml
        │   └── gen_def/              # import root → `gen_def`
        │       ├── pydantic/{commands,events}.py, models/<name>.py, query/<name>.py
        │       └── sqla/models/<name>.py
        ├── gen_int/                  # installable package (auto-generated by `gen`)
        │   ├── pyproject.toml
        │   └── gen_int/              # import root → `gen_int`
        │       └── python/
        │           ├── adapters/<name>.py
        │           ├── query/<name>.py
        │           ├── procedure/<name>_context.py, <name>_protocol.py
        │           ├── policy/<name>_context.py, <name>_protocol.py
        │           └── projection/<name>_projection.py
        └── <kind>/<name>/            # one element package per procedure/policy/query/projection
            ├── pyproject.toml        # depends on gen_def + gen_int (workspace deps)
            └── src/<name>.py         # implementation stub (YOUR code)
```

- `def/` — **you author these** (LinkML schemas, never overwritten)
- `lib/python-uv/gen_def/` — auto-generated Pydantic/SQLAlchemy models, installable package
- `lib/python-uv/gen_int/` — auto-generated typed protocols, contexts, adapters, installable package
- `lib/python-uv/<kind>/<name>/src/<name>.py` — **you implement these** (stubs, never overwritten)

---

## Import Conventions

Generated files use root-relative imports from the output directory.

**Naming rule:** you author element names in `snake_case` (in the feat file and as
`def/` class keys), but the LinkML toolchain emits **`PascalCase`** Pydantic classes.
So a command `ingest_receipt_image` becomes the class `IngestReceiptImage`, and an
event `receipt_ingested` becomes `ReceiptIngested`. Generated code imports the
PascalCase class. Runtime accessors keyed by element name stay snake_case — e.g.
`context.emit.receipt_ingested(...)`.

- Commands: `from gen_def.pydantic.commands import <CommandClass>`  (PascalCase)
- Events: `from gen_def.pydantic.events import <EventClass>`  (PascalCase)
- Query IO: `from gen_def.pydantic.query.<name> import <Name>Input, <Name>Output`
- Model classes: `from gen_def.pydantic.models.<name> import <ClassName>`
- Protocols: `from gen_int.python.procedure.<name>_protocol import <name>_protocol`
- Contexts: `from gen_int.python.procedure.<name>_context import <name>_context`
- Adapters: `from gen_int.python.adapters.<adapter> import <Adapter>Adapter`

`gen_def` and `gen_int` are installable packages and every element package depends on
them, so imports resolve once the `lib/python-uv/` workspace is synced
(`uv sync --project <output_dir>/lib/python-uv`) — no `sys.path` manipulation needed.
