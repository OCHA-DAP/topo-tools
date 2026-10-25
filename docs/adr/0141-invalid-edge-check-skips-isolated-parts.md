# 0141: The invalid-edge check skips isolated parts

## Status

Accepted.

## Context

`ST_CoverageInvalidEdges_Agg` on a Canada stitch input ran over 31
minutes. GEOS's CoverageValidator wraps every envelope-near polygon per
target and builds a point-in-area index per target, so thousands of islets
near one multi-million-vertex polygon cost quadratic time.

## Decision

`has_invalid_edges` validates only parts with another part within
`SNAP_TOLERANCE`, found by `_near_part_pairs`: a grid-cell equi-join on
bboxes (cell sized to the 99th-percentile part extent), heavy parts split
with `subdivide_sql`, then `ST_DWithin`. A part with no part within the gap
width can't produce or receive an invalid edge, so the result is the same.
`merge_micro_polygons` and `merge_detached_parts` pair through the same
primitive.

## Consequences

Canada rows: 24.6s. North America stitch output (217k parts): 39s. 16
defect cases (overlap, shifted, shrunk, islets, same-row multipart, light
and heavy parts) agree with the whole-table check. DuckDB's R-tree Spatial
Join and a bbox range join were both slower than the grid.
