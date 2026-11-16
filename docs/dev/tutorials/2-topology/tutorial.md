---
title: "Topo"
sidebar:
  order: 1
  label: "Tutorial"
---

Detect gap/overlap coverage defects in a single polygon layer, review
them, then fix them.

## Detect defects

Inspect the layer first, without fixing anything:

    topo-tools topo-detect admin2.geojson

This writes `admin2_issues.geojson`, listing every gap/overlap with its
area, width, and (for gaps) a compactness score. Review it to judge which
gaps are real features (a strait, a lake) versus digitization slivers,
before picking a fix width.

## Fix defects

Fix gaps at or below the default snap tolerance, plus every overlap:

    topo-tools topo-clean admin2.geojson admin2_cleaned.geojson \
      --issues-file admin2_cleaned_issues.geojson

Widen the fill to every detected gap regardless of shape
(`--maximum-gap-width all`) or to a specific decimal-degree width. Sliver
gaps are merged either way. The issues file records each defect's actual fixed outcome, not just what was
detected.

See the [`topo-detect`](../../reference/2-topology/topo_detect.md) and
[`topo-clean`](../../reference/2-topology/topo_clean.md) references for the full
detection and fix rules.
