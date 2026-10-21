# Contributing

Thanks for contributing! This guide covers how to get set up, how we work,
and what a finished pull request looks like.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/OCHA-DAP/topo-tools.git
cd topo-tools
uv sync
uv run pre-commit install
```

## Running the tool locally

```bash
uv run topo-tools edge-extend example.geojson
# equivalently: uv run python -m topo_tools edge-extend example.geojson
```

## Tests

```bash
uv run pytest
```

Tests use small synthetic geometry generated in-fixture (see
`tests/test_edge_extend.py`) rather than committed binary fixtures; prefer that
pattern for new tests unless a bug genuinely requires a real-world file to
reproduce.

## Code quality

```bash
uv run ruff format
uv run ruff check
```

`ruff` runs with `select = ["ALL"]` (see `pyproject.toml`); `pre-commit`
runs the same checks locally that CI runs, so a check should never pass
locally and fail in CI.

## Architecture

Read `CLAUDE.md` before making structural changes. It documents the
three-layer split (`core/` → `api/` → `cli/`) and the rule that `core/` and
`api/` must never import `click`. New tools should follow the same
`core/api/cli` layering as `edge-extend`.

## Workflow

Work on an issue starts on its own branch off `main`, before any
exploration or research, not just before the first commit. Open a PR
referencing the issue once the checklist below is met; `main` is
protected and only accepts merges via PR.

## What a finished PR looks like

- [ ] Tests added/updated for the change
- [ ] `uv run pytest` passes
- [ ] `uv run ruff format && uv run ruff check` clean
- [ ] Docs updated (README, `CLAUDE.md`, or `docs/*.md`) if user-facing
      behavior changed; a new public API/CLI option gets an example in
      its command's `Examples:` docstring block, then
      `uv run python .github/scripts/gen_reference.py` regenerates
      `docs/pages/{phase}/reference/{tool}.md`. Preview
      with `npm run dev` in a sibling `cod-site` checkout, which
      serves `docs/pages` at `/docs/`
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` for user-visible
      changes

## Reporting bugs / requesting features

Use the issue templates. The most useful bug report is a failing test:
add it to the matching `tests/test_<tool>.py` file (e.g. a dissolve bug
goes in `tests/test_dissolve.py`), following that file's synthetic-fixture
pattern, small in-test DuckDB fixtures, not committed binary files.

## Security issues

Do not open a public issue for a security vulnerability, see
[`SECURITY.md`](SECURITY.md).
