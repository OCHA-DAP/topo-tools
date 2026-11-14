---
title: "package-points"
sidebar:
  order: 5
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `package-points` MUST read the input, reproject it to EPSG:4326 and
  merge or drop its micro-polygons.
- `package-points` MUST detect every admin level present, either
  structurally (`core.schema_map`'s cardinality/containment matcher, no
  naming convention assumed, the default when `name_field`/`code_field`
  are omitted) or via an explicit `name_field`/`code_field` pair, raising
  `ValueError` if no level is found.
- With auto-detection, a whole-table-constant column family sharing the
  detected levels' own naming style (e.g. `adm0_*` beside a detected
  `adm1_*`/`adm2_*`, or a word-anchored `country_*` beside `state_*`/
  `county_*`) MUST be treated as its own coarsest level, dissolved to a
  single row.

## Point extraction

- `package-points` MUST dissolve the input once per detected level,
  grouping by that level's own code column, then reduce each dissolved
  unit to a single point via `ST_MaximumInscribedCircle(geom).center` (the
  pole of inaccessibility), never the centroid.
- `package-points` MUST raise `ValueError` if a level's dissolved row count
  does not equal that level's distinct code-column count in the input, or
  if any output point is not covered by its own source polygon
  (`ST_Covers`).
- Every level's points MUST be tagged with a depth column (`adm_lvl` by
  default, overridable via `depth_column`), holding that level's own
  numeric depth. An input column of the same name (e.g. `schema-fill`'s
  real-depth stamp) MUST be dropped and replaced, logged, matching
  `package-lines`.
- Every level's own identity columns, including a whole-table-constant
  root's, MUST land under one name shared across every level: the source
  file's own naming convention (e.g. `pcode`, `name`, derived by stripping
  each level's own naming anchor), or, for an explicit schema, each
  family's suffix after the level number (`pcode`, `name`, `name1`),
  falling back to `code`/`name` for a field with none (`GID_{n}`). No level-numbered column (e.g. `adm1_pcode`) MUST ever
  appear in the output: an ancestor level's identity columns are excluded
  from a finer level's own dissolve, the same as any other level's, rather
  than kept as a repeated, numbered ancestor value.

## Outputs

- `package-points` performs no topology hard gate; it is a derived
  cartographic layer, not a coverage layer.
- `package-points` MUST combine every level's points into one output file.
  Every output row MUST carry only geometry, its own level's generic
  identity columns and the depth column. Any other input column MUST NOT
  appear in the output: metadata stays on the polygons, joined back by the
  identity columns and the depth column.

## Configuration (`api.package_points.package_points()` / CLI)

- `package-points` MUST process exactly one input file per call.
- The output path MUST default to the input path with a `_points` suffix.
- `package-points` MUST raise `FileExistsError` if the output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `points`, `outputs`; any
  other value MUST raise `ValueError`.
- `name_field`/`code_field` MUST be given together, or both omitted; when
  both are omitted, `package-points` MUST fall back to full structural
  auto-detection of every level.
- `min_level`, if given, MUST leave out every level whose depth is below it,
  and MUST raise `ValueError` if no level is at or above it.

## Examples

### Example 1: default naming

Produces `adm3_points.geojson`, one combined file covering every detected
level:

    topo-tools package-points adm3.geojson

### Example 2: custom depth column

Rename the stamped level column from `adm_lvl` to something else:

    topo-tools package-points adm3.geojson --depth-column level
