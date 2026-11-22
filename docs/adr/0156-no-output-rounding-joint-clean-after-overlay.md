# 0156: No output rounding; one joint coverage clean after a per-row overlay

## Status

Accepted. Supersedes ADR-0155. The empty-row handling and `topo-clean`'s
outputs-stage gap check from ADR-0155 stand.

## Context

ADR-0155 rounded every final output to the 1e-9 grid with
`ST_ReducePrecision`, which snap-rounds each geometry on its own. A valid
coverage needs neighbours to share bit-identical vertices, and rounding each
row separately can't guarantee that: two near-equal values on either side of
a grid boundary round apart, and a vertex within one grid cell of a
neighbour's edge collapses onto it (a CMR/GNQ seam in a USGS admin0 layer,
and a synthetic pair).

Seams at 1e-8 come from per-row overlays: each unit clipped against its own
overlay polygon computes border crossings separately. `edge-mosaic` on
country pairs, admin0 overlay cut to the pair, invalid-edge parts at 1e-8:

| Pair | `--no-clean`, no rounding | `--no-clean`, rounded | Cleaned, either |
| --- | --- | --- | --- |
| sen + gmb admin2 | 148 | 148 | 0 |
| ben + tgo admin1 | 17 | 17 | 0 |
| slv + hnd admin1 | 422 | 422 | 0 |
| gtm + blz admin1 | 1570 | 1570 | 0 |

Rounding removed none of these seams; one `coverage_clean()` over the
output removed all of them. Single-country `edge-mosaic` (one overlay
polygon), `package` (per-group union of an exact coverage), `edge-extend`
and `topo-clean` on global admin0 layers kept 0 invalid edges and 0 invalid
rows without rounding.

A FileGDB export snaps every vertex to a 1e-9 grid itself. Shared vertices
round to the same grid point, so an exact coverage stays exact: global
admin1 (1,916 units) and sen admin3 kept 0 invalid edges through a
round trip.

## Decision

- No tool calls `ST_ReducePrecision`. `finalize_geometry()` drops empty
  rows and validates per `validate=`; it doesn't round.
- A step that cuts rows against another layer (clip or intersect to an
  overlay) is followed by one `coverage_clean()` over the whole output.
  Steps that only dissolve an exact coverage, or extend it by Voronoi, need
  none.
- Final polygon outputs are validated with `ST_CoverageInvalidEdges` at
  1e-8 and `ST_IsValid`.

## Consequences

- Output coordinates are full float64; a FileGDB export rounds them on
  write without breaking an exact coverage.
- `--no-clean` output of `edge-match`/`edge-mosaic` keeps its seams and
  reports them as `invalid-edge` rows (ADR-0149).
- A coverage with features closer than 1e-9 without sharing a vertex can
  still break on FileGDB export; none of the tested layers had one.
