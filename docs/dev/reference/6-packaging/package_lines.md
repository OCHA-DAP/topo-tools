---
title: "package-lines"
sidebar:
  order: 6
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `package-lines` MUST read the input, reproject it to EPSG:4326 and
  merge or drop its micro-polygons.
- `package-lines` MUST detect every admin level present, either
  structurally (`core.schema_map`'s cardinality/containment matcher, no
  naming convention assumed, the default when `name_field`/`code_field`
  are omitted) or via an explicit `name_field`/`code_field` pair, raising
  `ValueError` if no level is found.

## Boundary extraction

- `package-lines` MUST dissolve the input once, at the finest detected
  level only; every coarser boundary is already contained in that level's
  own adjacency, so no per-level repeat dissolve is needed.
- `package-lines` MUST derive each pair of touching units' shared boundary
  from `ST_Boundary` and `ST_Intersection`, never a PostGIS-style
  shared-paths function (not available in this DuckDB spatial build), and
  MUST merge the result with `ST_LineMerge` before dumping to atomic rows:
  an unmerged intersection returns one fragment per matching edge segment,
  not one line, even where both sides' vertices exactly coincide.
- A shared-boundary row MUST be produced exactly once per touching pair
  (`left_fid < right_fid`), never twice. A pair whose polygons only touch
  at a point (corner touch) MUST produce zero shared rows.
- `package-lines` MUST dump every multi-part shared or exterior geometry
  into atomic `LineString` rows; a unit with multiple disjoint exterior
  segments MUST produce one row per segment, never a single
  `MultiLineString`.
- Every output row MUST carry each side's identity at the row's own depth
  (the coarsest detected level for an outer edge) under
  single-letter-prefixed generic columns, `a_*` for one side and `b_*` for
  the other (e.g. `a_pcode`/`b_pcode`, `a_name`/`b_name`), one pair of
  columns per identity kind the detected levels' naming families
  detect (`group_families_by_level()`/`level_family_names()`, or, for an
  explicit schema, `generic_families()`: `code`, `name` and each sibling's
  suffix, e.g. `name1`, `pcode`), never a raw `fid`. Single-letter
  prefixes keep every generated field name within a Shapefile DBF field's
  10-character limit. There is no `boundary_type` column: a row is
  exterior exactly when every `b_*` column is `NULL`, never shared.
- `package-lines` MUST classify every shared row by the coarsest detected
  level at which its two sides' code columns first differ, and every
  exterior row one level coarser than the coarsest detected level
  (`min(levels) - 1`), into a depth column (`adm_lvl` by default,
  overridable via `depth_column`). An outer edge (`min(levels) - 1`, e.g.
  -1) is unclassified: it MAY be a coastline or a land border with an area
  outside the input. When adm0 is a detected level, depth 0 is a border
  between two adm0 units.
- `package-lines` MUST raise `ValueError` if any finest-level unit is
  absent from every output row (matched internally by `fid`, dropped from
  the output once the `a_*`/`b_*` columns resolve each side's identity).
- `package-lines` MUST raise `ValueError` if `depth_column` collides with
  one of its own fixed output column names (`left_fid`, `right_fid`,
  `geom`).

## Outputs

- `package-lines` performs no topology hard gate; it is a derived
  cartographic layer, not a coverage layer.
- `package-lines` MUST combine shared and exterior rows from every level
  into one output file, deduplicated so no boundary segment repeats across
  levels.

## Configuration (`api.package_lines.package_lines()` / CLI)

- `package-lines` MUST process exactly one input file per call.
- The output path MUST default to the input path with a `_lines` suffix.
- `package-lines` MUST raise `FileExistsError` if the output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `boundaries`, `outputs`; any
  other value MUST raise `ValueError`.
- `name_field`/`code_field` MUST be given together, or both omitted; when
  both are omitted, `package-lines` MUST fall back to full structural
  auto-detection of every level.
- `min_level`, if given, MUST leave out every row whose depth is below it,
  and MUST raise `ValueError` if no level is at or above it.

## Examples

### Example 1: default naming

Produces `adm3_lines.geojson`, combining shared and exterior boundaries
from every detected level, deduplicated so no segment repeats:

    topo-tools package-lines adm3.geojson

### Example 2: style by boundary depth

`adm_lvl` (or a custom `--depth-column`) lets a web map style an
international boundary (level 0) differently from a sub-national one:

    topo-tools package-lines adm3.geojson --depth-column boundary_level
