# 0155: Round every final geometry output to the 1e-9 grid, then validate

## Status

Accepted. Moves `docs/adr/0037`'s gap check from `topo-clean`'s clean stage
to its outputs stage; the check itself stands.

## Context

Tools wrote full float64 coordinates. Esri stores lon/lat on a 1e-9° grid
(default XY resolution) with a 0.001 m (about 1e-8°) default XY tolerance,
and `SNAP_TOLERANCE` is already 1e-8. Rounding partway through a tool would
let later steps build on rounded values, so rounding belongs once, after the
last geometry change, with validation on the rounded result. DuckDB spatial
2.0.1's only grid-rounding function is `ST_ReducePrecision`, which snap-rounds
each geometry on its own.

Per-polygon rounding of two extended layers kept each coverage intact:

| Layer | Units | Vertices | Off-grid share | Invalid edges at 1e-8, before / after |
| --- | --- | --- | --- | --- |
| sen admin3 | 121 | 90,882 | 55% | 0 / 0 |
| tha admin3 | 7,425 | 15,646,950 | under 1% | 0 / 0 |

Neither layer gained an invalid or empty geometry or lost a vertex.

## Decision

- `finalize_geometry()` (`core/coverage.py`) rounds a table's `geom` in place
  to `GRID_RESOLUTION` (1e-9), then validates per `validate=`: `coverage`
  (invalid edges, gaps up to `gap_maximum_width` unless `None`,
  micro-polygons), `issues` (`invalid-edge` rows under `--no-clean`), or
  `none`.
- Every row with empty geometry is dropped from a tool's output, never
  written: rows rounding empties (under about 1e-9 wide or long, a
  digitization artifact) and rows a fix already emptied, such as a unit
  `topo-clean` finds fully covered by another. Each drop logs a warning,
  since a tool run on its own may meet such rows, and tools with a
  shared-schema issues report add a `kind='empty-geometry'` row per dropped
  row, `fixed` FALSE so a lost unit gets reviewed. `topo-clean` also reports
  a removed unit on its overlap row, `fixed` FALSE. Issues reports keep their own rows, so a defect narrower than the
  grid is still reported.
- Every outputs stage calls it on its final table(s) before any check, issue
  row or export; `export_issues_table()` calls it with `none`. Internal
  tables, including `package`'s cleaned intermediate, are not rounded.
- `topo-clean` returns its fill width from the clean stage and checks gaps
  at that width in the outputs stage, after rounding.

## Consequences

- Every output vertex sits on the grid, and a FileGDB export stores the same
  coordinates the tool validated.
- `ST_ReducePrecision` rebuilds each polygon, so ring orientation and start
  vertex change even for on-grid input; tools that never modify geometry
  (`schema-join`, `schema-fill`, `code-*`) no longer return byte-identical
  geometry, only geometry equal after `ST_Normalize`.
- `topo-clean` raises on a micro-polygon in its output, which its own
  `merge_micro_polygons` pass already removes.
