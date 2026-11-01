# 0146: Offset Voronoi points per line, drop map-cut points, check every cell

## Status

Accepted.

## Context

GEOS's Voronoi diagram can return wrong cells for evenly spaced points that
line up across several straight lines (libgeos/geos#1040, open in GEOS 3.14
and 3.15). DuckDB spatial calls it with tolerance 0 and exposes no option.
A wrong cell overlaps its neighbours, and the merge stage splits the overlap
into straight strips. The fieldmaps OSM adm0 layer hit this: one cell covering
173.9°E to 180°, pole to pole, was assigned to NZL, FJI, TUV, KIR and ATA.
Two inputs trigger it: edges on the map cuts (x = ±180, y = -90), and
aligned straight edges such as squares or square EEZs.

On a 15-country Pacific subset and a 4-square synthetic input, these
variants were tested:

| Variant | Synthetic | Pacific |
| --- | --- | --- |
| Unchanged | fails | fails (20 bad cells) |
| 1e-9 jitter per point | passes | fails (TopologyException) |
| Per-line start offset | passes | passes |
| Frame of points around the world | fails | fails |
| Drop points on map cuts | fails | passes |

All variants passed on 19 real layers away from the map cuts.

## Decision

- Start each line's points at a stable offset of up to one spacing, from a
  hash of the fid and the line's geometry.
- Generate no points on x = ±180 or y = ±90. Those edges are map cuts, not
  borders.
- After assigning points to cells, raise if any cell holds more than one
  distinct point, naming the worst cell's point count, fid count and bbox.

## Consequences

- On the Pacific subset the strip is gone and the merge stage takes 175s
  instead of 651s. NZL's area inside 173.9°E to 180° drops from 1017 to
  204 square degrees, and FJI's rises from 2 to 105.
- Output moves on every extended edge by up to one spacing. On the West
  Africa adm2 cluster, row counts and attributes are unchanged, at most
  0.016% of a country's area changes fid, and at most 0.36% of one row.
- Three `edge-match` test expectations were set by wrong cells. With correct
  cells, inputs far from the winning overlay polygon clip empty.
- The offset uses DuckDB's `hash()`, which is stable within a DuckDB version
  but not promised across versions, so an upgrade can move points.
- A layer built before this change may hold wrong cells even without visible
  strips.
