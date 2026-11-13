# 0151: package cleans and validates by default

## Status

Accepted.

## Context

Validation ran as a separate `validate` command, easy to skip before packaging.
`package` raised on any topology defect, so a layer from `edge-mosaic
--no-clean` (seams along every border it clipped) could never be packaged, and
a global coverage clean does not fit in 16 GB.

## Decision

`package` coverage-cleans its input when it has defects, then runs
schema-, topo-, code- and name-detect and stops before writing any output on an
error row. `--no-validate` skips the checks. `--no-clean` skips the clean and
the topo check, and `package-polygons` writes invalid-edge rows to each level's
issues file in place of raising, without gap rows, since the gap scan's union
fails on unmatched edges. The `validate` command is removed.

## Consequences

- Packaging a layer with a placeholder name, duplicate code or similar error
  requires fixing it or passing `--no-validate`.
- A `--no-clean` output has seams and no gap check.
