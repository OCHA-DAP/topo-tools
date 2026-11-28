# 0159: Normalize antimeridian-crossing and out-of-range longitudes at read

## Status

Accepted.

## Context

SPC's 2017 Fiji tikina boundaries hold 4 island parts (adm2_pcode 301, 303, 304, 702) written in ±180 form. Each has two edges that jump about 360° from near 180 to near -180. Their bbox stays inside ±180, so the world-bounds clip passes them. Planar GEOS then reads each part as a band around the globe, and `topo-detect` runs out of memory on the file. Across 126 cod-app explorer/source layers and 403 portolan `original/*/latest` admin layers, the only parts wider than 180° are these 4 and the Antarctica polygon in the FAO, UNICEF and WFP adm1 layers. Antarctica is drawn with explicit ±180 legs and has no jump edge. No layer has a longitude past ±180. A read that only clipped longitudes past ±180 would drop the part of a 0..360 source past 180 and raise on a part lying wholly past it.

## Decision

`reproject_select_sql()` treats every polygon edge as the shorter way around the globe (RFC 7946). It runs after reprojection and before `ST_MakeValid` and the world-bounds clip. Two kinds of edge never count as a jump. One has both ends within `SNAP_TOLERANCE` of ±90° latitude and runs along a pole. The other has both ends exactly on ±180° longitude and spans a band, as in a rectangle drawn from -180 to 180. A part changes only if one of its rings has a jump edge or the part has a longitude past ±180. Each ring is unwrapped on its own by adding 360 times the running sum of jump signs, made valid, and cut into 360°-wide windows over its own extent. Each piece shifts back into ±180, and the holes' pieces are subtracted from the shell's. A piece narrower than 1e-6° is float overshoot and is dropped when a wider piece remains, so a coordinate a hair past 180 is clipped and a tiny part wholly past it is shifted. A ring whose jumps don't sum to zero circles a pole without a pole edge, and the read raises naming the file and the feature's source row. A row-level `CASE` on a one-pass `ST_Extent` leaves every other row untouched, so its bytes don't change. DuckDB evaluates both sides of an `AND` on every row, so the costly per-ring check sits inside the `CASE`.

## Consequences

Every tool's read handles crossing parts, Natural Earth-style and explicit-leg Antarctica bands, and sources in 0..360 or shifted by whole turns. Callers need no workaround of their own. The 4 Fiji parts keep their spheroid area (230.41, 42.82, 1.57 and 7.24 km²). `topo-detect` on the file finishes in under 2 s. The 2 pieces of one crossing part sit 360° apart in planar space, so `package-lines` draws an exterior line along ±180, and an `edge-mosaic` original's bbox spans the globe. A ring circling a pole without a pole edge isn't auto-closed. A ring that crosses 180 through vertices placed exactly on ±180 stays a planar band. Neither shape appears in either catalog. On the largest cod-app layer (geoBoundaries adm2, 49,152 rows) the read takes 11.4 s against 11.5 s without the check, within run-to-run noise.
