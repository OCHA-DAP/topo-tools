---
title: "package-polygons"
sidebar:
  order: 4
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `package-polygons` MUST read the input and reproject it to EPSG:4326.
- `package-polygons` MUST detect every admin level present, either
  structurally (`core.schema_map`'s cardinality/containment matcher, no
  naming convention assumed, the default when `name_field`/`code_field`
  are omitted) or via an explicit `name_field`/`code_field` pair (the same
  shape `schema-map`/`schema-fill` take, each containing a `{n}`
  placeholder, given together or not at all), raising `ValueError` if no
  level is found.
- With auto-detection, a whole-table-constant column family sharing the
  detected levels' own naming style (e.g. `adm0_*` beside a detected
  `adm1_*`/`adm2_*`) MUST be treated as its own coarsest level, dissolved
  to a single row.

## Dissolving

- `package-polygons` MUST dissolve the input once per detected level
  coarser than the finest, grouping by that level's own code column.
  The finest level MUST NOT be dissolved; its output is the loaded input
  itself.
- Every column not at or above a given level's own detected depth MUST be
  dropped unconditionally (via the explicit `code_field`, or via each
  finer level's own structurally-detected identity columns when
  auto-detecting), never triggering the auto-drop warning `dissolve` would
  otherwise log for a genuinely finer-level column.

## Outputs

- `package-polygons` MUST produce one output file per detected level.
- Each level's output MUST pass the coverage check
  (no overlap or micro-polygon; a gap at or below `SNAP_TOLERANCE` blocks
  export, a wider one does not). Micro-polygons are merged on input, and their `micro-polygon` rows go in the
  finest level's issues report.
- `package-polygons` MUST also export an issues report per level, using the same columns as every other tool's issues report. A level's issues report MUST
  be produced only when it has at least one row.
- `package-polygons` MAY take `output_name_field`/`output_code_field`
  (each containing a `{n}` placeholder, either or both). When given, every
  written level MUST rename each column in the input
  `name_field`/`code_field` family (numbered siblings included) to the matching output template at the same
  level, leaving every other column unchanged. Either one given without
  `name_field`/`code_field` MUST raise `ValueError`, as MUST a renamed
  column colliding with any other output column.
- The finest level's own output MUST be skipped (no file written, no
  `check_overwrite` call) when its computed path resolves to the same file
  as the input; otherwise it MUST be written as a plain copy of the loaded
  input.

## Configuration (`api.package_polygons.package_polygons()` / CLI)

- `package-polygons` MUST process exactly one input file per call.
- `output_path`, if given, MUST contain a literal `{n}` placeholder,
  formatted per level; `package-polygons` MUST raise `ValueError` if it is
  given without one. If omitted, each level's output path MUST default to
  the input path with an `_admin{n}` suffix.
- `issues_path` follows the same `{n}`-template-or-omitted rule as
  `output_path`, defaulting per level to that level's own output path with
  an `_issues` suffix.
- `package-polygons` MUST raise `FileExistsError` for any level whose
  output or issues path already exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `dissolve`, `outputs`; any
  other value MUST raise `ValueError`.
- `name_field`/`code_field` MUST be given together, or both omitted; when
  both are omitted, `package-polygons` MUST fall back to full structural
  auto-detection of every level.
- `min_level`, if given, MUST leave out every level whose depth is below it,
  and MUST raise `ValueError` if no level is at or above it.
- `aggregations` (CLI: repeatable `--aggregation column=function`) MUST map
  a column name to one of `sum`, `min`, `max`, `avg`, `first`, overriding
  `dissolve`'s default of summing a numeric column that varies within a
  group (or dropping it, if non-numeric); any other function name MUST
  raise `ValueError`.

## Examples

### Example 1: default naming

Produces `adm3_admin1.geojson`, `adm3_admin2.geojson`, one file per
detected level, from a single admin3 input:

    topo-tools package-polygons adm3.geojson

### Example 2: explicit output template

`{n}` MUST appear in the output path if given, formatted per level:

    topo-tools package-polygons adm3.parquet "level_{n}.parquet"

### Example 3: finest level as a plain copy

Routing every level, including the finest, through the same template
still writes the finest level (as a copy of the input), unless that
level's own computed path happens to equal the input path itself:

    topo-tools package-polygons adm3.parquet "web/{n}.parquet"
