---
title: "edge-mosaic"
description: "Fit an already-extended input layer into a new overlay layer."
sidebar:
  order: 7
---

Fit an already-extended input layer into a new overlay layer.

## Synopsis

```text
topo-tools edge-mosaic [OPTIONS] INPUT_FILE OVERLAY_FILE [OUTPUT_FILE]
```

## Description

Like edge-match, but skips the extending step, for input already run through edge-extend. OUTPUT_FILE defaults to INPUT_FILE with a "_mosaicked" suffix. It's required when INPUT_FILE is a pattern matching more than one file, or when `--input` is given.

## Options

- `--input TEXT`: Another input file to process together with INPUT_FILE. Repeat it or separate files with commas.
- `--original TEXT`: The original layer before extension. It decides whether a piece cut off by clipping is merged into a neighbor. Without it, such pieces are only reported. Repeat it or separate files with commas.
- `--issues-file TEXT`: Path for the issues report. Defaults to OUTPUT_FILE with an "_issues" suffix.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|assign|clip|outputs]`: Run only this step of the tool, for debugging.
- `--match-column TEXT`: Column in both layers, such as a p-code, used to match input polygons to overlay polygons. It wins over overlap where the two disagree. Can't be combined with `--overlay-match-column` or `--input-match-column`.
- `--overlay-match-column TEXT`: Matching column in the overlay, when its name differs from the input's. Give it with `--input-match-column`.
- `--input-match-column TEXT`: Matching column in the input, when its name differs from the overlay's. Give it with `--overlay-match-column`.
- `--merge`: Copy the overlay's columns onto every matched input polygon, and keep unmatched overlay or input polygons in the output, unclipped, instead of dropping them. Choose columns with `--overlay-include`, `--overlay-exclude`, `--input-include` and `--input-exclude`. When both layers have a column with the same name, choose which one to keep with `--prefer`.
- `--overlay-include TEXT`: Overlay columns to copy, comma-separated. Needs `--merge`.
- `--overlay-exclude TEXT`: Overlay columns not to copy, comma-separated. Needs `--merge`.
- `--input-include TEXT`: Input columns to keep, comma-separated. Needs `--merge`.
- `--input-exclude TEXT`: Input columns to drop, comma-separated. Needs `--merge`.
- `--prefer [overlay|input]`: When the overlay and input both have a column with the same name, keep this layer's column. Needs `--merge`. Can't be combined with `--overlay-include`, `--overlay-exclude`, `--input-include` or `--input-exclude`.
- `--fill-schema`: Before writing the output, fill each row's empty finer admin columns from its coarser ones and add a column with the row's own admin level. Set the columns with `--name-field` and `--code-field`, and the level column's name with `--depth-column`.
- `--name-field TEXT`: Name column of each level, with {n} for the level number, e.g. 'adm{n}_name'. Needs `--fill-schema` and `--code-field`. Without both, levels are detected from the data.
- `--code-field TEXT`: Code column of each level, with {n} for the level number, e.g. 'adm{n}_code'. Needs `--fill-schema` and `--name-field`. Without both, levels are detected from the data.
- `--depth-column TEXT`: Name of the added column holding each row's own admin level. Needs `--fill-schema`. [default: adm_lvl]

## Examples

Clip an extended admin3 layer to a new admin0 boundary:

```sh
topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson
```

Clip every country's extended layer to a world admin0 layer:

```sh
topo-tools edge-mosaic "*/latest/adm2/extended.parquet" world_adm0.geojson \
  out.parquet
```

List files instead of a pattern (repeat `--input` or use commas):

```sh
topo-tools edge-mosaic afg.parquet world_adm0.geojson out.parquet \
  --input ago.parquet,are.parquet
```

Match on a shared p-code column, overriding overlap where they disagree:

```sh
topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson --match-column pcode
```

Keep an overlay polygon's own shape where no input file covers it:

```sh
topo-tools edge-mosaic "*/latest/adm4/extended.parquet" world_adm0.geojson \
  out.parquet --merge
```

Keep the overlay's column when both layers have one with the same name:

```sh
topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson \
  --merge --prefer overlay
```

Re-clip an extended layer to a new overlay, with an issues report:

```sh
topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson \
  adm3_mosaicked.parquet --issues-file mosaic_report.parquet
```
