# Add a validated tutorial

Tutorials under `docs/tutorials/` are **executed and checked** by `just tutorials-check`
(which runs [byexample](https://byexamples.github.io/byexample/) over each page), so they
can't silently drift from the tool. This guide is the recipe for writing one. For a
worked reference, read [`docs/tutorials/guestbook.md`](../tutorials/guestbook.md) and its
asset folder `docs/tutorials/guestbook/`.

The governing rule: **the filesystem is the source of truth; markdown transcludes it.**
You never paste the same content twice.

## File layout

```
docs/tutorials/
  <name>.md              # the tutorial page
  <name>/                # its assets, staged into the sandbox at check time
    <name>.feat.yaml     # whole files the reader creates
    edits/
      commands.yaml.diff # edits to generated files (unified diffs)
      ...
```

`just tutorials-check` copies `docs/tutorials/<name>/` into a throwaway directory and runs
the page's shell steps there. So **reference assets by the relative path they'll have in
that sandbox** (`edits/commands.yaml.diff`, `<name>.feat.yaml`), not by their
`docs/tutorials/...` path.

## The three kinds of code block

Pick the block type by intent. byexample is run with `-l shell`, so it **only executes
` ```shell ` blocks and ignores every other fence** — that's what lets the other two
render freely without being run.

### 1. Commands — ` ```shell ` (executed + checked)

```` markdown
```shell
$ dizzy generate definitions guestbook.feat.yaml .
Generated def/ stubs and libconfig.yaml. Next steps:
<...>
```
````

- Lines starting with `$ ` are commands; `> ` continues the previous line (a heredoc).
- Everything below a command up to the next `$`/blank is its **expected output**, matched
  exactly.
- `<...>` matches "anything here" — use it for output you don't want to pin (long blocks,
  variable tails).
- Keep output **deterministic**. Timestamps, UUIDs, and absolute paths will fail the
  match — wildcard them with `<...>`, or avoid printing them.
- `ls` columnises under byexample's pty; use `ls -1` for one-per-line output.

### 2. New files — ` ```yaml ` / ` ```python ` (displayed, copyable)

Store the file as an asset and transclude it, so it renders with highlighting, a title,
and a copy button:

```` markdown
```yaml title="guestbook.feat.yaml"
--8<-- "tutorials/guestbook/guestbook.feat.yaml"
```
````

Because the asset is staged into the sandbox, the file is already on disk — no `cat`
needed. Anchor it with a cheap executed check so Step N still validates:

```` markdown
```shell
$ head -n 1 guestbook.feat.yaml
description: Guestbook — visitors sign, signatures are stored and listed
```
````

### 3. Edits to generated files — ` ```diff ` (red/green, applied)

Show the change to a *scaffolded* file as a unified diff (renders red/green) and apply
the same patch in a shell step:

```` markdown
```diff
--8<-- "tutorials/guestbook/edits/commands.yaml.diff"
```

```shell
$ git apply edits/commands.yaml.diff
$ cat def/commands.yaml
... filled-in content ...
```
````

A diff makes clear the file **didn't start empty** — the context lines show the
generated scaffold, and `- attributes: {}` → `+ attributes: …` shows what you added.

## Generating a diff that applies cleanly

A patch must match the scaffolder's output exactly, so derive it from a real run rather
than writing it by hand. The reliable recipe: in a throwaway directory, scaffold, commit
that baseline, make your edit, and dump the diff. `git diff` emits `a/ … b/ …` headers
that `git apply` consumes:

```shell
cd "$(mktemp -d)"
cp /path/to/your.feat.yaml .
dizzy generate definitions your.feat.yaml .
git init -q && git add -A && git commit -qm scaffold

# edit def/commands.yaml (fill in the attributes) ...

git diff def/commands.yaml > commands.yaml.diff   # → docs/tutorials/<name>/edits/
```

Two phases mirror the tutorial: capture `def/**.yaml` edits against the scaffold from
`generate definitions`, and capture the implementation edits under
`lib/<runtime>/<kind>/<name>/src/` against the stubs from `generate libraries` (run
`generate static` then `generate libraries`, commit, then edit and diff).

Tidy the patch for rendering: strip git's `diff --git`/`index` preamble so the snippet is
a plain unified diff (keep everything from the first `--- ` line). `git apply` accepts
that form and works in a bare directory with no `.git`. If the scaffolder's output ever
changes, `just tutorials-check` fails — regenerate the diff.

## Wiring it up

1. **Transclusion** already works: `pymdownx.snippets` is configured with
   `base_path: [docs]`, and `*.diff` is in `exclude_docs` so patches aren't copied into
   the site as orphan files (snippets still read them from disk).
2. **Add the page to the nav** in `mkdocs.yml` under `Tutorials:` and link it from
   `docs/tutorials/index.md`.
3. **byexample** ships in the `docs` dependency-group — nothing to install.

## Run it

```shell
just tutorials-check        # every docs/tutorials/*.md, in a sandbox
just docs-serve             # preview rendering (red/green diffs, copy buttons)
```

`tutorials-check` exits non-zero on any mismatch. Confirm your tutorial actually *guards*
against drift: change an expected output (or break a patch) and watch it go red, then
revert.

## Gotchas

- **Asset paths are sandbox-relative.** `edits/x.diff`, not `docs/tutorials/<name>/edits/x.diff`.
- **Snippets vs. raw view.** In the rendered site the diff/yaml shows inline; in *raw*
  markdown (e.g. GitHub) readers see the `--8<--` directive. The mkdocs site is the
  canonical view.
- **Determinism is the whole game.** If a step's output depends on time, randomness, or
  the network, wildcard it with `<...>` or don't print it — otherwise the check is flaky.
