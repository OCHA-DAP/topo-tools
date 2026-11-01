---
title: "Edge"
sidebar:
  order: 8
  label: "Overview"
  badge:
    text: Draft
    variant: caution
---

Why edge-match and edge-mosaic are both built from the same primitives
(edge-extend, assign, and the clip in `core.clip`), and when to reach
for edge-extend directly instead.

- [edge-extend](edge_extend.md): Voronoi extension, the standalone
  primitive.
- [edge-match](edge_match.md): extend and clip a raw finer layer into a
  coarser overlay polygon.
- [edge-mosaic](edge_mosaic.md): re-clip an already-extended layer into a
  new overlay polygon, skipping extension.
