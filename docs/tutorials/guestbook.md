# Build a guestbook

In this tutorial you build the **guestbook** — the smallest feature that still
exercises both of DIZZY's loops — from an empty directory. By the end of these first
steps you'll have described the feature and generated typed schemas from it.

A DIZZY feature is described as two **loops**: a write path, where something happens and
gets recorded as a permanent fact, and a read path, where those facts are accumulated
into tables you can query. The guestbook is one lap of each:

```
sign_guestbook ─▶ record_signature ─▶ guestbook_signed              (reactivity loop)
guestbook_signed ─▶ signature_store ─▶ guestbook ─▶ list_signatures (data loop)
```

A visitor **signs** the guestbook (a *command*: a request that something happen). A
*procedure* validates it and emits a *fact* (an *event*: something that did happen, never
edited or deleted). A *projection* **folds** that fact into a *model* — it applies each
event in turn to a running read table, so the table is the accumulation of every fact so
far. A *query* reads that table back out.

!!! note "Validated tutorial"
    Every command and file on this page is executed and checked by
    `just tutorials-check` (via [byexample](https://byexamples.github.io/byexample/)),
    so it cannot silently drift from the tool. Lines beginning with `$` are commands you
    run; lines beginning with `>` continue the command above (a shell heredoc). Don't
    copy the `$`/`>` markers themselves.

## Before you start

You need DIZZY installed. It is **not published to a package index** — `uv add dizzy`
fetches an unrelated project that happens to share the name — so install it from a
checkout or straight from git, as the
[install instructions](https://github.com/PNNL/dizzy#install) describe:

```shell
$ dizzy --help | head -n 1
```

Work in a fresh directory. This tutorial's assets — the feature-file, `demo.py`, and the
patches it applies to generated files — ship under the
[tutorial source](https://github.com/PNNL/dizzy/tree/main/docs/tutorials/guestbook);
grab that folder to follow along, or just copy each block by hand as you go:

```shell
$ ls -1
demo.py
edits
guestbook.feat.yaml
```

## Step 1 — Describe the feature

The **feature-file** is the single source of truth: it declares every component of the
domain in one readable artifact. Create `guestbook.feat.yaml` with this content (it also
ships alongside the tutorial, so it's already in your working directory if you grabbed
the folder):

```yaml title="guestbook.feat.yaml"
--8<-- "tutorials/guestbook/guestbook.feat.yaml"
```

That's the whole design. Each entry names a component and, where it matters, how the
components connect (`record_signature` handles `sign_guestbook` and emits
`guestbook_signed`; `signature_store` folds `guestbook_signed` into the `guestbook`
model). The `adapters: [sqla]` on the model — and the matching `adapter: sqla` on the
projection and the query — name the storage the model is reached through; `sqla` means
SQLAlchemy, and an element only ever touches its model through the adapter it declares.
Names are `snake_case`; LinkML — the schema language DIZZY compiles the `def/` files
with — will turn them into `PascalCase` classes later.

A quick sanity check that the file is in place:

```shell
$ head -n 1 guestbook.feat.yaml
description: Guestbook — visitors sign, signatures are stored and listed
```

## Step 2 — Scaffold the schemas

`dizzy generate definitions` reads the feature-file and writes **LinkML schema stubs**
into `def/`, plus a `libconfig.yaml` that assigns a runtime to each element:

```shell
$ dizzy generate definitions guestbook.feat.yaml .
Generated def/ stubs and libconfig.yaml. Next steps:
<...>
```

Look at what it produced:

```shell
$ ls -1 def
commands.yaml
events.yaml
models
queries
```

The scaffolds are intentionally empty where *you* must decide the shape. Open the
command schema and you'll see its `attributes` left blank for you to fill:

```shell
$ cat def/commands.yaml
id: https://example.org/commands
name: commands
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
imports:
  - linkml:types
classes:
  sign_guestbook:
    description: A visitor wants to leave a signature
    attributes: {}
```

## Step 3 — Fill in the schema (patching generated files)

This is the heart of the workflow: the generator scaffolds *structure*, and you author
the *field-level detail*. The files **don't start empty** — the scaffold gave each class
everything except the fields, leaving `attributes: {}` for you. A `sign_guestbook`
command needs a visitor name and a message, so edit `def/commands.yaml`:

```diff
--8<-- "tutorials/guestbook/edits/commands.yaml.diff"
```

Each change in this tutorial ships as a patch under `edits/`, so you can apply it
directly (or just make the highlighted edit by hand):

```shell
$ git apply edits/commands.yaml.diff
$ cat def/commands.yaml
id: https://example.org/commands
name: commands
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
imports:
  - linkml:types
classes:
  sign_guestbook:
    description: A visitor wants to leave a signature
    attributes:
      visitor_name:
        range: string
        required: true
      message:
        range: string
        required: true
```

Do the same for the event. An event is an **immutable fact**, so it must carry
everything needed to replay it — its own id and a timestamp, not just the user-supplied
fields:

```diff
--8<-- "tutorials/guestbook/edits/events.yaml.diff"
```

```shell
$ git apply edits/events.yaml.diff
```

The **model** is the read-optimized table the projection will write into. Its scaffold
starts at `classes: {}`; give it a `Signature` class with an identifier:

```diff
--8<-- "tutorials/guestbook/edits/guestbook.yaml.diff"
```

And the **query** needs the shape of its input and output — here, an optional `limit` in
and a list of formatted lines out:

```diff
--8<-- "tutorials/guestbook/edits/list_signatures.yaml.diff"
```

Apply both:

```shell
$ git apply edits/guestbook.yaml.diff edits/list_signatures.yaml.diff
```

Re-running `dizzy generate definitions` now is safe — it **never clobbers** files you've
edited, so your attributes survive:

```shell
$ dizzy generate definitions guestbook.feat.yaml .
Generated def/ stubs and libconfig.yaml. Next steps:
<...>
$ grep -c 'visitor_name' def/commands.yaml
1
```

Your hand-authored attributes are still there.

## Step 4 — Compile the type packages

`dizzy generate static` runs LinkML over `def/` to produce **`gen_def`** (Pydantic +
SQLAlchemy classes) and **`gen_int`** (the interfaces your code is written against: a
*protocol* per element — the signature its implementation must match — the *context*
object each one is handed, and the *adapters* that context reaches its model through).
Both land under `lib/python-uv/` as installable packages:

```shell
$ dizzy generate static guestbook.feat.yaml .
<...>
$ ls -1 lib/python-uv
gen_def
gen_int
```

These are generated, not authored — you never edit them. They're the typed contracts the
next step builds against. The same stage also writes a runtime-neutral `gen_schema/`
beside `def/` — the same commands and queries as JSON Schema, for consumers that aren't
Python. Which contracts it emits is the `json_schema:` section of `libconfig.yaml`.

## Step 5 — Package each element

`dizzy generate libraries` reads `libconfig.yaml` (every element targets `python-uv` here)
and emits one redistributable package per element, plus the workspace `pyproject.toml`
that ties them and the type packages together:

```shell
$ dizzy generate libraries guestbook.feat.yaml .
<...>
$ ls -1 lib/python-uv
gen_def
gen_int
procedure
projection
pyproject.toml
query
```

Each element package carries a real-signature **implementation stub** in `src/<name>.py`
that raises `NotImplementedError` — the typed shape is there, the logic is yours to write:

```shell
$ cat lib/python-uv/procedure/record_signature/src/record_signature.py
# Implementation stub — fill in your logic here
<...>
    raise NotImplementedError
```

The stub already has the right typed signature — `context` and `command`, both generated
types — and leaves the body to you. You'll see the full original in the next step's diff.

## Step 6 — Implement the stubs

Three elements carry logic: the **procedure** turns a command into an event, the
**projection** folds the event into the model, and the **query** reads it back. Fill them
in — each diff replaces the `NotImplementedError` stub with a real body.

The procedure stamps identity and time (so the event is a self-contained fact) and emits
it:

```diff
--8<-- "tutorials/guestbook/edits/record_signature.py.diff"
```

The projection merges each event into the read model through the SQLAlchemy adapter:

```diff
--8<-- "tutorials/guestbook/edits/signature_store.py.diff"
```

The query reads the model back out, newest first:

```diff
--8<-- "tutorials/guestbook/edits/list_signatures.py.diff"
```

Apply all three:

```shell
$ git apply edits/record_signature.py.diff edits/signature_store.py.diff edits/list_signatures.py.diff
```

## Step 7 — Wire it up and run

Everything DIZZY generates is a typed package; a **host** supplies what the feature-file
cannot know — the database, and when the work runs. That's `demo.py`. It owns an
in-memory SQLite database, hands each emitted `guestbook_signed` event to the projection,
signs the guestbook three times, then runs the query:

```python title="demo.py"
--8<-- "tutorials/guestbook/demo.py"
```

!!! note "The routing is generated too"
    The event routing `demo.py` writes by hand — which procedure handles which command,
    which projection gets which event — is declared in the feature-file, so a fourth
    stage emits it: `dizzy generate wiring guestbook.feat.yaml .` writes
    `lib/python-uv/wiring/`, the same elements bound to a `dizzy.engine` engine. It is
    hand-wired here so you can see every connection once. What stays the host's is
    persistence and *scheduling* — where a command dispatched by a policy actually runs.
    Run `dizzy docs` for the wiring stage, and see
    [`examples/recipes`](https://github.com/PNNL/dizzy/tree/main/examples/recipes) for a
    host built on generated wiring.

Sync the generated workspace and run it:

```shell
$ uv sync --project lib/python-uv
<...>
$ uv run --project lib/python-uv python demo.py
Guestbook (newest first):
  - Edsger: Goto considered harmful
  - Grace: Compiled it
  - Ada: Hello from 1843
```

🎉 **That's the whole feature.** A command flowed through a procedure into an event, a
projection folded it into a model, and a query read it back — both of DIZZY's loops,
generated from a single feature-file and a handful of edits you made to the parts only
you could decide.

## Where next

- [A policy that consults a query](library.md) — the missing half of the reactivity
  loop: a policy that reads state to decide which command to dispatch.
- [A streaming agent turn](agent.md) — environment and telemetry, the two context
  inputs a procedure gets besides its emitters.
- [Feature-file format](../reference/SPECIFICATION.md) — every section and field the
  `.feat.yaml` accepts.
- `dizzy docs` — the CLI manpage, including the `generate wiring` stage this tutorial
  stops short of.