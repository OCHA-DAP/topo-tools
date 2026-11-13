---
title: "package"
sidebar:
  order: 3
  badge:
    text: Draft
    variant: caution
---

## Behavior

- `package` MUST call `package-polygons`, `package-points`, and
  `package-lines` against the same input path and the same `name_field`/
  `code_field` (or the same auto-detection, when both are omitted), each
  reading and reprojecting the input independently (no shared connection
  or table across the three).
- `package` MUST NOT expose `--step` or a separate issues-path option; each
  sub-tool manages its own connection and derives its own issues path from
  its own output path.

## Output path templating

- `output_path`, if given, MUST contain a literal `{x}` placeholder;
  `package` MUST raise `ValueError` if it is given without one.
- `{x}` MUST be substituted per sub-call with a fixed word: `admin{n}` for
  `package-polygons` (itself still `{n}`-templated per level), `points`
  for `package-points`, `lines` for `package-lines`. No other placeholder
  is exposed.
- If `output_path` is omitted, every sub-tool MUST receive `output_path=None`
  and fall back to its own default naming.

## Configuration (`api.package.package()` / CLI)

- `package` MUST process exactly one input file per call.
- `overwrite`, `threads`, `tmp_dir`, `debug`, `name_field`, and `code_field`
  MUST be passed through unchanged to all three sub-calls.
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
