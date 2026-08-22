# examples/simulate — Library lending example

The reference feature-file and scenarios for `dizzy simulate`.

`library.feat.yaml` is small enough to hold in your head and wired enough to
exercise both loops (reactivity: commands → procedures → events → policies →
commands; data: queries answered from the event store).

## Running a scenario

Run from this directory; the paths below are relative to it.

```bash
mkdir -p sessions   # not in the repo: its contents are ignored, so a clone has no sessions/
uv run dizzy simulate library.feat.yaml scenarios/catalog_one.scenario.yaml sessions/catalog_one.jsonl
uv run dizzy simulate library.feat.yaml scenarios/borrow_available_book.scenario.yaml sessions/borrow.jsonl
```

Pass `--verbose` to stream LLM output. Sessions are written to `sessions/`
(gitignored — they are ephemeral run artifacts); the session path is opened for
append, never created, so the directory must exist first.

## Scenarios

| File | What it exercises |
|---|---|
| `catalog_one.scenario.yaml` | Smoke test — one command, one procedure, no queries, no policies |
| `borrow_available_book.scenario.yaml` | Happy path — member borrows an available book; exercises the reactivity loop |

## See also

- `docs/explanation/simulate-playbook.md` — the design record: protocol rulings, findings, session log format
- `dizzy docs cli § dizzy simulate` — full CLI reference
