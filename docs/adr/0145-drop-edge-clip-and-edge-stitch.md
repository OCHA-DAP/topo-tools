# 0145: Drop edge-clip and edge-stitch

## Status

Accepted. Supersedes `docs/adr/0089`, and removes the standalone tools
`docs/adr/0144` left unchanged.

## Context

`edge-match` and `edge-mosaic` clip with shared border points and run no
final stitch (`docs/adr/0144`). The standalone `edge-clip` and `edge-stitch`
tools each ran one step of those pipelines on its own, a step the COD-AB
workflow never runs by itself. Each one added CLI options, docs and tests,
and made it harder to pick the right edge tool.

## Decision

- Remove the `edge-clip` and `edge-stitch` CLI commands, their `api`
  functions, and `core.edge_stitch`.
- Remove `coverage_clean_escalating` and the `SNAP_ESCALATION_*` constants,
  which only `edge-stitch` used.
- Keep the clip engine `edge-match` and `edge-mosaic` share, as `core.clip`.

## Consequences

- Scripts that call either command fail. The release notes list the removal.
- `edge-mosaic` with one input file replaces `edge-clip`, and `topo-clean`
  on a combined file replaces `edge-stitch`. On Ghana adm2 against a West
  Africa adm0 overlay, `edge-mosaic` gave the same 260 rows and issue rows
  as `edge-clip`, with byte-identical geometry. On Ghana and Togo clipped
  separately (318 invalid edges), `topo-clean` and `edge-stitch` both left
  0 invalid edges, with byte-identical geometry on all 300 rows.
- Earlier ADRs that name `edge-clip`, `edge-stitch` or `core.edge_clip`
  describe the code as it was on their own date.
