---
title: "Topology"
sidebar:
  order: 2
  label: "How-to"
---

Run `topo-detect` first to list the dataset's gaps and overlaps:

    topo-tools topo-detect admin2_mapped.parquet admin2_detect_issues.parquet

Load the result as a map layer and check whether any of the large gaps
are lakes or other water bodies that the source leaves outside every
unit. That decides the gap setting:

| Large gaps | Setting | Result |
| --- | --- | --- |
| None are water bodies | `--maximum-gap-width all` | Every gap is filled by a neighboring unit |
| Some are water bodies | no flag | Slivers are filled, wider gaps stay open |

    topo-tools topo-clean admin2_mapped.parquet admin2_topo.parquet \
      --issues-file admin2_topo_issues.parquet --maximum-gap-width all

Load the issues file as a map layer, not just a table, to see exactly
which polygons got fixed (`kind='gap'`, `kind='sliver-gap'` or `kind='overlap'`, `fixed=true`).
With no flag, a gap left open that isn't a water body shows as
`fixed=false`: send it to review or the data provider.

## Fitting a finer level into one parent unit (uncommon)

Skip this unless a source authority also provides a level finer than the
base level, covering only part of it. Isolate the target parent unit
first, then fit the finer layer to it in one pass (match by overlap,
extend to fill any gap, clip to the parent). No codes exist yet at this
stage, so filter by name rather than p-code:

    duckdb -c "INSTALL spatial; LOAD spatial;
      COPY (SELECT * FROM 'admin2_topo.parquet' WHERE adm2_name = 'your_district')
      TO 'parent.parquet';"

    topo-tools edge-match finer_level_raw.parquet parent.parquet \
      finer_matched.parquet --issues-file finer_matched_issues.parquet

Check the issues file (if written) for anything that didn't match or
clip cleanly.
