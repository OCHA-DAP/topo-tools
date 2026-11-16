# 0152: coverage clean merges sliver gaps by vertex spacing

## Status

Accepted. Supersedes the `thin` mode of 0033.

## Context

Default cleaning fills a gap only up to `SNAP_TOLERANCE` (1e-8°, about Esri's
1 mm XY tolerance). A needle gap 1.4 mm wide and 1.6 km long in UNHCR Brazil
adm2 passed cleaning and broke `edge_extend`. `topo-clean --maximum-gap-width
thin` picked gaps by thinness but passed one file-wide width to
`ST_CoverageClean`, filling every narrower gap whatever its shape. Thinness
alone does not separate slivers from water: in the latest portolan layers a
200 m river (0.087) and the Sivash lagoon (0.015) are as thin as 4 m and 34 m
digitising slivers (0.038, 0.077). A fixed width does not scale from city
blocks to countries. GEOS, Esri, QGIS and GRASS fill by width or by a user's
selection, with no default beyond snap tolerance.

## Decision

After `ST_CoverageClean`, `coverage_clean()` merges each enclosed gap with
r = 2 · area · vertices / perimeter² ≤ 0.2 (mean gap width at most a fifth of
its edges' mean segment length) into the neighbour sharing its longest
border, and writes a `sliver-gap` issue row for it. The test is relative to
each dataset's own digitising resolution, so one cutoff holds at any scale.
Vertices count the ring's closing point, so with four or more vertices
r ≤ 0.2 implies thinness ≤ 0.25 and compact holes are kept. `edge_extend`'s
merge stage skips the pass, since its output has no gaps.
`--maximum-gap-width thin` is removed.

## Consequences

- Of 2,474 enclosed gaps in 282 latest portolan layers, 138 wider than 1e-8°
  qualify, all in `ukr_admin4`, `col_admin3` and `mmr` layers. None of the
  other 73 layers with gaps has one (lowest r 0.49). Rivers and lagoons sit
  at r ≥ 2.
- A real feature drawn with few vertices and long straight sides, such as a
  canal drawn as a bare rectangle, qualifies and is merged.
- Simplified input fills more gaps, densified input fewer.
- Finding gaps needs a coverage union: +17 s on `phl_admin4` (41 s clean),
  +6 s on `idn_admin4` (39 s), +0.1 to 0.4 GB peak memory.
