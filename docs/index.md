# DIZZY

DIZZY generates event-sourced software from a single readable file — one `.feat.yaml` **feature-file** that is both the design and the source of the implementation.

**New to event sourcing? Start with [What DIZZY is (and why events)](explanation/what-is-dizzy.md)** — about ten minutes, nothing to install. Then **[Build a guestbook](tutorials/guestbook.md)**, which takes a feature from an empty directory to a running demo.

> ⚠️ **Research code.** DIZZY is a work in progress. The Python (`python-uv`) path is the most complete; the `rust-cargo` and `typescript-npm` runtimes are experimental.

## Documentation

This site is organized along the [Diátaxis](https://diataxis.fr/) framework:

- **[Tutorials](tutorials/index.md)** — learning-oriented lessons for getting started.
- **[How-to guides](how-to/index.md)** — task-oriented recipes for specific goals.
- **[Reference](reference/SPECIFICATION.md)** — the feature-file format spec and the
  generated [code API](reference/api/index.md) reference.
- **[Explanation](explanation/what-is-dizzy.md)** — background, design records, and
  the whitepaper.

> The CLI's own documentation ships with the tool: run `dizzy docs`, `dizzy docs authoring`,
> and `dizzy onboard`.
