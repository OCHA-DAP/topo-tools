---
title: "change"
sidebar:
  order: 6
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `change` MUST accept exactly one old-version file and one new-version
  file per call.
- `change` MUST load and coverage-clean both layers via the shared
  `core.io.read_reproject_and_clean()` helper before comparison.

## Overlap computation

- `change` MUST compute `shared_area`, `coverage_a`, `coverage_b`, and
  `iou` for every old/new fid pair whose parts touch, using exact
  `ST_Intersection`; it MUST NOT fall back to point-sampling on failure
  (see `docs/dev/explanation/4-codes/change.md`).
- Both layers MUST be exploded into parts before the join, so a
  multi-part fid does not collapse to one bbox spanning all its parts.
- An intersection crumb below `INTERSECTION_SLIVER_DEG2` (raw, untransformed
  degree² area) MUST be dropped before area/ratio computation.
- Area and ratio computation (`shared_area`, `coverage_a`, `coverage_b`,
  `iou`) MUST use an equal-area projection (`EQUAL_AREA_CRS`), not raw
  EPSG:4326 degrees.

## Classification

- `change` MUST classify every unit into exactly one of: `unchanged`,
  `renamed`, `modified`, `relocated`, `split`, `merge`, `complex`,
  `created`, `removed`.
- Classification MUST be driven by connected-component cardinality
  (`na`/`nb` = old/new member count of a unioned cluster): `1:0` is
  `removed`, `0:1` is `created`, `1:many` is `split`, `many:1` is
  `merge`, `many:many` is `complex`.
- A `1:1` cluster reached only through identity linking (no spatial
  `tau_match` pass) MUST classify as `relocated`.
- A `1:1` cluster that passed spatial `tau_match` MUST classify as
  `unchanged` when its IoU is at or above `tau_same` and no linked
  code/name differs, `renamed` when its IoU is at or above `tau_same`
  and a linked code/name differs, and `modified` when its IoU is below
  `tau_same`.
- `renamed` MUST NOT fire when `link_by_code`/`link_by_name` are both
  unset: pure geometry mode MUST NOT consult code/name for
  classification, only for display.
- A pair MUST be linked by code/name only when `--link-by-code`/
  `--link-by-name` is set, the value is non-null on both sides, and the
  value is unique on that side (a value repeated within one side MUST
  NOT be used as an identity match).
- An identity-matched pair MUST NOT be unioned ahead of spatial matching
  unless every other `tau_match`-passing spatial neighbor of both its
  fids is also identity-covered; a pair failing this guard MUST fall
  through to spatial-only classification instead (see
  `docs/dev/explanation/4-codes/change.md`).
- `link_mode` MUST be `either` (code OR name match links, default) or
  `both` (code AND name must both match); any other value MUST raise
  `ValueError`. `link_mode` only has an effect when both
  `link_by_code` and `link_by_name` are set.

## Column auto-detection

- Each side's code/name column MUST default to the finest detected admin
  level's code column and its primary name column (`core.schema_map`'s
  structural level detection, or the side's `--name-field-*`/
  `--code-field-*` pair, given together or not at all); an explicit
  `--code-column-*`/`--name-column-*` MUST always override it.
- If no column was given or detected on a side, `change` MUST log a
  warning, or raise `ValueError` when the matching `--link-by-code`/
  `--link-by-name` is set.

## Outputs

- `change` performs no topology hard gate at all; it is a read-only
  comparison between two inputs, not a fix.
- `change` MUST always write two artifacts: a tabular changelog (no
  geometry column) and a spatial overlay layer, even when
  `--link-by-code`/`--link-by-name` are unset.
- The tabular changelog MUST contain one row per matched pair plus one
  row per unmatched singleton (a pure `created`/`removed` unit, or a
  `split`/`merge` remnant with nothing on the other side).
- Every changelog row MUST echo the run's own `tau_match`, `tau_same`,
  `link_by_code`, `link_by_name`, and `link_mode` values, regardless of
  that row's own classification.
- `code_a`/`code_b`/`name_a`/`name_b` MUST be null on every row unless
  the corresponding column was given or detected for that side.
- The spatial overlay layer MUST contain every new-version unit tagged
  with its `relationship_class`, plus every old-version unit classed
  `removed`; together these MUST tile the comparison area exactly once.

## Configuration (`api.change.change()` / CLI)

- `change` MUST process exactly one old file + one new file per call.
- `output_path` MUST default to `{old_stem}_{new_stem}_changelog.csv`
  next to the old file; its suffix MUST be a tabular format (`.csv` or
  `.parquet`), any other suffix MUST raise `ValueError`.
- `overlay_path` MUST default to `output_path`'s stem with an
  `_overlay` suffix, in the old file's own format; its suffix MUST be a
  GDAL-vector-supported format, any other suffix MUST raise `ValueError`.
- `change` MUST raise `FileExistsError` if either output path already
  exists and overwriting wasn't requested.
- `tau_match` and `tau_same` MUST be floats in `[0, 1]`; there is no
  `auto`/`all` string mode.
- `step`, if given, MUST be one of `inputs`, `overlap`, `classify`,
  `outputs`; any other value MUST raise `ValueError`.

## Examples

### Example 1: compare two versions by spatial overlap alone, output name chosen automatically

    topo-tools change admin2_2020.geojson admin2_2024.geojson

### Example 2: explicit output and overlay file paths

    topo-tools change old.gpkg new.gpkg changelog.csv --overlay-file overlay.gpkg

### Example 3: also link units sharing a unique code across versions

    topo-tools change old.gpkg new.gpkg --link-by-code \
      --code-column-a adm2_pcode --code-column-b adm2_pcode

### Example 4: loosen the "related" threshold for heavily redrawn boundaries

    topo-tools change old.parquet new.parquet --tau-match 0.6
