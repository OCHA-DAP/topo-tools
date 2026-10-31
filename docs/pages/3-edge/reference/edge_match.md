---
title: "edge-match"
description: "Fit an input layer into the polygons of a coarser overlay layer."
sidebar:
  order: 6
---

Fit an input layer into the polygons of a coarser overlay layer.

## Synopsis

```text
topo-tools edge-match [OPTIONS] INPUT_FILE OVERLAY_FILE [OUTPUT_FILE]
```

## Description

Each input file (or, with `--assign` many, each input polygon) is matched to the overlay polygon it overlaps most, extended to fill gaps, clipped to that polygon, with shared border points so the edges line up. OUTPUT_FILE defaults to INPUT_FILE with a "_matched" suffix. It's required when INPUT_FILE is a pattern matching more than one file, or when `--input` is given.

## Options

- `--input TEXT`: Another input file to process together with INPUT_FILE. Repeat it or separate files with commas.
- `--issues-file TEXT`: Path for the issues report. Defaults to OUTPUT_FILE with an "_issues" suffix.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|assign|groups|clip|outputs]`: Run only this step of the tool, for debugging.
- `--match-column TEXT`: Column in both layers, such as a p-code, used to match input polygons to overlay polygons. It wins over overlap where the two disagree. Can't be combined with `--overlay-match-column` or `--input-match-column`.
- `--overlay-match-column TEXT`: Matching column in the overlay, when its name differs from the input's. Give it with `--input-match-column`.
- `--input-match-column TEXT`: Matching column in the input, when its name differs from the overlay's. Give it with `--overlay-match-column`.
- `--merge`: Copy the overlay's columns onto every matched input polygon, and keep unmatched overlay or input polygons in the output, unclipped, instead of dropping them. Choose columns with `--overlay-include`, `--overlay-exclude`, `--input-include` and `--input-exclude`. When both layers have a column with the same name, choose which one to keep with `--prefer`.
- `--overlay-include TEXT`: Overlay columns to copy, comma-separated. Needs `--merge`.
- `--overlay-exclude TEXT`: Overlay columns not to copy, comma-separated. Needs `--merge`.
- `--input-include TEXT`: Input columns to keep, comma-separated. Needs `--merge`.
- `--input-exclude TEXT`: Input columns to drop, comma-separated. Needs `--merge`.
- `--prefer [overlay|input]`: When the overlay and input both have a column with the same name, keep this layer's column. Needs `--merge`. Can't be combined with `--overlay-include`, `--overlay-exclude`, `--input-include` or `--input-exclude`.
- `--assign [auto|one|many]`: How input polygons are assigned to overlay polygons. one: the whole input file goes to the overlay polygon most of its polygons overlap. many: each input polygon goes to the overlay polygon it overlaps most, for an input file spanning many overlay polygons, e.g. an admin4 layer fitted into many admin3 units. auto: one, switching to many when fewer than half the input polygons overlap the winner. The mode used is always logged. many only works with a single input file. [default: auto]
- `--fill-schema`: Before writing the output, fill each row's empty finer admin columns from its coarser ones and add a column with the row's own admin level. Set the columns with `--name-field` and `--code-field`, and the level column's name with `--depth-column`.
- `--name-field TEXT`: Name column of each level, with {n} for the level number, e.g. 'adm{n}_name'. Needs `--fill-schema` and `--code-field`. Without both, levels are detected from the data.
- `--code-field TEXT`: Code column of each level, with {n} for the level number, e.g. 'adm{n}_code'. Needs `--fill-schema` and `--name-field`. Without both, levels are detected from the data.
- `--depth-column TEXT`: Name of the added column holding each row's own admin level. Needs `--fill-schema`. [default: adm_lvl]

## Examples

Fit an admin4 layer into a single country boundary:

```sh
topo-tools edge-match adm4.geojson adm0.geojson
```

Fit admin3 into admin2, each group of units into its own admin2 unit:

```sh
topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg
```

Fit several countries' admin1 layers into one shared overlay together:

```sh
topo-tools edge-match sen_adm1.parquet world_adm0.geojson out.parquet \
  --input gmb_adm1.parquet,gnb_adm1.parquet
```

Match on a shared p-code column, overriding overlap where they disagree:

```sh
topo-tools edge-match adm3.gpkg adm2.gpkg --match-column pcode
```

Copy only iso_3 and adm0_name onto every matched input polygon:

```sh
topo-tools edge-match adm3.gpkg adm2.gpkg \
  --merge --overlay-include iso_3,adm0_name
```

Keep the overlay's column when both layers have one with the same name:

```sh
topo-tools edge-match adm3.gpkg adm2.gpkg --merge --prefer overlay
```

An admin4 layer whose units fall in many different admin3 units:

```sh
topo-tools edge-match adm4.gpkg adm3.gpkg --assign many
```

Choose the output and the issues report:

```sh
topo-tools edge-match adm3.gpkg adm0.gpkg adm3_matched.gpkg \
  --issues-file match_report.gpkg
```

Also fill empty finer admin columns from coarser ones before writing:

```sh
topo-tools edge-match adm3.gpkg adm0.gpkg adm3_matched.gpkg --fill-schema
```
