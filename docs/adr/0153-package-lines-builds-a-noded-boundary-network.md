# 0153: `package-lines` builds a noded boundary network

## Status

Accepted. Supersedes the pair-finding and exterior-difference part of 0097.

## Context

`edge-mosaic --no-clean` output keeps clip seams: two neighbours overlap by
up to 5e-17 deg², and one side carries T-junction vertices about 1e-16°
off the other side's segment. Per-pair extraction (`ST_Touches`, then
`ST_Intersection` of the two boundaries) drops such a pair or misses the
stretches beside each T-junction, so the border comes out as two exterior
lines. In the fieldmaps Georgia, Serbia and Xinjiang layers this left
317.6, 521.8 and 1061.9 km of depth-0 border without a shared line.

## Decision

Node every unit boundary together into one planar line network, then
label each edge by the units whose boundary lies within
`10 * SNAP_TOLERANCE` of its midpoint: one unit is exterior, two are
shared, and more than two keep the two nearest. Noding runs per 1° tile
(`TILE_DEG`), with boundaries clipped to the tile plus `TILE_MARGIN_DEG`
so a line cut at the tile edge still meets its node. Two copies of one
border with the same labels in one tile take each other's vertices
(`ST_Snap` within the same tolerance, which only inserts vertices).
Edges are clipped back to their exact tile, cut points a rounding error
apart are snapped together, and each label pair is line-merged. Edges are
normalised after noding and every aggregate is ordered, so the output is
the same on every run. `ST_ReducePrecision` and snapping before noding are
not used: snapping both sides swaps near vertices.

## Consequences

On a clean coverage the linework matches per-pair extraction to within
1e-9°. On seamed input the shared borders are recovered: raw Georgia
input gives the same rows as its cleaned copy. Global admin4 (225,316
units, `threads=4`) runs in 425 to 446 s at a 3.8 to 4.2 GB peak, against
636 s and 3.7 GB for per-pair extraction on the same machine.
