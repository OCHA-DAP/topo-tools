---
title: "change"
description: "Compare two versions of a polygon layer and list what changed."
sidebar:
  order: 6
---

Compare two versions of a polygon layer and list what changed.

## Synopsis

```text
topo-tools change [OPTIONS] OLD_FILE NEW_FILE [OUTPUT_FILE]
```

## Description

Each unit is classed as unchanged, renamed, modified, relocated, split, merged, complex, created or removed. OLD_FILE is the previous version and NEW_FILE the new one. OUTPUT_FILE, the changelog table (CSV or Parquet), defaults to both file names combined with a "_changelog" suffix. A map layer colored by type of change is written next to it.

## Options

- `--overlay-file TEXT`: Path for the map layer of changes. Defaults to OUTPUT_FILE with an "_overlay" suffix.
- `--tau-match FLOAT`: Smallest share of a unit's area that must overlap another unit for the two to count as related. [default: 0.8]
- `--tau-same FLOAT`: Smallest overlap, as shared area divided by combined area, for a matched pair to count as the same shape rather than modified. [default: 0.98]
- `--link-by-code`: Also match units that have the same code in both versions, when that code is unique.
- `--link-by-name`: Also match units that have the same name in both versions, when that name is unique.
- `--link-mode [either|both]`: With both `--link-by-code` and `--link-by-name`, match on either one or only on both. [default: either]
- `--name-field-a TEXT`: Name column of each level in OLD_FILE, with {n} for the level number, e.g. 'adm{n}_name'. Give it with `--code-field-a`. Without both, levels are detected from the data.
- `--code-field-a TEXT`: Code column of each level in OLD_FILE, with {n} for the level number, e.g. 'adm{n}_pcode'. Give it with `--name-field-a`. Without both, levels are detected from the data.
- `--name-field-b TEXT`: Name column of each level in NEW_FILE, with {n} for the level number. Give it with `--code-field-b`. Without both, levels are detected from the data.
- `--code-field-b TEXT`: Code column of each level in NEW_FILE, with {n} for the level number. Give it with `--name-field-b`. Without both, levels are detected from the data.
- `--code-column-a TEXT`: Code column in OLD_FILE. Defaults to the finest level's code column.
- `--code-column-b TEXT`: Code column in NEW_FILE. Defaults to the finest level's code column.
- `--name-column-a TEXT`: Name column in OLD_FILE. Defaults to the finest level's name column.
- `--name-column-b TEXT`: Name column in NEW_FILE. Defaults to the finest level's name column.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|overlap|classify|outputs]`: Run only this step of the tool, for debugging.

## Examples

Basic run, matching units by overlap only:

```sh
topo-tools change admin2_2020.geojson admin2_2024.geojson
```

Also match units that keep the same p-code:

```sh
topo-tools change old.gpkg new.gpkg --link-by-code
```

Accept less overlap, for heavily redrawn boundaries:

```sh
topo-tools change old.parquet new.parquet --tau-match 0.6
```

Choose the changelog and the map layer of changes, for review:

```sh
topo-tools change old.gpkg new.gpkg changelog.csv --overlay-file overlay.gpkg
```
