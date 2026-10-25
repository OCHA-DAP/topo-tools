# 0143: Gap checks union validated coverages with `ST_CoverageUnion_Agg`

## Status

Accepted.

## Context

The gap check and gap issue rows union the whole output with
`ST_Union_Agg`, 140s each on a North America stitch output. GEOS's
`CoverageUnion` is several times faster but assumes a valid coverage, and
`topo-detect` and `topo-clean`'s gap width step run on raw input.

## Decision

`has_gaps`, `check_gaps` and `gap_issues_sql` take `coverage` (default
False, `ST_Union_Agg`). Only call sites that have already passed
`has_invalid_edges` pass True: `check_valid_topology`,
`has_valid_topology`, `topo-clean`'s post-clean check, and every output
stage's gap issue rows. Both unions read polygon parts only, since
`CoverageUnion` raises on mixed dimensions.

## Consequences

North America stitch output: 24s against 140s, per check and per issue
build. On West Africa and North America outputs with units removed to
punch holes (53 and 405 holes), both unions give the same holes.
