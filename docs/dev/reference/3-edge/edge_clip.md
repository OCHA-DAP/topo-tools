---
title: "edge-clip"
sidebar:
  order: 4
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `edge-clip` MUST load the input layer and the overlay layer raw,
  neither coverage-checked nor -cleaned.
- `edge-clip` MUST NOT require or read an `overlay_fid` column on the input polygons
  layer.
- `edge-clip` MUST accept exactly one input file and exactly one
  overlay file per call, a strict 1:1 primitive (see `docs/adr/0080`);
  batching many input files against one shared overlay load is
  `edge-mosaic`'s job (see `docs/dev/reference/3-edge/edge_mosaic.md`).
- The output row MUST carry a `source_file` column recording the path of
  the input file it came from.

## Assignment

- `edge-clip` MUST internally assign every input polygon to exactly one overlay polygon before
  clipping, via `assign-one`'s file-wide majority-vote strategy (see
  `docs/dev/explanation/3-edge/assign.md`): every input polygon is forced onto the one overlay polygon
  that wins a majority vote by count, unconditionally, not evaluated per
  input polygon. An input polygon with zero individual overlap with the winner is not
  dropped at this stage; it still gets clipped against the winner and MAY
  drop later if that clip result is empty (see Clipping).
- A whole input file with no overlap against any overlay polygon at all MUST be
  dropped, not clipped against the wrong overlay polygon.

## Clipping

- `edge-clip` MUST snap the overlay tiles and input polygons to shared border points
  before clipping (see `docs/adr/0144`).
- `edge-clip` MUST clip each row to its own `overlay_fid`'s geometry via
  `ST_Intersection`, one distinct `overlay_fid` at a time, each in its own
  spawned OS subprocess.
- Within one `overlay_fid`'s subprocess, `edge-clip` MUST grid-subdivide that
  overlay polygon's boundary into small tiles before intersecting once its vertex
  count exceeds an adaptive threshold, sizing the tile grid from that
  overlay polygon's own vertex density, and MUST join input polygons to tiles via bbox
  comparison, never `ST_Intersects`.
- An input polygon whose clipped result is empty MUST be dropped from the output,
  not treated as fatal, and MUST be recorded in the issues report as a
  `kind='clip-empty'` row (see Outputs).
- `edge-clip` MUST merge or keep every clip-detached piece in the clipped result, recording each one with an edge
  neighbour as a `kind='detached-part'` row.
- `edge-clip` MUST merge or drop every micro-polygon in the clipped result, recording each as a
  `kind='micro-polygon'` row.
- `edge-clip` MUST raise immediately on the first `overlay_fid` whose subprocess
  fails, aborting the whole run rather than skipping just that `overlay_fid`.

## Outputs

- `edge-clip` MUST NOT run the coverage check on its own output.
- `edge-clip` MUST raise `RuntimeError` if the clipped result has zero rows.
- `edge-clip` MUST export the clipped layer to the output file.
- `edge-clip` MUST export an issues report alongside it, using the same columns as every other tool's issues report, whenever it has at least one
  `kind='clip-empty'`, `kind='detached-part'` or `kind='micro-polygon'` row (or, when a code join is given, one
  `code-mismatch`/`code-fallback` row); when it would be empty, no file
  MUST be written (and a stale file from a previous run at that path MUST
  be removed).

## Configuration (`api.edge_clip.clip()` / CLI)

- The output path MUST default to the input path with a `_clipped` suffix.
  The issues-report path MUST default to the output path with an `_issues`
  suffix.
- `edge-clip` MUST raise `FileExistsError` if either output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `assign`, `edge-clip`, `outputs`;
  any other value MUST raise `ValueError`.
- `edge-clip` MAY accept `match_column`/`overlay_match_column`/`input_match_column`
  to override spatial assignment with an exact code join (see `docs/dev/explanation/3-edge/assign.md`), adding
  `code-mismatch`/`code-fallback` rows to the issues report alongside any
  `clip-empty` rows.
- `edge-clip` MAY accept `carry_columns` (CLI: `--carry-column`) to copy
  named overlay columns onto every matched input polygon (see `docs/adr/0077`).
- `edge-clip` MAY accept `original_path` (CLI: `--original`, env
  `ORIGINAL_FILE`), the input layer's pre-extension original, in any
  supported format or as a URL. Without it, no clip-detached piece merges.

## Examples

### Example 1: clip an input layer against an overlay layer, explicit output

    topo-tools edge-clip input.parquet adm1.geojson clipped.parquet

### Example 2: custom issues report path

    topo-tools edge-clip input.parquet adm1.geojson clipped.parquet \
      --issues-file clip_report.parquet
