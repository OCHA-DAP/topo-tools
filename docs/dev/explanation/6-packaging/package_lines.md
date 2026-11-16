---
title: "package-lines"
sidebar:
  order: 11
  badge:
    text: Draft
    variant: caution
---

`package-lines` produces one deduplicated line network of admin
boundaries, tagged by adjacency and the coarsest admin level each segment
belongs to. A row is shared between two units when its `b_*` columns are
populated, and exterior to all of them when they're `NULL`; there is no
separate `boundary_type` column. A web map can style or filter
international vs. sub-national boundaries from this one layer, instead of
drawing every level's own polygon outline on top of each other.

## Usage

```sh
topo-tools package-lines admin3.geojson
```

```python
from topo_tools import package_lines

package_lines("admin3.parquet")
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with a
`_lines` suffix.

Run `topo-tools package-lines --help` for the full, always-current option
list.

## Why a noded network

Two units can share a border without `ST_Touches` holding: clip seams
leave sliver overlaps and T-junction vertices a rounding error off the
neighbour's segment, so per-pair `ST_Intersection` misses them. Noding
every boundary together gives each stretch of line once, whatever the
polygons' own topology, and labelling by distance finds the units on each
side (see `docs/adr/0153`). Where two units overlap each other along a
third unit's border, each copy keeps its own label, so the shared line can
appear twice with different unit pairs (2.8 km of 14,397 km in the
fieldmaps Xinjiang layer). Cleaning the input first resolves the overlap.

## Pipeline (`_02_boundaries`)

1. Dissolve the input once, at the finest detected level only (via
   `core.dissolve`'s `_02_dissolve.main()`); every coarser boundary is
   already contained in the finest level's own adjacency, so no per-level
   repeat is needed. Every ancestor code column survives as a
   constant-per-group attribute, used later for classification.
2. Take each unit's `ST_Boundary` and clip it to every 1° tile
   (`TILE_DEG`) its bbox covers, widened by `TILE_MARGIN_DEG`, skipping
   the clip for a boundary already inside its tile.
3. Node each tile with `ST_LineMerge(ST_Union_Agg(geom))`, dumped to
   edges and normalised, with endpoints within `10 * SNAP_TOLERANCE` of
   another moved onto the least one, then add each edge's midpoint and bbox in a
   separate pass (computing them in the noding query makes it about three
   times slower).
4. Label each edge: pair edges with boundaries on bbox columns only, then
   keep the units within `10 * SNAP_TOLERANCE` of the midpoint, nearest
   two first. One unit makes the edge exterior, two make it shared
   (`left_fid < right_fid`).
5. Snap same-label shared edges in one tile onto each other, clip edges
   back to their exact tile, move endpoints within tolerance onto the
   least one again (tile cuts differ by a rounding error), and line-merge
   each label pair into atomic `LineString` rows.
6. Classify every row: a shared row's depth is the coarsest detected level at which
   its two sides' own code columns first differ; an exterior row's depth
   is the coarsest detected level when that level has a single unit,
   since every outer edge is then that unit's outline, and one level
   coarser (`min(levels) - 1`) otherwise, so it never collides with a
   shared border between two units of the coarsest level (e.g. two adm0
   units in a multi-country file).
7. Raise `ValueError` if any finest-level fid is absent from every output
   row (as `left_fid` or `right_fid`), catching a fid silently dropped
   somewhere in the pipeline. A fid fully enclosed by neighbors (zero
   exterior boundary) appears only in shared rows, as `left_fid` or
   `right_fid`; the check accepts either column, not `left_fid` alone.
8. Resolve each side's `fid` to the detected identity families at the
   row's own depth, the coarsest level for an outer edge
   (`group_families_by_level()`/`level_family_names()`, the same
   naming-anchor mechanism `package-points` uses, or, for an explicit
   schema, `generic_families()`: each family's suffix after the level
   number, e.g. `pcode`, `name`, `name1`, falling back to `code`/`name` for
   a field with none, e.g. `GID_{n}`), one column pair per family under `a_*`/`b_*`
   (e.g. `a_pcode`/`b_pcode`, `a_name`/`b_name`), then drop
   `left_fid`/`right_fid` from the final output: `fid` is a fresh
   `row_number()` per run, not a stable identifier a caller could join
   back against their own data, while the resolved identity is.
   Single-letter `a`/`b` prefixes (rather than `left`/`right`) keep every
   generated field name within a Shapefile DBF field's 10-character limit
   even for a longer family name like `name1`.

`depth_column`/`--depth-column` is checked up front against the tool's own
fixed output column names (`left_fid`, `right_fid`, `geom`), raising
`ValueError` on a collision rather than letting DuckDB silently rename the
duplicate; the fid check (step 7) runs before `left_fid`/`right_fid` are
dropped (step 8).

## Portolan-scale profiling

The real target scale for this tool, `--debug` off, `threads=4`, Apple
Silicon:

| Run | Fids in / rows out | Wall time | RSS peak | Result |
| --- | --- | --- | --- | --- |
| Global admin4 (`portolan/global/admin4/admin4.parquet`, 225,316 rows, `--depth-column pkg_lvl`) | 225,316 / 679,765 rows | 501 to 557 s | 4.2 to 5.1 GB | Every finest-level fid present in the output |
| Indonesia admin2 (`portolan/original/idn/latest/idn_admin2`) | 9,503 rows | 24 s | 1.6 GB | Every finest-level fid present in the output |

Noding and labelling run per 1° tile, so cost grows with the busiest tile
(2.27M vertices globally), not with the whole input.
