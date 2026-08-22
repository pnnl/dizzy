# Dizzy examples

The minimal features — **guestbook**, the **policy that consults a query**, and the
**streaming agent turn** — are now full, validated walkthroughs under
[`docs/tutorials/`](../docs/tutorials/), built end to end and checked by
`just tutorials-check`. Start there.

This directory holds the worked features that don't (yet) have a tutorial. They commit
their **feature-file, authored schemas, and element implementations**; the compiled type
packages (`gen_def`/`gen_int`) are generated on demand, so build them before running.

| Example | What it shows |
|---------|---------------|
| [`recipes/`](recipes/) | A **multi-step, policy-driven cascade** over W3C PROV-style events. Three chained recipes (starter ▶ loaf ▶ croutons) where each output feeds the next; batches open *blocked* and a policy advances them as upstream entities are produced. Steps are typed data, not text. Runs via `demo.py` (CLI), a FastAPI server (`server.py`), and a browser UI. |
| [`simulate/`](simulate/) | The reference feature-file and scenarios for **`dizzy simulate`** — a small library-lending feature exercising both loops, plus the scenarios an LLM executes it against. Simulation input, not a runnable app: there is no `lib/` and nothing to sync. |

## Running an example

Regenerate the type packages, the element packages and the wiring, sync the workspace,
then run the demo inside it:

```bash
# from the repo root:
uv run dizzy generate static examples/recipes/recipes.feat.yaml examples/recipes
uv run dizzy generate libraries examples/recipes/recipes.feat.yaml examples/recipes
uv run dizzy generate wiring examples/recipes/recipes.feat.yaml examples/recipes --dizzy-source ../../../..
uv sync --project examples/recipes/lib/python-uv
uv run --project examples/recipes/lib/python-uv python examples/recipes/demo.py
```

> `generate libraries` rewrites the workspace's `pyproject.toml`, which drops the
> `wiring` member and the `dizzy` source entry only `generate wiring` writes — so the
> wiring stage must follow it, or `uv sync` silently resolves without either and
> `demo.py` dies on `import wiring`. `--dizzy-source ../../../..` (resolved from the
> generated workspace root) points that entry at this checkout; DIZZY is not published to
> a package index, so the wiring package always names a source — omit the flag and it
> names the canonical git URL instead.

> Each `demo.py` imports the example's **generated** packages, so it must run inside that
> example's own uv workspace — hence the `--project .../lib/python-uv` flag. A plain
> `uv run demo.py` uses the repo environment and fails with
> `ModuleNotFoundError: No module named 'gen_def'`.
