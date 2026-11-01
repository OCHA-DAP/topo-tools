---
title: "edge-extend"
sidebar:
  order: 3
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `edge-extend` MUST read the input and reproject it to EPSG:4326.
- Before checking for coverage violations, `edge-extend` MUST close every
  notch in the input the way `topo-clean` does.
- If the reprojected input has any coverage violation (an overlap or a
  mismatched shared edge), `edge-extend` MUST correct it before continuing;
  otherwise it MUST leave the input unmodified.
- Correcting a violation MAY shift any polygon's boundary, not just the
  violating one.
- `edge-extend` MUST NOT distinguish a real hole from a digitization gap;
  both are left for the boundary-extension stage.

## Extracting boundaries

- Each polygon's exterior boundary MUST be its own boundary minus the
  combined boundary of every bounding-box-overlapping neighbor. A polygon
  with no such neighbor MUST keep its full boundary.

## Generating the boundary extension (points and Voronoi cells, retried together)

- `edge-extend` MUST generate points along each polygon's exterior boundary, at
  a target spacing, as input to a Voronoi diagram.
- The target spacing MUST default to the smaller of a fixed default or the
  file's own median real segment length.
- A single real segment MUST NOT contribute more than a fixed cap's worth
  of points.
- Generated points MUST exclude a buffered zone around every shared
  boundary endpoint.
- Points on each line MUST start at a stable per-line offset of up to one
  spacing.
- `edge-extend` MUST NOT generate points on x = ±180 or y = ±90.
- `edge-extend` MUST build a Voronoi diagram from the points, assign each cell
  to the polygon whose point generated it, and union cells by polygon into
  that polygon's extension.
- `edge-extend` MUST raise, without retrying, if any Voronoi cell holds more
  than one distinct point.
- On failure, or more points than a fixed maximum, `edge-extend` MUST retry
  with the spacing doubled from the resolved default, up to 10 times, then
  raise.

## Merging

- Each polygon's final geometry MUST be its original geometry combined
  with the portion of its own extension not already covered by a nearby
  original polygon.
- `edge-extend` MUST snap an extension to its nearby original neighbors, using
  a small fixed tolerance, before subtracting them.
- `edge-extend` MUST run one whole-layer coverage-clean pass afterward, using a
  gap-closing width equal to the snapping tolerance, not a shape-based
  heuristic.

## Outputs

- `edge-extend`'s final output MUST pass the coverage check (no
  overlap, no gap) before export.
- `edge-extend` MUST export the final merged layer.

## Configuration (`api.edge_extend.extend()` / CLI)

- `edge-extend` MUST process exactly one input file per call.
- The output path MUST default to the input path with an `_extended`
  suffix.
- `edge-extend` MUST raise `FileExistsError` if the output exists and
  overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `lines`, `attempt`, `merge`,
  `outputs`; any other value MUST raise `ValueError`.

## Examples

### Example 1: basic run, output name chosen automatically

    topo-tools edge-extend example.geojson

### Example 2: explicit output

    topo-tools edge-extend example.gpkg example_extended.gpkg

### Example 3: rerun and overwrite a previous output

    topo-tools edge-extend example.parquet example_extended.parquet --overwrite
