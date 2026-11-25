# 0158: Re-check invalid edges at hole pinch vertices against each ring

## Status

Accepted.

## Context

GEOS 3.15.1's coverage validator tests a segment that meets a neighbour at a shared vertex against each of the neighbour's ring corners there separately (`InvalidSegmentDetector::isInteriorSegment`), and any corner that reads it as interior marks it invalid. Where the neighbour's hole touches its own shell at that vertex, the hole corner reads almost every direction as interior. A free coastline edge from that vertex is then flagged. `ST_CoverageClean` leaves it in place, so `topo-clean` and `package` raise `INVALID_EDGES` on EuroRegionalMap Ireland (2 edges in 3455 units). `geosop` reproduces both the flag and the unchanged clean output, with no DuckDB involved.

## Decision

`_invalid_part_pairs()` finds pinch vertices: a vertex of a holed part that lies on a hole and on another ring of the same part. Only a flagged segment with an endpoint on a pinch vertex of either part is re-checked. Each part of its pair is split into one polygon per ring, and every ring pair touching the segment is validated on its own. The segment is dropped if no ring-pair check flags it. Segments are compared by exact endpoints. Every other flagged segment is kept unchanged.

## Consequences

Every other reason GEOS flags a segment (crossing, collinear overlap, near-parallel within `SNAP_TOLERANCE`, a vertex inside the neighbour, a segment inside the shell's corner) still fires against the shell or a hole polygon alone, so real defects at a pinch stay flagged. A hole-hole pinch away from the shell stays misflagged, since the shell polygon contains it. Across 397 portolan `original/*/latest` admin layers, 9 have invalid edges (66 unit pairs) and the re-check drops none of them. The re-check can go once GEOS tests a pinch vertex's corners together.
