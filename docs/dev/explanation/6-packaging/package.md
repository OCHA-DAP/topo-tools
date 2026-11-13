---
title: "package"
sidebar:
  order: 8
  badge:
    text: Draft
    variant: caution
---

`package` cleans and checks one input, then runs `package-polygons`,
`package-points`, and `package-lines` against it, for the common case of
wanting the full cartographic bundle at once. It calls each tool's own
`api.*()` function directly, composing at the `api.*()` layer, not the
stage layer, so each detect and each sub-tool reads the cleaned file on its
own connection and a stage that raises leaves the others unaffected.

## Clean

`core.io.read_reproject_and_clean()`, the same call `edge-extend` and
`edge-match` make on their inputs, checks the layer with
`has_valid_topology(gap_maximum_width=0)` and runs `coverage_clean()` only
when it finds a defect. The result goes to a GeoParquet in `tmp_dir`, which
every later stage reads; output names still follow the original input, via
`package-polygons`' API-only `original_path`.

`--no-clean` exists for a layer too large to clean, such as a global
`edge-mosaic --no-clean` output. The sub-tools then skip their overlap and
topology checks (`check_topology=False`), and `package-polygons` writes
`invalid-edge` rows from `invalid_edge_issues()`, the same rows
`edge-mosaic --no-clean` writes. It writes no `gap` rows: the gap scan
unions the layer, and `ST_CoverageUnion` fails on unmatched edges while
`ST_Union_Agg` over a global layer is an unmeasured whole-world union.

## Validation

| Stage | Tool | Report |
| --- | --- | --- |
| schema | `schema-detect` | `{stem}_schema_issues.parquet` |
| topo | `topo-detect` | `{stem}_topo_issues.parquet` |
| code | `code-detect` | `{stem}_code_issues.parquet` |
| name | `name-detect` | `{stem}_name_issues.parquet` |

Validation runs on the cleaned file, so a defect the clean fixes never
blocks packaging. Edge checks need an overlay layer and have no place in a
single-layer check. `code-detect` and `name-detect` need the admin levels
resolved, so they are skipped when `schema-detect` reports
`levels-undetected`. `schema-detect` runs the same level resolution they
do, so a layer they would refuse is reported there once, with the reason.
Under `--no-clean`, `topo-detect` is skipped: a seamed layer would fail it
by design, and over a global layer its overlap scan runs out of memory. A
`failed` row means a whole stage broke; a `check-failed` row means one
check inside a stage broke and the stage's other checks still ran.

The summary is built in an in-memory DuckDB table from the written
reports, one `GROUP BY` per report. `topo-detect`'s report has no severity
column, so each kind gets one: an overlap or a micro-polygon is an error,
but a gap is a warning, since a gap may be a real hole in the layer (a
lake, an enclave). Any error row stops `package` before it writes an
output, unless `--no-block` is given; the reports are written either way.

## Usage

```sh
topo-tools package admin3.geojson
```

```python
from topo_tools import package

package("admin3.parquet")
```

Run `topo-tools package --help` for the full, always-current option list.

## Output path templating

`--output`/`output_path`, if given, MUST contain a literal `{x}`
placeholder. `{x}` is substituted per sub-call with a fixed, hardcoded
word: `admin{n}` for `package-polygons` (itself still `{n}`-templated per
level, unaffected by `package`'s own substitution), `points` for
`package-points`, `lines` for `package-lines`. `{n}` is never exposed to
`package`'s own caller directly; it only ever exists embedded inside the
`admin{n}` substitution, so it can't leak into the points/lines paths. A
caller who wants a different word for one output calls that tool directly
instead, e.g. `package-polygons` accepts any custom `{n}`-containing
template.
