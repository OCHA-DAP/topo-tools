# 0140: Heavy geometries split with `ST_Subdivide`

## Status

Accepted. Supersedes ADR-0016 and ADR-0017.

## Context

The adaptive grid tiler split only the clip worker's overlay boundary. The
global `edge-mosaic` build ran out of memory where other joins copied whole
multi-million-vertex geometries per pair (detached-part neighbours, the
detached-part overlay clip).

## Decision

One helper, `subdivide_sql`, splits any geometry of at least
`SUBDIVIDE_MIN_VERTICES` (5000) vertices into `ST_Subdivide` pieces of at
most `SUBDIVIDE_MAX_VERTICES` (1350), for the clip worker, the assign tile
cache, detached-part neighbour boundaries and the detached-part overlay
union.

## Consequences

`core/edge_clip/_tiling.py` and the `CLIP_TILE_*` constants are removed.
Clip pieces differ from the grid tiles, so outputs drift at float level only
(West Africa parity: max relative symmetric difference 8.7e-12).
