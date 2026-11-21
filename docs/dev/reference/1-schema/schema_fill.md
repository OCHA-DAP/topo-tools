---
title: "schema-fill"
sidebar:
  order: 6
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `schema-fill` MUST read the input and reproject it to EPSG:4326 the same
  way every other tool does, via `core.io.read_and_reproject()`.
- `schema-fill` MAY take the same `name_field`/`code_field` pair `schema-map`
  takes (each containing a `{n}` placeholder); both MUST be given together,
  or both omitted. When given, `schema-fill` MUST detect every admin level
  1..N present via `code_field`'s prefix, N being the deepest level column
  found, and MUST raise `ValueError` if any level in that 1..N range is
  missing its own code column, or if none is found at all; level 0 is
  additionally included whenever its own code column is present, without
  requiring it.
- When `name_field`/`code_field` are omitted, `schema-fill` MUST instead
  structurally auto-detect every admin level and its own code column
  (`core.schema_map`'s cardinality/containment matcher, no naming
  convention assumed), raising `ValueError` if any detected level lacks a
  code column.

## Filling

- `schema-fill` MUST append one new column, named by `depth_column`
  (`--depth-column` on the CLI, defaulting to `adm_lvl`), stamping each
  row with the deepest level whose *original* (pre-fill) code column was
  non-NULL. This is the only signal distinguishing a genuine leaf-level
  row from a coarser row whose deeper columns were only ever filled down.
- `schema-fill` MUST raise `ValueError` if `depth_column` already names an
  existing column on the input.
- For each admin-hierarchy column family sharing a level prefix and suffix
  (matched independently against `name_field`'s and `code_field`'s own
  prefixes when given, e.g. every `adm{n}_pcode`, every `adm{n}_name`; or,
  when auto-detecting, grouped by each level's shared naming anchor, digit
  or word, prefix or suffix position), `schema-fill` MUST pin every level
  past a row's own `depth_column` value
  to the family's own value at that row's real depth (or the nearest
  shallower level the family itself has a column for), leaving a value at
  or before a row's own real depth untouched, NULL included. This is a
  per-row pin: two rows in the same input file at different real depths are
  each filled relative to their own depth, never a single file-wide level.
- `schema-fill` MUST NOT touch geometry, and MUST NOT drop or rename any
  column other than adding `depth_column`.

## Outputs

- `schema-fill` performs no topology check at all; it only fills
  attribute columns and stamps a depth column, rounding geometry to the
  output grid and changing it in no other way.
- `schema-fill` MUST export the filled layer to a single output file.

## Configuration (`api.schema_fill.fill()` / CLI)

- `schema-fill` MUST process exactly one input file per call.
- The output path MUST default to the input path with a `_fill` stem
  suffix.
- `schema-fill` MUST raise `FileExistsError` if the output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `fill`, `outputs`; any other
  value MUST raise `ValueError`.
- `schema-fill` MAY accept `depth_column`/`--depth-column`, overriding the
  `adm_lvl` default name for the stamped depth column.

## Examples

### Example 1: basic run, structural auto-detection, output name chosen automatically

    topo-tools schema-fill admin4.parquet

### Example 2: custom target naming

    topo-tools schema-fill admin4.parquet --name-field state_name --code-field state_code

### Example 3: explicit output

    topo-tools schema-fill admin4.gpkg admin4_fill.gpkg --name-field state_name --code-field state_code

### Example 4: custom depth column name

    topo-tools schema-fill admin4.parquet admin4_fill.parquet --depth-column adm_depth
