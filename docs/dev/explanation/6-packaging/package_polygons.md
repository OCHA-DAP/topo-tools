---
title: "package-polygons"
sidebar:
  order: 9
  badge:
    text: Draft
    variant: caution
---

`package-polygons` replaces the standalone `dissolve` tool: instead of a
single caller-chosen `group_by`, it auto-detects every admin level present
and produces one dissolved output per level in a single call. Level
detection is structural by default (`core.schema_map`'s
cardinality/containment matcher, no naming convention assumed); passing an
explicit `name_field`/`code_field` pair uses that naming convention
instead. It reuses `core/dissolve/_02_dissolve.py`'s stage function
directly, the same primitive-reuse pattern `edge_match` uses on
`edge_extend`'s stages.

## Usage

```sh
topo-tools package-polygons admin3.geojson
```

```python
from topo_tools import package_polygons

package_polygons("admin3.parquet")
```

`OUTPUT_FILE` (positional, optional) MUST contain a literal `{n}`
placeholder if given; omitted, each level defaults to `INPUT_FILE` with an
`_admin{n}` suffix.

Run `topo-tools package-polygons --help` for the full, always-current
option list.

## Pipeline

1. **`_01_inputs`**: loads and reprojects the input via
   `core.io.read_and_reproject`, into `{name}_01`.
2. **`_02_dissolve`**: detects every level, either via an explicit schema's
   `detect_levels()` or, when `name_field`/`code_field` are omitted,
   structurally via `detect_level_columns()`
   (`core/schema_map/_level_columns.py`), then calls `core.dissolve`'s
   `_02_dissolve.main()` once per level coarser than the finest, grouping
   by that level's own code column. An explicit schema passes
   `target_schema` through so every finer-level column is dropped
   unconditionally rather than triggering the auto-drop warning; structural
   auto-detection instead excludes each finer level's own
   structurally-detected identity columns, verifying via
   `verify_functional_cluster()` that the group-by columns don't fragment
   the level's own code column's groups. The finest level is never
   dissolved: its own table is `{name}_01` directly, since grouping by its
   own finest code column would be a no-op.
3. **`_03_outputs`**: resolves every level's output/issues paths up front
   (`_plan_levels()`), raising `ValueError` early if `{n}` is missing from
   a caller-given template, then exports each non-skipped level through
   the same `finalize_geometry()` + gap-issues-table shape `dissolve`
   used, dropping intermediate tables afterward.

## Why the finest level's own path can equal the input path

A caller who wants the finest level's own file to simply be a copy of the
input (rather than a redundant no-op dissolve) gets that behavior for
free: `_plan_levels()` compares the finest level's computed path against
`input_path` and skips writing entirely when they match, since the file
already exists with exactly that content. When they differ (e.g. an
explicit `{n}`-template routes every level, including the finest, to a new
directory), the finest level is still written, as a plain export of the
already-loaded `_01` table.

## Portolan-scale profiling

The real target scale for this tool, `--debug` off, Apple Silicon/10 logical
cores, every level dissolved from the same finest-level `_01` table
independently (never chained from the previous coarser level's own output):

| Run                              | Groups in / out per level         | Wall time | RSS peak | Result                              |
| --------------------------------- | ---------------------------------- | --------- | -------- | ------------------------------------ |
| Global admin4 (`portolan/global/admin4/admin4.parquet`, 217,223 rows, 5 detected levels) | 217,223 / 111 (adm0), 1,889 (adm1), 19,842 (adm2), 58,438 (adm3) | 1,391s | 7.56 GB | All 5 levels written; each level logs 30 gaps above the noise floor, tolerated and reported in that level's own issues file |

Every coarser level's dissolve pass re-scans the full 217,223-row finest
table rather than the previous level's own smaller output, so cost scales
with input size times detected-level count, not with each level's own
shrinking output size: a caller with many detected levels on a large input
pays for a full-table dissolve pass per level.

**Real-data gotcha: `depth_column`/`--depth-column` collision with an
existing input column.** The portolan catalog's own global admin files
(e.g. `global/admin4/admin4.parquet`) already carry their own `adm_lvl`
column (the source's own real-depth stamp). `package-polygons` itself
never stamps a new depth column, so it's unaffected; `package-points` and
`package-lines` both do, and raise `ValueError` if their `depth_column`
(default `"adm_lvl"`) already exists on the input, rather than silently
producing a renamed duplicate column the way DuckDB's own `CREATE TABLE AS
SELECT` does for a repeated column name. Pass a non-colliding
`--depth-column` (e.g. `pkg_lvl`) when running either tool against a
portolan file that already has its own `adm_lvl`.
