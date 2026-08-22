# A streaming agent turn

This tutorial models a **single streaming agent turn** — a user sends a message, an
LLM-backed procedure streams its reply token by token, and the completed reply is recorded
as a durable fact. Along the way it introduces two context inputs beyond the earlier
tutorials: an **environment** (injected configuration, in place of `os.environ`) and
**telemetry** sinks (host-supplied observation channels).

```
send_message ─▶ run_agent_turn ─▶ user_message_sent / agent_replied   (reactivity)
                      │
                      ├─(env: llm)──────────▶ the LLM client config
                      └─(telemetry: stream_chunk, usage)──▶ live tokens + token counts
```

The arc here is the one you'd actually follow: **start from the API**, model the feature
around what you learned, then let the generated contract replace your scratch script.

!!! note "Partly validated"
    The generation pipeline on this page (through `uv sync`) is executed and checked by
    `just tutorials-check`. The **live run** — `example.py` and `demo.py` — calls a real
    LLM, so it needs an API key and its output varies; those blocks are shown for
    illustration and are *not* executed.

## Before you start

Work in a fresh directory with DIZZY installed — it is not on a package index, so follow
the [install instructions](https://github.com/PNNL/dizzy#install). This tutorial's assets
— `agent.feat.yaml`, `example.py`, `demo.py`, and the patches under `edits/` — ship under
the [tutorial source](https://github.com/PNNL/dizzy/tree/main/docs/tutorials/agent); grab
that folder to follow along. The live run additionally needs credentials for an
OpenAI-compatible provider (Step 6).

## Step 1 — Start from the API

Before modelling anything, you learn the provider's streaming API by hand. This throwaway
script streams a completion, prints each token as it arrives, and reads the aggregate
token usage from the final chunk:

```python title="example.py"
--8<-- "tutorials/agent/example.py"
```

Three things in here become the shape of the feature:

- `os.getenv(...)` for the key and base URL → an **environment** input (the host injects
  config; the procedure never reads `os.environ`).
- `print(content, ...)` per token → a **telemetry** sink (`stream_chunk`): a transport
  concern, streamed live, never recorded.
- the final `usage` → a second telemetry sink (`usage`): a turn-level observation.

Running `example.py` needs an API key, so we won't run it here — it's the sketch we model
against.

## Step 2 — Model the feature

Translate that sketch into a feature-file. Note the two new sections, `environment:` and
`telemetry:`, and how `run_agent_turn` declares what it consumes (`environment: [llm]`,
`telemetry: [stream_chunk, usage]`) alongside the events it emits:

```yaml title="agent.feat.yaml"
--8<-- "tutorials/agent/agent.feat.yaml"
```

The event stream records only completed facts (`user_message_sent`, `agent_replied`) —
never individual tokens. Streaming is a transport concern, which is exactly why it's
telemetry, not events.

!!! tip
    New to the pipeline? The [guestbook tutorial](guestbook.md) covers
    definitions → static → libraries in more detail; this one moves quickly.

## Step 3 — Scaffold and fill the schemas

```shell
$ dizzy generate definitions agent.feat.yaml .
Generated def/ stubs and libconfig.yaml. Next steps:
<...>
$ ls -1 def
commands.yaml
environment.yaml
events.yaml
telemetry.yaml
```

`environment.yaml` and `telemetry.yaml` are scaffolded just like commands and events —
with `attributes: {}` for you to fill. Give the LLM config its fields:

```diff
--8<-- "tutorials/agent/edits/environment.yaml.diff"
```

Give each telemetry sink its shape — a single `text` delta for `stream_chunk`, and the
token counts for `usage`:

```diff
--8<-- "tutorials/agent/edits/telemetry.yaml.diff"
```

And the command and events:

```diff
--8<-- "tutorials/agent/edits/commands.yaml.diff"
```

```diff
--8<-- "tutorials/agent/edits/events.yaml.diff"
```

Apply all four:

```shell
$ git apply edits/commands.yaml.diff edits/events.yaml.diff edits/environment.yaml.diff edits/telemetry.yaml.diff
```

## Step 4 — Compile and package

```shell
$ dizzy generate static agent.feat.yaml .
<...>
$ dizzy generate libraries agent.feat.yaml .
<...>
$ ls -1 lib/python-uv
gen_def
gen_int
procedure
pyproject.toml
```

There's one element here — the `run_agent_turn` procedure. Its generated context now
carries `context.env` (the environment) and `context.telemetry` (the sinks) in addition
to `context.emit`.

## Step 5 — Implement the procedure

This is where `example.py`'s logic moves into the procedure — but now reading the LLM
config from `context.env.llm` instead of `os.environ`, and forwarding tokens to
`context.telemetry.stream_chunk` instead of `print`. It emits `user_message_sent` first,
streams the reply, reports `usage`, then emits `agent_replied` with the full text:

```diff
--8<-- "tutorials/agent/edits/run_agent_turn.py.diff"
```

Confirm the generated workspace and your implementation form a coherent, installable
package:

```shell
$ uv sync --project lib/python-uv
<...>
```

## Step 6 — Run it

`demo.py` is the host that replaces `example.py`: it owns an in-memory event log, supplies
the emit closures, injects the `llm` environment, and provides the telemetry sinks — then
invokes `run_agent_turn` exactly as a real runtime would:

```python title="demo.py"
--8<-- "tutorials/agent/demo.py"
```

The live run needs provider credentials. Put them in a `.env` beside `demo.py`:

```bash title=".env"
OPENAI_API_KEY=sk-...
OPENAI_API_BASE=https://api.your-provider.example/v1
OPENAI_MODEL=google/gemma-4-26b-a4b-it
```

Then run it (this calls the live provider, so the reply and token counts will differ each
time). The text streams in token by token, the telemetry sink reports usage, and the
durable events are what remain:

```text
uv run --project lib/python-uv python demo.py "Say hi in five words"

>>> Say hi in five words

Hello there, friendly human greetings!

──── telemetry ────
prompt     :       14 tokens
completion :        7 tokens
total      :       21 tokens

[events] recorded 2: ['UserMessageSent', 'AgentReplied']
```

The tokens streamed live through telemetry; the event log recorded only the two completed
facts. That separation — transport vs. provenance — is the point of giving `run_agent_turn`
both telemetry sinks *and* events.

The full source for every step on this page lives in the tutorial's own
[asset folder](https://github.com/PNNL/dizzy/tree/main/docs/tutorials/agent) —
`example.py`, `demo.py`, and the patches under `edits/`.

## Where next

- [`examples/recipes`](https://github.com/PNNL/dizzy/tree/main/examples/recipes) — a
  multi-step, policy-driven cascade whose host runs on generated wiring bound to a
  `dizzy.engine` engine, rather than invoking elements by hand as `demo.py` does here.
- `dizzy docs` — the CLI manpage, including the `generate wiring` stage that emits that
  binding.
- [Feature-file format](../reference/SPECIFICATION.md) — the full `.feat.yaml`
  reference, including the `environment:` and `telemetry:` sections this page uses.
