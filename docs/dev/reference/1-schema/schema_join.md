---
title: "schema-join"
sidebar:
  order: 5
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `schema-join` MUST read one input file and one join file, reprojecting
  both to EPSG:4326 the same way every other tool does, via
  `core.assign.load_input()`/`load_overlay()`.
- `schema-join` MAY take the same `name_field`/`code_field` pair
  `schema-map` takes (each containing a `{n}` placeholder); both MUST be
  given together, or both omitted. When given, the join layer's hierarchy
  columns MUST be every column in a `{n}`-numbered family under
  `name_field`'s or `code_field`'s prefix, for every level `detect_levels()` finds on the
  join layer, raising `ValueError` under the same missing-level rules as
  `schema-fill`.
- When `name_field`/`code_field` are omitted, `schema-join` MUST instead
  structurally auto-detect the join layer's hierarchy columns (every level's
  identity columns from `core.schema_map`'s cardinality/containment
  matcher, no naming convention assumed), raising `ValueError` if no level
  is detected.
- Only the join layer's hierarchy columns are ever copied; any other join layer
  column MUST be ignored.

## Assignment

- `schema-join` MUST assign each input polygon to the single join polygon it shares the
  most area with (`core.assign.assign_many()`, per-polygon plurality, ties
  broken by lowest join fid), measured in `EQUAL_AREA_CRS`.
- An input polygon overlapping no join polygon MUST stay in the output, with every copied
  join column NULL.

## Joining

For each join layer hierarchy column:

- absent from the input layer: `schema-join` MUST add it, filled from the
  input polygon's assigned join polygon;
- present on the input layer and equal (`IS NOT DISTINCT FROM`) on every
  assigned input polygon: `schema-join` MUST skip it, leaving the input layer's column
  as-is;
- present on the input layer and different on any assigned input polygon:
  `schema-join` MUST leave the input layer's column untouched and add the
  join polygon's values under the next free numbered sibling name (`adm2_name1`,
  then `adm2_name2` if `adm2_name1` is taken on either layer), logging a
  warning with the differing row count.

`schema-join` MUST NOT raise on a conflicting value, and MUST NOT
overwrite any input value (see `docs/adr/0109`).

## Outputs

- `schema-join` MUST change geometry only by rounding it to the output
  grid, and performs no topology check.
- The output MUST keep every input row, using `name_field`/`code_field`, or
  `adm{n}_name`/`adm{n}_code` when omitted. Columns MUST keep input order,
  each added numbered sibling right after the last existing column of its
  family, and every other column absent from the input layer within its
  own level: a name after the level's last name column or else before its
  first code column, any other column after the level's last column of its
  own family or else its last column. A level absent from the input layer
  goes before the first coarser input level, or after all input columns
  when there is none, in template order (see `docs/adr/0138`). Rows MUST
  be sorted by the deepest level's own code column, as in `schema-map`.
- `schema-join` MUST write an issues file in the shared issues-table
  column schema, with one row per:
  - `no-overlap`: an input polygon overlapping no join polygon;
  - `low-overlap`: an input polygon whose assigned join polygon covers less than
    `min_overlap` of its own area, with `area_m2` set to the input polygon's area
    outside that join polygon and `reason` stating the covered share;
  - `value-mismatch`: an input polygon and column where the input polygon's value and its
    join polygon's value are both non-NULL and differ, with `reason` naming the
    column and both values.
- `unit_a` MUST hold the input polygon's 1-based row number in the output file, not
  its input fid, since rows are re-sorted by code.
- `schema-join` MUST NOT write an empty issues file, and MUST remove a
  stale one at the issues path instead.

## Configuration (`api.schema_join.join()` / CLI)

- `schema-join` MUST process exactly one input file and one join file per
  call; either MAY be an `http://`/`https://` URL to a `.parquet` file.
- The output path MUST default to the input path with a `_join` stem
  suffix, and the issues path to the output path with an `_issues` stem
  suffix (`issues_path`/`--issues-output` to override).
- `schema-join` MUST raise `FileExistsError` if either the output or the
  issues path already exists and overwriting wasn't requested.
- `min_overlap`/`--min-overlap` MUST default to `0.5` and MUST raise
  `ValueError` outside `(0, 1]`.
- `step`, if given, MUST be one of `inputs`, `assign`, `join`, `outputs`;
  any other value MUST raise `ValueError`.

## Examples

### Example 1: basic run, structural auto-detection, output name chosen automatically

    topo-tools schema-join admin3.parquet admin2.parquet

### Example 2: chain levels coarsest-first

    topo-tools schema-join admin2.parquet admin1.parquet admin2_join.parquet
    topo-tools schema-join admin3.parquet admin2_join.parquet admin3_join.parquet

### Example 3: custom target naming

    topo-tools schema-join admin3.parquet admin2.parquet --name-field adm{n}_name --code-field adm{n}_pcode

### Example 4: explicit issues path and a stricter overlap threshold

    topo-tools schema-join admin3.gpkg admin2.gpkg admin3_join.gpkg --issues-output review.gpkg --min-overlap 0.9
