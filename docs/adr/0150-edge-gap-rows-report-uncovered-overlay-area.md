# 0150: Edge gap rows report uncovered overlay area

## Status

Accepted. Supersedes the `gap` rows in `docs/adr/0149`.

## Context

`gap_issues_sql` finds only enclosed holes, and `--no-clean` runs wrote no `gap`
rows at all, so overlay area left uncovered along an open edge went unreported.

## Decision

`edge-mosaic` and `edge-match` write `gap` rows from
`uncovered_overlay_issues_sql` in both modes. Each clip records its rows' area
per overlay polygon, and only an overlay falling short by more than
`UNCOVERED_AREA_RATIO` is unioned.

## Consequences

- A global `--no-clean` run reports gaps. Its union cost on large overlays is
  unmeasured at global scale.
- A hole in the overlay's own shape is not reported.
