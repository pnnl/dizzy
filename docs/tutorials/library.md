# A policy that consults a query

This tutorial builds a **library hold queue**, and its centerpiece is the payoff of
DIZZY's reactivity loop: a **policy that runs a query to decide which command to
dispatch**. Patrons place holds on a checked-out book; when the book is returned, a
policy consults the queue and notifies whoever is next in line.

```
return_book ─▶ process_return ─▶ book_returned
book_returned ─▶ notify_next_on_return ──(query get_next_hold)──▶ notify_next_patron
notify_next_patron ─▶ send_notification ─▶ patron_notified
```

A **policy** emits *commands only*, never events — and a **query** informs *which*
command it dispatches, and with what arguments. That conditional, read-informed dispatch
is the whole point.

!!! tip "New to DIZZY?"
    If you haven't yet, do the [Build a guestbook](guestbook.md) tutorial first — it
    covers the generation pipeline (definitions → static → libraries, with wiring as a
    fourth stage) and the file conventions this one moves through more quickly.

!!! note "Validated tutorial"
    Every command and file here is executed and checked by `just tutorials-check` (via
    [byexample](https://byexamples.github.io/byexample/)). `$` lines are commands; `>`
    lines continue the one above. Don't copy the `$`/`>` markers.

## Before you start

DIZZY installed (it is not on a package index — see the
[install instructions](https://github.com/PNNL/dizzy#install)), and the tutorial's
assets — the feature-file, `demo.py`, and the patches under `edits/` — in a fresh
directory. They ship under the
[tutorial source](https://github.com/PNNL/dizzy/tree/main/docs/tutorials/library):

```shell
$ dizzy --help | head -n 1
$ ls -1
demo.py
edits
library.feat.yaml
```

## Step 1 — Describe the feature

The feature-file declares three commands (one of them, `notify_next_patron`, is
dispatched by the policy rather than an end user), the events, the procedures, a read
model, a query, and — the star — the **policy**:

```yaml title="library.feat.yaml"
--8<-- "tutorials/library/library.feat.yaml"
```

Note the `policies:` block: `notify_next_on_return` reacts to `book_returned`, declares
it `queries: [get_next_hold]`, and `emits: [notify_next_patron]`. Declaring the query is
what lets the policy read state to make its decision.

## Step 2 — Scaffold and fill the schemas

Scaffold the LinkML schemas and runtime config:

```shell
$ dizzy generate definitions library.feat.yaml .
Generated def/ stubs and libconfig.yaml. Next steps:
<...>
```

As in guestbook, the scaffolds leave `attributes: {}` for you. Give the commands and
events their fields:

```diff
--8<-- "tutorials/library/edits/commands.yaml.diff"
```

```diff
--8<-- "tutorials/library/edits/events.yaml.diff"
```

```shell
$ git apply edits/commands.yaml.diff edits/events.yaml.diff
```

The read model is the hold queue — who is waiting for which book, and when:

```diff
--8<-- "tutorials/library/edits/holds.yaml.diff"
```

The **query** is what the policy will consult, so its shape matters: it takes a `book_id`
and returns the next `patron` — or nothing when the queue is empty (note `required: false`
on the output):

```diff
--8<-- "tutorials/library/edits/get_next_hold.yaml.diff"
```

```shell
$ git apply edits/holds.yaml.diff edits/get_next_hold.yaml.diff
```

## Step 3 — Compile and package

Compile the type packages, then split each element into its own runtime package:

```shell
$ dizzy generate static library.feat.yaml .
<...>
$ dizzy generate libraries library.feat.yaml .
<...>
$ ls -1 lib/python-uv
gen_def
gen_int
policy
procedure
projection
pyproject.toml
query
```

There's a package per element now — three procedures, the projection, the query, and the
`policy`. Each `src/<name>.py` is a stub raising `NotImplementedError`.

## Step 4 — Implement the elements

Most elements are the familiar shapes from guestbook. The **procedures** turn commands
into events:

```diff
--8<-- "tutorials/library/edits/record_hold.py.diff"
```

```diff
--8<-- "tutorials/library/edits/process_return.py.diff"
```

```diff
--8<-- "tutorials/library/edits/send_notification.py.diff"
```

The **projection** folds each placed hold into the read model, and the **query** reads
the oldest active hold for a book back out:

```diff
--8<-- "tutorials/library/edits/hold_store.py.diff"
```

```diff
--8<-- "tutorials/library/edits/get_next_hold.py.diff"
```

And the **policy** — the centerpiece. It queries the hold queue and, *only if* someone is
waiting, dispatches the notify command. A policy emits commands, never events; the
downstream `notify_next_patron → send_notification → patron_notified` chain is what
records the fact:

```diff
--8<-- "tutorials/library/edits/notify_next_on_return.py.diff"
```

Apply all six:

```shell
$ git apply edits/record_hold.py.diff edits/process_return.py.diff edits/send_notification.py.diff edits/hold_store.py.diff edits/get_next_hold.py.diff edits/notify_next_on_return.py.diff
```

## Step 5 — Wire it up and run

The host's `demo.py` owns persistence and scheduling. The interesting part is how it
binds the query into the policy's context — a closure over the read adapter — so the
policy calls `context.query.get_next_hold(...)` exactly the way it calls an emitter:

```python title="demo.py"
--8<-- "tutorials/library/demo.py"
```

Sync the workspace and run it:

```shell
$ uv sync --project lib/python-uv
<...>
$ uv run --project lib/python-uv python demo.py
Holds placed:
  - Ada holds 'dune'
  - Grace holds 'dune'
<...>
Returning 'dune':
  -> notified Ada that 'dune' is ready
<...>
Returning 'gardens-of-the-moon' (no holds):
  -> no one waiting; nothing dispatched
```

Two patrons hold `dune`; when it's returned the policy queries the queue and notifies
**Ada** (the oldest hold), not Grace. When a book with no holds comes back, the policy
queries, finds nobody, and dispatches nothing — the conditional dispatch that makes
policies-with-queries the expressive heart of the reactivity loop.

The routing this `demo.py` hand-wires — event to policy, and the policy's command back
into a procedure — is what `dizzy generate wiring` emits for you as
`lib/python-uv/wiring/`: the elements bound to a `dizzy.engine` engine. Hand-wiring it
here calls the notify chain in-process; under the engine a dispatched command lands on a
queue, and *where* it runs is the scheduling shell's choice — part of DIZZY's defined
semantics rather than a detail of this file. Run `dizzy docs` for that stage.

## Where next

- [A streaming agent turn](agent.md) — environment and telemetry as declared context
  inputs.
- [`examples/recipes`](https://github.com/PNNL/dizzy/tree/main/examples/recipes) — a
  multi-step, policy-driven cascade, and a host built on generated wiring rather than
  the hand-written routing above.
