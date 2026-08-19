# DIZZY CLI — Manpage & Roadmap

> This page is the canonical command reference **and** the requirements document for
> follow-on work: each command section defines its end state. Printed by `dizzy docs`.
> Tracked work items live in Seeds and reference these sections.

DIZZY is a philosophy/toolchain for event-driven systems with eight components
(Command `c`, Procedure `d`, Event `e`, Policy `y`, Projection `j`, Model `m`,
Query `q`, Querier `Q`). The **feature-file** (`.feat.yaml`) declares every component,
its wiring, prose/mermaid descriptions, and (eventually) field-level slot mappings.
The feature-file is the API of the entire ecosystem: every command below reads it, and
several write to it. Treat its format as a versioned, compatibility-guaranteed spec.
(Today's feature-file expresses the query/querier pair as a single `queries:` entry —
typed Input/Output plus the callable that answers it.)

Core thesis: the design lives in the artifact, never in an LLM context window. LLMs are
replaceable components sandwiched between deterministic checks.

---

## Lifecycle overview

| Phase | Commands | Status |
|---|---|---|
| Onboarding | `onboard` | **shipped** |
| Design | `lint`, `simulate`, `diff`, `impact` | planned |
| Build | `generate` | **shipped** (python-uv; rust/ts experimental) |
| Build | `scaffold`, `verify` | planned (stub scaffolding ships inside `generate libraries`) |
| Change | `diff`, `impact`, `compat`, `rebuild` | planned |
| Run | `trace`, `drift` | planned |
| Adoption | `wrap` | planned |
| Deploy | `deploy` | planned |
| Utility | `docs`, `config` | **shipped** |

Dependency/build order: `generate` → `diff` → `impact`/`lint` → `simulate` →
`rebuild`/`compat` → `scaffold`/`verify` → `trace`/`drift` → `wrap` → `deploy`.

---

# Shipped commands

## dizzy generate

Feature-file → LinkML schemas → typed data contracts + process interfaces → per-language
library artifacts. A procedure's interface declares exactly the command it receives, the
queries it may call, the events it may emit; exceeding declared scope is a type error.
The spine of the toolchain; everything else is decoration until this works.

Generation is a four-stage pipeline with **human-in-the-loop** authoring at each
handoff. Generated interfaces are always overwritten; files you author (`def/` schemas,
`libconfig.yaml`, and the implementation stubs in `lib/`) are never clobbered.

The first three stages produce the *design*: schemas, contracts, and element stubs for
you to fill in. The fourth produces the *host*: the binding that hands those elements to
a runtime. Stages one to three answer "what is this feature"; stage four answers "how
does it run".

### dizzy generate definitions <feat_file> <output_dir>

Scaffold LinkML schema stubs into `def/` plus a `libconfig.yaml` assigning a runtime to
each element. **You then author** field-level detail: attributes on commands/events,
model classes, query input/output shapes. `--default-runtime` picks the runtime written
into the libconfig stub (default `python-uv`).

### dizzy generate static <feat_file> <output_dir>

Compile your authored `def/` schemas through the LinkML toolchain into the static type
packages under `lib/python-uv/`: `gen_def` (Pydantic + SQLAlchemy models) and `gen_int`
(typed Protocols, contexts, adapters). Each is an installable package. Requires `def/`
stubs; run `definitions` first.

### dizzy generate libraries <feat_file> <output_dir>

Read `libconfig.yaml` and package every element (procedure/policy/projection/query)
into its own redistributable per-runtime package with a real-signature implementation
stub in `src/` for you to fill in. Writes the workspace manifest; a generated
`lib/<runtime>/` is self-contained and can be lifted out and shipped on its own.

**Open requirement:** must work end-to-end in **at least two target languages** (this
is what proves the language-agnostic claim). Today `python-uv` is complete;
`rust-cargo` and `typescript-npm` generate types + stubs but not protocols/contexts.

