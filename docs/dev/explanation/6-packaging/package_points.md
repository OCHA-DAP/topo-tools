---
title: "package-points"
sidebar:
  order: 10
  badge:
    text: Draft
    variant: caution
---

`package-points` produces one label point per admin unit per detected
level, combined into a single output file, for web-map label placement.
It uses the pole of inaccessibility rather than the centroid, since a
centroid can fall outside a concave or multi-part polygon (e.g. a
crescent-shaped or archipelago admin unit), which would place a label off
its own territory.

## Usage

```sh
topo-tools package-points admin3.geojson
```

```python
from topo_tools import package_points

package_points("admin3.parquet")
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with a
`_points` suffix.

Run `topo-tools package-points --help` for the full, always-current option
list.

## Pipeline

1. **`_01_inputs`**: loads and reprojects the input, same as
   `package-polygons`.
2. **`_02_points`**: detects every level (explicit schema or structural
   auto-detection, same as `package-polygons`), then per level: dissolves
   via `core.dissolve`'s `_02_dissolve.main()` (grouped by that level's
   code column, every other level's own identity columns excluded
   outright so no numbered ancestor column ever survives), checks the
   dissolved row count against the input's own distinct code-column
   count, renames that level's own identity columns to one name shared
   across every level (`group_families_by_level()`'s naming-anchor
   stripping for auto-detection, or a fixed `code`/`name` for an explicit
   schema), excludes any remaining column that can't generalize to a
   coarser level (an attribute already dropped from a coarser level's own
   dissolve), computes each dissolved unit's
   `ST_MaximumInscribedCircle(geom).center` as its label point, stamps
   the depth column, and checks every point is `ST_Covers`-ed by its own
   source polygon. Every level's points table is combined via
   `UNION ALL BY NAME`, not sequential inserts into a fixed-schema table:
   a column excluded at a given level is simply absent from that level's
   own table rather than unioned in as an always-`NULL` column.
3. **`_03_outputs`**: exports the combined table directly; no topology
   gate applies, since this is a derived cartographic layer, not a
   coverage layer.

## Detecting a constant root level (e.g. a single-country file's `adm0_*`)

With auto-detection, `detect_root_level()` (`core.schema_map`) looks for an
unassigned column family sharing the detected levels' own naming style
(the same `(prefix, anchor, suffix)` split `_level_anchors()` derives for
a real level, so it covers a digit anchor like `adm0_*` beside `adm1_*`/
`adm2_*` just as well as a word anchor like `country_*` beside `state_*`/
`county_*`), constant across the whole table; it's `None` if no such
unassigned family exists or more than one does, rather than guess. When
found, it's treated as its own level, dissolved with an empty `group_by`
(the whole table collapses to one row), excluded from every other level's
own dissolve like any other level, and renamed via
`group_families_by_level()`'s naming-anchor injection into the same
`pcode`/`name` columns every other level's own row uses, rather than
surviving under a separate `adm0_pcode`/`adm0_name` (or `country_code`/
`country_name`) name. The root's own iteration is also excluded from the
coarsest-level `generalizable` set used to decide what a finer level may
keep, since it has nothing coarser to compare against and would otherwise
wrongly veto attributes real
levels can keep.

## Why `ST_MaximumInscribedCircle`, not the centroid

`ST_Centroid` is a purely geometric average of a polygon's area and can
land outside the polygon itself for a concave shape, or on a body of water
between two islands for a multi-part one. `ST_MaximumInscribedCircle`'s
`center` is always interior to the polygon (it's the center of the largest
circle that fits inside it), the standard technique web-map label
placement uses to keep a label anchored on its own territory.

## Portolan-scale profiling

The real target scale for this tool, `--debug` off, Apple Silicon/10 logical
cores:

| Run                              | Points out (all levels combined)   | Wall time | RSS peak | Result                              |
| --------------------------------- | ---------------------------------- | --------- | -------- | ------------------------------------ |
| Global admin4 (`portolan/global/admin4/admin4.parquet`, 217,223 rows, 5 detected levels, `--depth-column pkg_lvl`) | 297,503 (111 + 1,889 + 19,842 + 58,438 + 217,223) | 483s | 8.01 GB | Every point `ST_Covers`-ed by its own source polygon |

**`depth_column` on the input.** Files run through `schema-fill`, and the
portolan catalog's global admin files, already carry an `adm_lvl` column
holding each row's real depth. `_02_points.py` drops it before dissolving
and stamps each point with the level it labels, the same way
`package-lines` stamps each segment's coarsest level, logging the
replacement.
