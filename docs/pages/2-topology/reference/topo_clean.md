---
title: "topo-clean"
description: "Find and fix gaps and overlaps between the polygons of one layer."
sidebar:
  order: 4
---

Find and fix gaps and overlaps between the polygons of one layer.

## Synopsis

```text
topo-tools topo-clean [OPTIONS] INPUT_FILE [OUTPUT_FILE]
```

## Description

The fixes made are listed in an issues report for review. OUTPUT_FILE defaults to INPUT_FILE with a "_cleaned" suffix.

## Options

- `--issues-file TEXT`: Path for the issues report. Defaults to OUTPUT_FILE with an "_issues" suffix.
- `--maximum-gap-width TEXT`: Which gaps to fill: 'all' for every gap found, or a width in decimal degrees (not meters). By default only tiny gaps from rounding errors and sliver gaps (much narrower than the edges drawn around them) are filled.
- `--snapping-distance TEXT`: Distance in decimal degrees within which nearby vertices are joined (default: 0.00000001). Only change it if cleaning fails on nearly touching edges.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|issues|clean|outputs]`: Run only this step of the tool, for debugging.

## Examples

Basic run: fills tiny gaps from rounding errors and sliver gaps:

```sh
topo-tools topo-clean example.geojson
```

Fill every detected gap, not just slivers:

```sh
topo-tools topo-clean example.gpkg --maximum-gap-width all
```

Fill gaps up to about 0.0001 degrees wide (about 11 m at the equator):

```sh
topo-tools topo-clean example.parquet --maximum-gap-width 0.0001
```

Choose the output and the issues report, which lists how each problem was fixed:

```sh
topo-tools topo-clean admin2.geojson admin2_cleaned.geojson \
  --issues-file admin2_cleaned_issues.geojson
```
