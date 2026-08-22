# DIZZY Onboarding — Agent Overview

> Printed by `dizzy onboard`. This is the opinionated workflow layer for coding agents
> (Claude Code, OpenCode, pi, ...). Reference pages live under `dizzy docs`:
> `dizzy docs cli` (manpage + roadmap) and `dizzy docs authoring` (the `.feat.yaml`
> authoring guide). Read this first; reach for those when you need detail.

## What DIZZY is

DIZZY is a philosophy/toolchain for event-driven systems. The design lives in one
artifact — the **feature-file** (`.feat.yaml`) — never in an LLM context window.
Every `dizzy` command reads the feature-file; several write to it. If you are an agent
working on a DIZZY project, the feature-file is your map and your contract: change it
deliberately, and regenerate rather than hand-edit generated code.

Two loops drive everything:

```
Commands → Procedures → Events → Policies → Commands   (reactivity loop)
Events → Projections → Models → Queriers → Procedures  (data loop)
```

## The eight components

| Component | Symbol | Role | Naming |
|---|---|---|---|
| Command | `c` | Write intent — a request to do something | imperative snake_case (`ingest_receipt_image`) |
| Procedure | `d` | Handles exactly one command; may call queries; emits events | snake_case verb phrase |
| Event | `e` | Immutable fact — a record of what happened; the source of truth | past-tense snake_case (`receipt_ingested`) |
| Policy | `y` | Reacts to one event; may dispatch new commands | snake_case |
| Projection | `j` | Folds events into a model (read-optimized state) | snake_case |
| Model | `m` | Named read-side schema, served through adapters | plural snake_case (`receipts`) |
| Query | `q` | Typed question — Input/Output contract | snake_case |
| Querier | `Q` | Answers a query from a model via an adapter | snake_case |

Today's feature-file expresses the query/querier pair as a single `queries:` entry —
typed Input/Output plus the callable that answers it.

## The feature-file's role

`.feat.yaml` declares every component, its wiring (which command a procedure handles,
which events it may emit, which model a querier reads), and prose/mermaid descriptions.
It is the API of the entire ecosystem and a versioned, compatibility-guaranteed spec.
A procedure's generated interface declares exactly the command it receives, the queries
it may call, the events it may emit; exceeding declared scope is a type error.

Rules of engagement for agents:

1. Never encode design decisions only in conversation — put them in the feature-file.
2. Never hand-edit generated interfaces (`gen_def`/`gen_int`) or the generated
   `wiring/` package; regenerate instead.
3. Files you author (`def/` schemas, `libconfig.yaml`, implementation stubs in `lib/`)
   are never clobbered by the generator — they are yours.

## Change taxonomy

Classify every change to the feature-file or its schemas before making it:

- **Additive** — new component, new optional field. Safe; regenerate and move on.
- **Contract-changing** — a slot's type, name, or requiredness changes. Consumers must
  be revisited; the typechecker is your first oracle.
- **Breaking** — stored events no longer parse under the new schema. Needs an upcaster
  or a compat-mode migration (optional-then-drop).
- **Lossy-to-truth** — deletes or stops recording information from the event stream.
  **One-way door.** A field nothing reads today still cannot be naively dropped:
  future projections can never recover it.

The recoverability test: Models are *rebuildable* — replay the event stream through a
projection and you get them back, so model/projection/querier changes are cheap.
Events are *irreducible* — what was never recorded is gone forever. Spend your caution
budget on the event side.

## Exemplar events — good and bad

Good:

- `receipt_ingested { receipt_id, image_path, ingested_at }` — past-tense fact,
  observed data only, carries identity and provenance.
- `recipe_extraction_failed { recipe_id, reason }` — failures are facts too; recording
  them keeps the stream honest.

Bad (and why):

- `ingest_receipt` as an event — imperative; that is a command. Events state what
  *happened*, commands request what *should happen*.
- `average_order_value_updated { avg }` — a derivation recorded as if observed. Derived
  values must be outputs of a procedure with provenance, or live in a model rebuilt by
  a projection — never masquerade as facts.
- `user_registered { email, password_hash }` — PII-class slots in an immutable stream
  you can never delete from. Route sensitive data through commands and models.
- `order_placed` and `order_submitted` sharing most slots — the same fact at two
  grains. Pick one grain per fact.

## Command per lifecycle step

| Step | You want to... | Reach for | Status |
|---|---|---|---|
| Onboard | Orient yourself / a fresh agent session | `dizzy onboard`, `dizzy docs` | shipped |
| Design | Author the feature-file | `dizzy docs authoring` | shipped |
| Design | Execute the design before implementing it | `dizzy simulate <feat> <scenario>` | shipped (level 0) |
| Design | Check the feature-file deterministically | `lint` | planned |
| Build | Scaffold schemas, generate types + packages, bind them to the engine | `dizzy generate definitions` → `static` → `libraries` → `wiring` | shipped (python-uv) |
| Change | Assess a feature-file edit | `diff`, `impact`, `compat` | planned |
| Migrate | Replace a model after a schema change | `rebuild <model>` | planned |
| Run | Observe / compare runtime vs. design | `trace`, `drift` | planned |
| Adopt | Wrap a legacy API as commands/events | `wrap` | planned |
| Deploy | Emit a deployment profile | `deploy` | planned |

The shipped path today (run in order, authoring between each step):

```
dizzy generate definitions <feat_file> <output_dir>   # scaffold def/ + libconfig.yaml — then YOU author field detail
dizzy generate static      <feat_file> <output_dir>   # compile def/ → gen_def + gen_int type packages
dizzy generate libraries   <feat_file> <output_dir>   # per-runtime element packages — then YOU implement the stubs
dizzy generate wiring      <feat_file> <output_dir>   # bind the elements to a dizzy.engine engine; emits the HostApp
```

The wiring package (`lib/python-uv/wiring/`) is a pure function of the feature-file:
it registers each procedure under the command it handles, each projection under the
event it folds, each policy under the event it reacts to. Never hand-edit it —
regenerate, and pass an override by name if one element needs a different binding.
What you author around it is the host: which database, which adapter instance, and
how the command queue gets drained.

For the full per-command reference and roadmap: `dizzy docs cli`.
For the authoring contract (what you write after each stage): `dizzy docs authoring`.
