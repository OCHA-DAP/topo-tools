# 0147: Build each merged fid from its cell and its original outside the cell

## Status

Accepted.

## Context

The merge stage built each fid as its original plus an extension piece (its
Voronoi cell minus every nearby original, its own included), then dissolved
the two with `ST_Union_Agg`. The piece shares the fid's whole coastline, so
the dissolve noded every coastline segment twice only to remove it. On the
fieldmaps OSM adm0 layer the merge took 13,849s, 6,617s of it in the
dissolve and 1,649s for Canada alone.

Two cheaper dissolves were tested on that layer:

| Variant | Result |
| --- | --- |
| `ST_CoverageUnion` per fid | 125s, but invalid or raising for 42 of 315 fids |
| `ST_Snap` the original to its piece, then `ST_CoverageUnion` | Valid, but over 900s on a 1M-vertex fid |

Coverage union fails where a cell edge crosses the fid's own coastline: the
difference adds a vertex there that the original lacks, so the pair is not
a noded coverage.

## Decision

Build each fid as its cell, snapped to and minus the nearby originals of
other fids, unioned with the part of its original outside the cell.
Originals don't overlap, so this equals the original plus the cell minus all
originals, without overlaying the fid's own coastline. A fid with no cell
keeps its original.

## Consequences

- On the fieldmaps adm0 layer the merge takes 465s instead of 13,849s, with
  peak RSS 5.8 GB instead of 4.9 GB.
- Output matches to float level, with identical attributes. The largest
  symmetric difference per row is 3e-15 square degrees on the world layer
  and 4e-14 on the West Africa adm2 cluster.
- Rows gain collinear vertices where a cell edge crosses a border: 0.04%
  more vertices on the world layer and a median of 0.27% across 42 portolan
  admin1/admin2 files, up to 4.6% on a 963-vertex file.
- A cell is snapped to other fids' originals only, not its own.
