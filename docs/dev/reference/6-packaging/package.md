---
title: "package"
sidebar:
  order: 3
  badge:
    text: Draft
    variant: caution
---

## Behavior

- `package` MUST read the input once, reproject it to EPSG:4326 and, unless
  `clean` is off, coverage-clean it when it has invalid edges or gaps,
  writing the result as GeoParquet in `tmp_dir`. Every later stage reads
  that file (or the input itself, with `clean` off).
- Unless `validate` is off, `package` MUST then call `schema-detect`,
  `topo-detect`, `code-detect` and `name-detect`, in that order, through
  their own `api.*.detect()` functions, each writing its own report, and
  MUST raise `ValueError` naming the summary, before writing any package
  output, if any summary row has severity `error`. The CLI MUST exit with
  status 1 on it.
- If `schema-detect` reports `levels-undetected`, `package` MUST skip
  `code-detect` and `name-detect`, recording a `skipped` row (severity
  `warn`) for each. With `clean` off, `package` MUST skip `topo-detect`,
  recording a `skipped` row (severity `warn`).
- If a stage raises, `package` MUST record a `failed` row (severity
  `error`, the exception message as `reason`) and still run the remaining
  stages. A single check failing inside a stage MUST show as that stage's
  `check-failed` error row.
- `package` MUST then call `package-polygons`, `package-points`, and
  `package-lines` against that file and the same `name_field`/`code_field`
  (or the same auto-detection, when both are omitted), each reading it
  independently (no shared connection or table across the three).
- With `clean` off, the three sub-tools MUST NOT raise on overlaps or
  invalid edges: `package-polygons` MUST write one `invalid-edge` row per
  row pair with unmatched edges to each level's issues file, and no `gap`
  rows.
- `package` MUST NOT expose `--step` or a separate issues-path option; each
  sub-tool manages its own connection and derives its own issues path from
  its own output path.

## Reports

- Reports MUST be Parquet, named after the input's stem:
  `{stem}_schema_issues.parquet`, `{stem}_topo_issues.parquet`,
  `{stem}_code_issues.parquet`, `{stem}_name_issues.parquet`, in the
  `package-polygons` output folder.
- The summary MUST be written to `{stem}_validate_summary.csv` (UTF-8 with
  a BOM) in the same folder, with columns `stage`, `kind`, `severity`,
  `count`, `report`, `reason`: one row per stage, kind and severity found,
  or one `count = 0` row with reason `no issues` for a stage whose report
  is empty or not written. Rows MUST be ordered by stage, then severity,
  then kind.
- `topo-detect` rows MUST take their severity from the kind: `gap` is
  `warn`, `overlap` and `micro-polygon` are `error`.
- `package` MUST log the summary table.

## Output path templating

- `output_path`, if given, MUST contain a literal `{x}` placeholder;
  `package` MUST raise `ValueError` if it is given without one.
- `{x}` MUST be substituted per sub-call with a fixed word: `admin{n}` for
  `package-polygons` (itself still `{n}`-templated per level), `points`
  for `package-points`, `lines` for `package-lines`. No other placeholder
  is exposed.
- If `output_path` is omitted, every output MUST take its sub-tool's own
  default name (`_admin{n}`, `_points`, `_lines`) beside the input, never
  beside the cleaned copy in `tmp_dir`.

## Configuration (`api.package.package()` / CLI)

- `package` MUST process exactly one input file per call.
- `clean` (CLI: `--no-clean`) and `block` (CLI: `--no-block`) MUST
  default to on. Validation MUST always run and write its reports; with
  `block` off, `package` MUST log a warning on an error row and package
  anyway.
- `package` MUST raise `FileExistsError` before reading the input if the
  points or lines output, any report or the summary already exists and
  overwriting wasn't requested.
- `overwrite`, `threads`, `debug`, `name_field`, and `code_field` MUST be
  passed through unchanged to all three sub-calls. `tmp_dir` (a fresh
  temporary folder when omitted) MUST be shared by the clean, every detect
  and all three sub-calls.
- `aggregations` (CLI: repeatable `--aggregation column=function`) MUST be
  passed through to `package-polygons` only; `package-points` and
  `package-lines` have no equivalent override.
- `output_name_field`/`output_code_field` (CLI:
  `--output-name-field`/`--output-code-field`) MUST be passed through to
  `package-polygons` only; `package-points` and `package-lines` write
  generic per-role columns, not template-named ones.
- `package` MUST NOT expose `depth_column`: `package-points` and
  `package-lines` each run with their own default (`adm_lvl`), replacing an
  input column of the same name.

## Examples

### Example 1: defaults for all three outputs

Each sub-tool falls back to its own default naming
(`_admin{n}`/`_points`/`_lines`):

    topo-tools package adm3.geojson

### Example 2: explicit output template

`{x}` MUST appear in `--output` if given; it's substituted per sub-tool
(`admin{n}`, `points`, `lines`):

    topo-tools package adm3.geojson --output "web/{x}.geojson"

### Example 3: a global layer too large to clean

The clean and the topology check are skipped; invalid edges go to each
level's issues file:

    topo-tools package global_admin2.parquet --no-clean
