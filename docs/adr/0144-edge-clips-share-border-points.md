# 0144: Edge clips share border points, with no final stitch or whole-table check

## Status

Accepted. For `edge-mosaic` and `edge-match` only, supersedes the final
`edge-stitch` pass (with its `docs/adr/0089` escalation) and the
`check_valid_topology` call on their output kept by `docs/adr/0035`.
Standalone `edge-stitch` and `edge-clip` are unchanged.

## Context

Each input row is clipped to its own overlay polygon, so the two sides of an
overlay border end up with different vertices, and a whole-table stitch had
to close the seams. At global scale the stitch and the whole-table check
run out of memory: `has_invalid_edges` puts all 1.2M parts into one
`ST_CoverageInvalidEdges_Agg` and was killed at 15,360 MB about 10 minutes in.

## Decision

- Before any clip, candidate border points are collected from every input
  file and the overlay tiles: overlay vertices, tile cut points, input
  vertices and crossings, in that priority. One point is kept per cluster
  within `SHARED_POINT_TOLERANCE` (5e-9). Tiles and input rows snap to the
  kept points, and rows also snap to overlay vertices within
  `OVERLAY_VERTEX_SNAP` (2.5e-8). Both sides of each border then share
  vertices, and a plain clip gives matching edges.
- `edge-mosaic` and `edge-match` run no stitch and no `check_valid_topology`.
  Each run ends with a bounded invalid-edge check: every pair of touching
  parts from different rows goes through `ST_CoverageInvalidEdges_Agg` in
  vertex-bounded batches. Each flagged pair is then rechecked with every part
  near either side, keeping only edges along both. A query that runs past
  `INVALID_EDGE_BATCH_SECONDS` (300s) stops, and its pairs are reported as
  unchecked. The results are `kind='invalid-edge'` issue rows. The check does
  not raise or fix anything.
- `edge-mosaic` and `edge-match` write no `gap` rows. A gap check needs a
  union of the whole output, and at global scale that runs out of memory
  (killed at 15,360 MB). Gap reporting is left to a follow-up.

## Consequences

- Global prototype: 54 minutes at a 14 to 15 GB peak, with 0 pieces across
  378 touching country pairs. The overlay vertex snap moves row vertices
  by up to about 2.8 mm.
- Global run (219 inputs, raw adm0): assign and clip took 2h10 at an 11.3 GB
  peak, the micro merge fixed 32 parts, and the invalid-edge check took
  8 minutes and flagged 149 row pairs. The outputs stage, run on the saved
  clip tables, took 3 minutes at a 10.2 GB peak. The output has 354,967 rows
  and 1,196,717 parts.
- Small runs also get no `gap` rows.
- West Africa (8 countries): 19.4s, 0 invalid-edge rows, and the whole-table
  `has_valid_topology` is True.
- A run can export with invalid-edge rows. Inputs that overlap each other,
  such as alternate versions of one country (IND and IND_1), give thousands
  of rows, and the recheck then takes about 40 minutes. Input defects are
  fixed upstream.
