# Tutorials

Learning-oriented lessons that take you through DIZZY hands-on. They form a ramp: the
guestbook establishes the whole four-stage pipeline end to end, the library adds the one
element the guestbook has no use for — a policy — and the agent turn adds the two sections a
host must inject. Do them in order; each assumes the one before it.

Never done event sourcing? Read [What DIZZY is](../explanation/what-is-dizzy.md) first — about
ten minutes, nothing to install.

- **[Build a guestbook](guestbook.md)** — take a feature from an empty directory all the
  way to a running demo: describe it, generate and fill in typed schemas, package each
  element, implement the stubs, and watch the signatures print back out. Every command,
  edit, and output is executed and checked by `just tutorials-check`.
- **[A policy that consults a query](library.md)** — build a library hold queue whose
  centerpiece is a policy that runs a query to decide which command to dispatch.
- **[A streaming agent turn](agent.md)** — model an LLM agent turn from a scratch API
  script, introducing environment (injected config) and telemetry (observation sinks).

See also:

- `dizzy onboard` — the agent-facing orientation.
- [`examples/`](https://github.com/PNNL/dizzy/tree/main/examples) — `recipes`, a multi-step
  policy-driven cascade running on generated wiring, and `simulate`, the reference
  feature-file and scenarios for `dizzy simulate`.
- [The feature-file specification](../reference/SPECIFICATION.md) — every section and field,
  once the tutorials have shown you what they are for.
