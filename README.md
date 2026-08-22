# DIZZY

> ⚠️ **Research code.**
> DIZZY is a work in progress.
> The Python (`python-uv`) path is the most complete;
> the `rust-cargo` and `typescript-npm` runtimes are experimental.

DIZZY generates event-sourced software from a single readable file.

You declare a feature once — the requests it accepts, the facts it records, the tables
it derives, and how they connect — in one `.feat.yaml` **feature-file**. `dizzy generate`
turns that into LinkML schemas, typed contracts, JSON Schema contracts, per-runtime
implementation stubs, and the runtime wiring that binds them together. You fill in the
bodies; you never hand-write the plumbing, and the design cannot drift from the code
because the code is generated from the design.

**New to event sourcing?** Start with
[What DIZZY is (and why events)](docs/explanation/what-is-dizzy.md) — about ten minutes,
nothing to install.


## Why DIZZY?

Because a feature declared as data can be read by more than the compiler. The
feature-file is not a config file the generator consumes and forgets — it stays the
system's map, and every tool DIZZY grows reads the same map.

That is the bet: work done once against the feature-file format applies to every feature
ever written in it. A generator for a new runtime targets the format, not your codebase.
So does the engine that schedules a feature at runtime, and so would a deployment
generator or a telemetry binding — each one written once, then available to every DIZZY
feature rather than re-integrated per project.

The cost of that bet is that the format has to carry the whole design, which is what the
feature-file is for and why it is the API of the ecosystem.

> Shipped today: schema, contract, stub and wiring generation (`python-uv` most complete),
> plus the `dizzy.engine` runtime and its two scheduling shells. Deployment generation and
> a telemetry binding are the roadmap in `dizzy docs`, not working code.

## Install

