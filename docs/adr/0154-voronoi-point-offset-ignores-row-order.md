# 0154: Hash only the normalized line for the Voronoi point offset

## Status

Accepted. Supersedes the offset bullet of `docs/adr/0146`; its map-cut and
cell-check decisions stand.

## Context

ADR-0146 hashed the fid and the line's geometry to pick each line's point
offset. The fid is the input row number, and the merged line's direction and
start vertex follow aggregation order, so the same geometry in a different
row order built a different point set. On CAN, two inputs with identical
geometry in different row orders built different sets, and one left a point
in no Voronoi cell, failing `edge-extend` on one build but not the other.

| Input | Change | Points differing |
| --- | --- | --- |
| gmb adm2 | rows shuffled | 49,762 of 50,720 |
| gmb adm2 | rings reversed | 90,509 |
| sen adm3 | rows shuffled | 405,754 |

## Decision

- Hash only the line's geometry, after `ST_Normalize`, for the offset.

## Consequences

- The same geometry builds the same points for any row order, ring direction
  or thread count. Both CAN inputs build identical points (8,604,926) at
  threads 2 and 5, and both pass.
- Output moves on every extended edge by up to one spacing, once.
- A point landing in no Voronoi cell remains possible for some point sets;
  such a file now fails on every run instead of on some.
