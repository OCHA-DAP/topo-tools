# 0149: Edge clips are plain, with a final coverage clean

## Status

Accepted. For `edge-mosaic` and `edge-match`, supersedes the shared border
points and the removal of the final clean in `docs/adr/0144`. Its bounded
invalid-edge check stays, for `--no-clean` runs. Superseded in part by
`docs/adr/0150`.

## Context

Two independent clips of one overlay border give different vertices. Snapping
tiles and rows to shared border points before the clip avoided a whole-table
clean, which ran out of memory at global scale. `ST_Snap` can make a geometry
invalid, and nothing checks validity between the snap and the clip: NLD admin2
into admin1 had 5 invalid rows after it.

## Decision

- Rows clip directly to the overlay tiles, with no snap and no shared points.
- `coverage_clean()` runs once over the clipped output, then
  `check_valid_topology` raises on what remains. Passthrough rows skip both, so
  under `--merge` they keep their geometry and any overlap with their neighbours.
  `gap` rows are written again, limited to the clip targets (`docs/adr/0133`).
- `--no-clean` skips the clean, the topology check and the `gap` rows. Micro
  polygons are merged, and seams are reported as `invalid-edge` rows.
- The clean makes no retry at a wider snap distance, so every tool shares one
  `coverage_clean()`.
- `edge-mosaic` runs `check_valid_topology` with `gap_maximum_width=0` on every
  input file, always, and raises naming the file. Slivers are checked at
  `SNAP_TOLERANCE`.

## Consequences

- West Africa (8 countries, 1,839 rows): 0 invalid edges in about 20 seconds.
  With `--no-clean`: 343 `invalid-edge` rows, all along overlay borders.
- NLD admin2 into admin1 with `edge-match`: 342 rows and 0 invalid edges, with
  169 `invalid-edge` rows under `--no-clean`.
- Haiti admin3 into its admin0 needs no retry on the current ADM0.
- A global run needs `--no-clean`, so its output keeps seams. They are vertex
  mismatches, not visible at map scale.
- The input check adds up to about 12 minutes to a 227-file run. Legacy `plw`
  and `usa` extended files raise until they are replaced.
- A Haiti-type seam raises at the final check, and that run needs `--no-clean`.