Requires **Python 3.11+**, [uv](https://docs.astral.sh/uv/), and (optionally)
[just](https://github.com/casey/just).

**DIZZY is not published to a package index.** Install it from a checkout or
straight from git — `uv add dizzy` would fetch an unrelated project that happens
to share the name.

From a clone:

```bash
uv tool install --editable ".[gen]"   # or: just install
dizzy --help
```

As a dependency of your own project, naming the source explicitly:

```bash
uv add "dizzy[gen] @ git+https://github.com/PNNL/dizzy"   # authoring: the CLI and every generator
uv add "dizzy[mp]  @ git+https://github.com/PNNL/dizzy"   # running a fleet: the Dramatiq/Redis shell
uv add "dizzy      @ git+https://github.com/PNNL/dizzy"   # the engine layer alone
```

Pin a release by appending a tag (`...dizzy@v0.1.1`); tagged builds are attached
to [GitHub Releases](https://github.com/PNNL/dizzy/releases) as wheels.

The `gen` extra carries the generator (LinkML, the CLI) and is what authoring
needs. It is an extra rather than a core dependency because DIZZY also ships a
**runtime**: `dizzy.engine` schedules a generated feature, and a worker process
that installs DIZZY for a scheduling shell should not inherit a code generator
it will never call.

## The model

A feature is built from two kinds of data and two kinds of functions, connected in two
loops:

```
Commands  ─▶  Procedures  ─▶  Events  ─▶  Policies  ─▶  Commands   (reactivity loop)
Events    ─▶  Projections ─▶  Models  ─▶  Queries   ─▶  Procedures (data loop)
```

- **Commands** — write intents ("please do this").
- **Events** — immutable facts ("this happened"). The source of truth.
- **Procedures** — handle a command, do the work, emit events.
- **Policies** — react to an event, dispatch follow-up commands.
- **Projections** — fold events into **models** (read-optimized state).
- **Queries** — read models back out.

Procedures emit an event for every effect, every fact worth recording, and every
business-level error. Those events accumulate in an event store and become the basis
for everything the system knows about itself over time.



## A minimal feature

A guestbook: visitors sign it, signatures get stored and listed. This is the smallest
definition that uses both loops.

```yaml
# guestbook.feat.yaml
description: Guestbook — visitors sign, signatures are stored and listed

commands:
  sign_guestbook: A visitor wants to leave a signature

events:
  guestbook_signed: A visitor signed the guestbook

procedures:
  record_signature:
    description: Validate the signature and record it as a fact
    command: sign_guestbook
    emits: [guestbook_signed]

models:
  guestbook:
    description: Stored guestbook signatures
    adapters: [sqla]

projections:
  signature_store:
    description: Persist each signature into the guestbook model
    event: guestbook_signed
    model: guestbook
    adapter: sqla

queries:
  list_signatures:
    description: List all guestbook signatures, newest first
    model: guestbook
    adapter: sqla
```

## The workflow

DIZZY generation is a pipeline with **human-in-the-loop** authoring at each handoff.
Generated interfaces are always overwritten; the files you author (`def/` schemas and
the implementation stubs in `lib/`) are never clobbered.

```bash
# 1. scaffold LinkML schemas + libconfig.yaml from the feat file
dizzy generate definitions  guestbook.feat.yaml ./out

#        fill in field-level detail in out/def/*.yaml
#        (attributes on commands/events, model classes, query input/output)

# 2. compile schemas → the gen_def/gen_int type packages under lib/python-uv/
dizzy generate static  guestbook.feat.yaml ./out

# 3. package each element into a redistributable per-runtime library
dizzy generate libraries  guestbook.feat.yaml ./out

#        implement the bodies in
#        out/lib/python-uv/{procedure,policy,projection,query}/<name>/src/*.py

# 4. generate the wiring: those elements bound to the runtime, ready to run
dizzy generate wiring  guestbook.feat.yaml ./out
```

What lands in `./out`:

```
out/
├── def/                 # YOU author — LinkML schemas (scaffolded, never overwritten)
├── libconfig.yaml       # YOU author — which runtime each element targets
└── lib/                 # generated — one self-contained workspace per runtime
    └── python-uv/
        ├── gen_def/      # generated — Pydantic + SQLAlchemy from your LinkML
        ├── gen_int/      # generated — typed Protocols, contexts, adapters
        ├── wiring/       # generated — elements bound to the engine + a HostApp
        └── <kind>/<name>/src/  # YOU implement — stubs (never overwritten)
```

Each runtime tree is a self-contained workspace: `gen_def` and `gen_int` are
installable packages, and every element package depends on them — so a generated
`lib/python-uv/` can be lifted out and shipped on its own.

The `wiring/` package is the one that makes it *run*: it registers each procedure
under the command it handles and each projection under the event it folds, binding
every emitter to a `dizzy.engine` engine. That binding is a pure function of the
feature-file, so it is generated rather than hand-written — the design stays in the
artifact, and the wiring cannot drift from it.

> **Naming:** you write `snake_case` element names; LinkML compiles them to
> `PascalCase` Pydantic classes (`sign_guestbook` → `SignGuestbook`). Generated code
> imports the class but keeps snake_case for runtime accessors like
> `context.emit.guestbook_signed(...)`.

## See it run

The **[Build a guestbook tutorial](docs/tutorials/guestbook.md)** takes this feature from
an empty directory all the way to a running demo — describe it, generate and fill in the
schemas, implement the stubs, and wire up a `demo.py` that prints the signatures back out:

```text
Guestbook (newest first):
  - Edsger: Goto considered harmful
  - Grace: Compiled it
  - Ada: Hello from 1843
```

Every command, edit, and output in that tutorial is executed and checked by
`just tutorials-check`. For more committed examples, see [`examples/`](examples/).

## For AI agents

DIZZY ships a reference document tuned for LLM agents (the analog of `sd prime`):

```bash
dizzy docs            # CLI manpage + roadmap (ships in dizzy/src/dizzy/docs/cli.md)
dizzy docs authoring  # agent guide: components, .feat.yaml shape, authoring surface
```

The `authoring` page explains every component, the `.feat.yaml` shape, the authoring
surface, and the generated layout in one pass. Point an agent at it before asking it
to write a feature. The `cli` page (the default) doubles as the project roadmap: each
unbuilt command's section is its requirements document.

## Project layout

This is a **uv monorepo**:

- **`dizzy/`** — the core package and generators (`dizzy/src/dizzy/`). The CLI's own
  docs ship here (`dizzy/src/dizzy/docs/`) and print via `dizzy docs` / `dizzy onboard`.
- **`dizzy/src/dizzy/engine/`** — the **runtime kit**: how a generated feature gets
  RUN. `registry` reads a feat file into an app's topology (resolving every declared
  command and event to its generated class), `ports` is the seam an app publishes
  itself through, and `st` / `mp` are the two scheduling shells — single-process and
  Dramatiq/Redis fleet. A shell schedules an app it knows nothing about: the feat
  already declares everything, so nothing is hard-coded.
- **`examples/`** — worked examples.
- **`docs/`** — the [mkdocs](https://www.mkdocs.org/) documentation site, organized by
  [Diátaxis](https://diataxis.fr/) (tutorials / how-to / reference / explanation), plus
  the maintainer whitepaper (Typst source + PDF). Run `just docs-serve` to preview it.

Common commands live in the [`justfile`](justfile) (`just test`, `just check`,
`just docs-serve`, `just whitepaper`). Configuration is documented via `dizzy config`.

## Issue tracking

This project uses [Seeds](https://github.com/jayminwest/seeds) for git-native issue
tracking. Run `sd ready` to find unblocked work; see [`CLAUDE.md`](CLAUDE.md).

## Use of AI

Portions of the code and commit history in this repository may be generated or
assisted by AI tools, reviewed before inclusion.

The whitepaper and other written documents under `docs/` are **authored and edited by
the maintainer**. AI may be used there only for review, fact-checking, and feedback —
not for authorship.

---

# License

See the [license](LICENSE) for more details.

```
This material was prepared as an account of work sponsored by an agency of the
United States Government.  Neither the United States Government nor the United
States Department of Energy, nor Battelle, nor any of their employees, nor any
jurisdiction or organization that has cooperated in the development of these
materials, makes any warranty, express or implied, or assumes any legal
liability or responsibility for the accuracy, completeness, or usefulness or
any information, apparatus, product, software, or process disclosed, or
represents that its use would not infringe privately owned rights.
 
Reference herein to any specific commercial product, process, or service by
trade name, trademark, manufacturer, or otherwise does not necessarily
constitute or imply its endorsement, recommendation, or favoring by the United
States Government or any agency thereof, or Battelle Memorial Institute. The
views and opinions of authors expressed herein do not necessarily state or
reflect those of the United States Government or any agency thereof.
 
                PACIFIC NORTHWEST NATIONAL LABORATORY
                             operated by
                               BATTELLE
                               for the
                  UNITED STATES DEPARTMENT OF ENERGY
                   under Contract DE-AC05-76RL01830
```