### dizzy generate wiring <feat_file> <output_dir>

Emit the binding between the generated elements and the runtime (`dizzy.engine`) as
`lib/<runtime>/wiring/` — an installable workspace package holding `build_runtime` and
the `HostApp` a scheduling shell resolves from `$DIZZY_HOST_APP`. Requires the element
packages; run `libraries` first.

**Wiring is a pure function of the feature-file, which is why it is generated.** The feat
already declares, per element, exactly the argument list of its generated context: a
procedure's `command:` gives the command it registers under, its `emits:` the emitter
bindings, its `queries:` the query bindings, its `environment:` and `telemetry:` the
remaining constructor arguments; a projection's `event:` gives the event→projection
routing; a policy's `event:` and `emits:` give its registration and its dispatch targets.
Hand-writing that is transcription, and transcription drifts — this is the last artifact
in the toolchain that was still copied out of the design by hand.

Always overwritten, like every other generated interface. To specialize an element's
binding, pass an override by name rather than editing the file:

```python
build_runtime(services, overrides={"detect_faces": my_lazy_runner})
```

That seam exists because real hosts need it — a capability-pooled element a node must not
import, a query that has to run on a short-lived session — and an app that forks the
generated module to get it has thrown away the guarantee the generator provides.

Emitted wiring is **engine-mediated**: every emit goes to the engine and every dispatch to
its command queue, so no element ever calls another directly. That is what lets the engine
own the ordering rule (projections fold and the read model commits before policies
dispatch) instead of each generated handler restating it, and what lets the same wiring run
under either scheduling shell.

> The legacy verbs `dizzy def` / `dizzy gen` / `dizzy lib` are deprecated aliases for
> `generate definitions` / `generate static` / `generate libraries`.

## dizzy docs [page]

Print DIZZY documentation to stdout (pretty-rendered on a terminal, plain markdown when
piped — agent-friendly either way). Pages:

- `cli` (default) — this manpage/roadmap.
- `authoring` — the agent guide: every component, the `.feat.yaml` shape, what you
  author after each stage, the generated layout, import conventions.

## dizzy config

Print a template `.dizzy.yaml` configuration file (logging options).

## dizzy onboard

