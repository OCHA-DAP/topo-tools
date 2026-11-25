# 0158: Re-check invalid edges against each ring of a holed part

## Status

Accepted.

## Context

GEOS 3.15.1's coverage validator tests a segment that meets a neighbour at a shared vertex against one ring corner of that neighbour (`InvalidSegmentDetector::isInteriorSegment`). Where the neighbour's hole touches its own shell at that vertex, the hole corner can be the one tested, and its interior wedge covers almost every direction. A free coastline edge from that vertex is then flagged as interior. `ST_CoverageClean` leaves it in place, so `topo-clean` and `package` raise `INVALID_EDGES` on EuroRegionalMap Ireland (2 edges in 3455 units). `geosop` reproduces both the flag and the unchanged clean output, with no DuckDB involved.

## Decision

`_invalid_part_pairs()` re-checks each flagged part pair where either part has a hole. Each part is split into one polygon per ring, and every ring pair touching the flagged edge is validated on its own. A flagged segment no ring-pair check flags is dropped. Segments are compared by exact endpoints.

## Consequences

Every other reason GEOS flags a segment (crossing, collinear overlap, near-parallel within `SNAP_TOLERANCE`, a vertex inside the neighbour, a segment inside the shell's corner) still fires against the shell or a hole polygon alone, so real defects stay flagged. Pairs without holes skip the re-check. Across 397 portolan `original/*/latest` admin layers, 9 have invalid edges (66 unit pairs) and the re-check drops none of them.
