---
title: "Edge"
sidebar:
  order: 1
  label: "Tutorial"
---

Fit a finer input layer (e.g. admin3) into a coarser overlay layer
(e.g. admin0), with no gap or overhang at the edge, then re-fit it when
the overlay layer changes.

## Fit a raw layer to an overlay layer

`edge-match` extends each input polygon outward with Voronoi diagrams, then clips
it to its overlay polygon, in one call:

    topo-tools edge-match adm3.gpkg adm0.gpkg adm3_matched.gpkg \
      --issues-file match_report.gpkg

Add `--fill-schema` to cascade admin-hierarchy columns down to each row's
real depth before export.

## Re-fit to a new overlay layer

When only the overlay layer changes, `edge-mosaic` re-clips an
already-fitted layer against it, skipping Voronoi extension entirely:

    topo-tools edge-mosaic adm3_matched.gpkg adm0_new.gpkg adm3_mosaicked.gpkg \
      --issues-file mosaic_report.gpkg

## The primitives underneath

`edge-match` runs `edge-extend` underneath, which is also runnable on its
own to extend a layer outward with no overlay to fit.

See the [`edge-match`](../../reference/3-edge/edge_match.md),
[`edge-mosaic`](../../reference/3-edge/edge_mosaic.md),
and [`edge-extend`](../../reference/3-edge/edge_extend.md) references for
details.
