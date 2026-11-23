# 0157: A shared level name vetoes an edge justified only by no embedding anywhere

## Status

Accepted. Refines `docs/adr/0070`.

## Context

`docs/adr/0070` lets the matcher chain past a level when no column embeds any other. On a single-level layer whose name column repeats values, containment alone then chains the name column as a coarser level over its own code column.

## Decision

An edge justified only by no embedding anywhere is vetoed when both columns' names match through their first digit (`adm0_pcode`, `adm0_name`). A lone code column with a contained name column at most 30% collapsed stays one level.

## Consequences

Names act only as a veto. Columns sharing a digit but not a level name (`prov_2019`, `iso3`) still chain. Corpus: 3 of 677 crosswalks change, all point files.