Prints a high-level overview of DIZZY and how to use this CLI, written for coding agents
(Claude Code, OpenCode, pi, etc.). Modeled on the `seeds` tool's `prime` verb.
Content includes: the eight components, the feature-file's role, the change taxonomy
(what's recoverable from the event stream vs. not), exemplar good/bad events with reasons,
and which command to reach for at each lifecycle step. Builds on `docs`; `onboard` is the
opinionated workflow layer over the reference pages (printed by `dizzy onboard`).

---

# Roadmap

## dizzy validate

Structural validation of a feature-file (and, when present, a scenario file):
parse, schema-check, and verify referential integrity — procedures handle declared
commands, emit declared events, query declared queries; projections/queries target
declared models; scenario commands exist in the feature-file. Exit nonzero with
actionable errors. This is the cheap, always-run gate (`load_feat` + `validate_feat`
already exist; this exposes them as a verb). `lint` layers semantic/taxonomy rules on
top of `validate`; every other command should call `validate` implicitly on load.

## dizzy lint

Deterministic checks over a feature-file. The change/event taxonomy expressed as rules:

- Events named as past-tense facts; Commands imperative.
- Fact vs. derivation: derived values (e.g. averages) must be modeled as outputs of a
  Procedure/Activity with provenance, not recorded as if observed.
- No PII-class slots in Events (route through Commands).
- Grain consistency: events sharing many slots flagged as possible same-fact-two-grains.
- Orphans: events no policy/projection consumes; procedures emitting nothing;
  queries targeting no model.
- Compat-mode checks against declared stored-event schemas.

Exit nonzero on error-level findings. Output: structured findings (JSON + pretty).
May delegate schema-level checks to existing LinkML linting where available.

## dizzy diff

Structural diff between two feature-file versions: components/edges/slots
added, removed, renamed, retyped. Foundation for `impact` (impact = diff + graph walk).
Output: machine-readable change set, classified per the change taxonomy
(additive / contract-changing / breaking / lossy-to-truth).

## dizzy impact

Question: "If I make this change to this component, what must I look at next?"

Input: a component name (coarse mode) or a `diff` change set (pointed mode).
Output: the affected **subgraph** (not a tree — the c→d→e→y→c reactivity loop means cycles
are the norm; emit as BFS spanning tree with annotated back-edges, visited-set required).
Traversal terminates at unchanged contract boundaries, not at a depth limit
(depth is a display option only). Graphical rendering of the subgraph belongs to the
separate visualization repo; this command's product is the machine-readable verdict set.

Every node gets a verdict: `must-change` | `must-review` (with the open question attached)
| `unaffected-proven` (with the proof), plus a recoverability bit
(rebuildable from event stream vs. irreducible).

Four tiers of discovery, run in order, each scoping the next:

1. **Slot lineage (deterministic).** Field-level walk of declarations: which procedures
   write a slot, which projections map it to which model columns, which queries return it.
   Sound w.r.t. what's declared. Requires feature-file/LinkML to declare projection
   mappings at field granularity — closing that spec gap is part of this work.
2. **Compiler oracle (deterministic).** Spin a git worktree, apply the change, regenerate
   contracts, run each target language's typechecker. Type errors are worklist items.
   Strength varies by language; report confidence accordingly.
3. **Event-store compat (deterministic).** Old schema vs. new: backward/forward
   compatibility verdict. Even a field nothing reads can't be naively dropped if stored
   events carry it. Also flag lossy-to-truth changes ("this field becomes unrecoverable
   for future projections") — deleting from the source of truth is a one-way door.
4. **LLM semantic review (judgment).** Given the proven-affected set from tiers 1–3,
   assess only what types can't see: does the change alter what an event *means*; is
   anything relying on a field's presence as a signal. Never used for enumeration.

Integration: `--emit seeds` writes the worklist as seeds tasks in topological order.
Keep the emitter behind a small interface so other orchestrators can be added.

## dizzy simulate

LLM-driven execution of a feature-file with **no code generation and no deployment**.
Component descriptions (prose / pseudocode / mermaid) are the prompts — author them as
YAML block scalars (`|`/`>`) with enough detail to execute (see `dizzy docs authoring`,
"Descriptions Are Design").

Initial stimulus comes from lightweight **scenario files** (`*.scenario.yaml`): a
description plus an ordered list of starting commands with prose payload sketches.
One scenario per probe; keep a collection per feature.

Scheduler: phased single-thread loop — drain the event queue, then the command queue,
repeat. This *selects one legal interleaving* (sequentially consistent best case); it does
not prove race freedom. That boundary is explicit; exhaustive interleaving checks are
future TLA+ work generated from the same feature-file.

Architecture (the constraints live in the harness, not the prompt):

- Each component activation = one narrow LLM call: the component's description, the
  triggering message, and **tools generated on the fly from declared wiring only**.
  A procedure that may emit two event types gets exactly two emit tools. No tool, no emission.
- Harness validates every emitted payload against the declared schema; nonconforming
  output is rejected, never silently accepted.
- Models must not heal gaps. Every node gets one always-present tool: `report_finding`
  (missing slot, ambiguous description, undecidable branch, unanswerable query).
  **The product of a run is its findings list; the trace is secondary.**
- Termination: step budget + repetition detector (same component, same message shape,
  N times → finding: possible livelock).

Fidelity levels:
- `--level 0` narrative: no payloads; does the story connect; finds missing layers/dead ends.
- `--level 1` slot-conformant payloads; finds contract mismatches between components.
- `--level 2` stateful: models materialize as JSON blobs in the session file (caches of
  the stream); projections propose mutations; queriers answer only from supplied state.

**The jmQ collapse (levels 0–1):** projections never activate and are never logged —
the simulated event store is the only state (the simplest projection is identity, so
the minimal model *is* the stream). The j→m→Q chain collapses into `q`: a query is
answered by a querier sub-activation given the query's description and full read access
to the event store; if the stream cannot answer, that is a finding. Feature-files may
omit projections/models early in design and declare only queries.

Session file: append-only JSONL log of commands handled / events emitted / projections
applied — itself a DIZZY event stream, versioned from the first commit. Entries carry
`id`/`parentId` so the file is a **tree** (prior art: the pi agent's session format):
branching — continue from step N with a modified feature-file — is appending an entry
whose parent is an earlier node, in place, full history in one file.

Agent compatibility (OpenCode, Claude Code, pi — minimal by design):
the harness never embeds a provider SDK as its only path. Three modes:
1. `--provider <api>` direct API calls (config-selected model; cheap models are fine
   because each call is narrow and the harness enforces structure).
2. `--exec '<cmd>'` subprocess mode: prompt on stdin, JSON tool-call response on stdout —
   any agent CLI that can run non-interactively plugs in.
3. `--manual` prints the prompt and awaits a JSON response on stdin (human or any agent
   pasted in). Mode 3 is the compatibility floor; modes 1–2 are conveniences over it.

Side benefit worth surfacing: simulation tests the *descriptions*. A mermaid diagram the
LLM misexecutes is one a new engineer will misread.

## dizzy scaffold

Generated implementation stubs for each procedure/policy/projection/querier, with the
feature-file description and mermaid embedded as the docstring, so humans or agents
implement into a frame that already carries the design. The basic stub mechanism ships
today inside `generate libraries`; `scaffold`'s end state is the docstring-carrying,
design-embedded version of those stubs.

## dizzy verify

Conformance tests generated from contracts. Includes promoting `simulate` session traces
to golden tests: "given this command, the implementation must emit events matching this
trace." Run against real implementations.

## dizzy compat

Event schema evolution checks: old vs. new schema → backward/forward/full verdict;
scaffolds upcasters for breaking changes; suggests compat-mode migrations
(optional-then-drop) for stored events.

## dizzy rebuild <model>

Replay the event stream through a projection into a fresh Model and cut over.
The payoff move of the whole architecture — polish far beyond its difficulty.
Converts what conventional shops call "migrations" into rebuilds.

## dizzy wrap

Anti-corruption adapter: present an existing REST API / legacy database as
commands/queries/events so one corner of a brownfield system can become DIZZY-shaped.
The write-side adoption story (dj scripts are the read side). No one adopts greenfield.

## dizzy deploy

Deployment profile generation from the feature-file. v1 is **three hand-written profiles**
— `monolith`, `per-component`, one `hybrid` — selected by flag. Demonstrates
"infrastructure follows."

## dizzy trace

Components log in feature-file vocabulary (command handled, events emitted — the
structured-log-pipes / `dj` work is the prototype); renders production flows as the same
c/d/e/y graph the workshop drew. Graph rendering / visualization front-ends may live in a
separate repo, but `trace` itself — the feature-file-vocabulary log pipeline — is built here.

## dizzy drift

Compares runtime traces against the feature-file: undeclared event emissions, undeclared
model access, components present in one but not the other. The feature-file as an
*enforced contract* against the running system, not documentation. Without this the map
eventually drifts from the territory — the exact disease DIZZY exists to cure.

---

## Acceptance test for the whole system

One person, one evening: workshop sketch → feature-file → `simulate` → `generate` →
implement via agent off `impact --emit seeds` tasks → `deploy monolith`.
Next evening: make one breaking event change and survive it via `impact` + `compat` +
`rebuild`. The library lending example is this test run short; the recipe app is it run long.
