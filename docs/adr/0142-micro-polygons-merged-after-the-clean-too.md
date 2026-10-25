# 0142: Micro-polygons merged after the clean too

## Status

Accepted. Extends ADR-0117.

## Context

A North America `edge-mosaic` stitch raised `MICRO_POLYGONS`. Clipping
CAN's coast against the ADM0 coast made a 1.14 mm sliver, just over the
1.11 mm micro width, so the pre-clean merge kept it; `ST_CoverageClean`'s
snap then narrowed it to 0.91 mm. Main and this change produce the same
sliver.

## Decision

`coverage_clean` merges micro-polygons in its own output as well, adding
the rows to the same micro issues table.

## Consequences

Every `coverage_clean` caller gets it, and the escalation loop validates
after it. On North America, the second merge moved one part into its
neighbour, and the output passed the invalid-edge and gap checks.
