/---
title: "Schema"
sidebar:
  &#x20; order: 1
  &#x20; label: "Tutorial"
/---

Crosswalk a source file's columns onto a target admin-hierarchy schema
(`adm{n}_name`/`adm{n}_code` by default), copy missing ancestor columns
from a join layer, then fill the hierarchy down to each row's real
depth.

\## Map and rename columns

Propose a crosswalk and apply it in one call:

    topo-tools schema-map example.geojson

This writes the renamed output plus `example_crosswalk.csv`. Review and
hand-edit the crosswalk (retarget, blank a target to drop, move rows to
reorder columns), then re-apply it without re-running the mapping:

    topo-tools schema-map example.geojson --csv example_crosswalk.csv

Add `--map-only` to write just the crosswalk, for review before anything
is renamed.

\## Copy ancestor columns from a join layer

A layer missing an ancestor level's columns entirely gets them from the
join polygon it overlaps most. Chain levels coarsest-first:

    topo-tools schema-join admin2.parquet admin1.parquet admin2_join.parquet
    topo-tools schema-join admin3.parquet admin2_join.parquet admin3_join.parquet

\## Fill the hierarchy down

Run `schema-fill` against an already-clipped/stitched layer (an
`edge-match`/`edge-mosaic` output):

    topo-tools schema-fill admin3_join.parquet

This stamps a new `adm_lvl` column (rename it with `--depth-column`) and
fills every level's name/code columns down to that depth, per row, so a
genuine `NULL` at a row's own real depth stays `NULL` rather than being
backfilled from a shallower ancestor.

See the [`schema-map`](../../reference/1-schema/schema_map.md),
[`schema-join`](../../reference/1-schema/schema_join.md), and
[`schema-fill`](../../reference/1-schema/schema_fill.md) references for details.
