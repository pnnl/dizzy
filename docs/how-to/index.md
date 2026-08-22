# How-to guides

Task-oriented recipes for getting specific things done with DIZZY.

- **[Add a validated tutorial](add-a-validated-tutorial.md)** — author a new
  `docs/tutorials/` page that is executed and checked by `just tutorials-check`.

!!! note "Work in progress"
    More how-to guides are being written. The authoring guide — how to write a
    `.feat.yaml` and what to fill in after each generation stage — currently ships with
    the tool: run `dizzy docs authoring`.

Common tasks today:

- **Author a feature** → `dizzy docs authoring`
- **Generate from a feature-file** → `dizzy generate definitions|static|libraries|wiring <feat> <out>`
- **See the full command reference** → `dizzy docs` (CLI manpage & roadmap)
