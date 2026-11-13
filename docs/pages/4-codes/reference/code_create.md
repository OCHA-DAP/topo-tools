---
title: "code-create"
description: "Give every unit a new hierarchical code, numbered within its parent."
sidebar:
  order: 4
---

Give every unit a new hierarchical code, numbered within its parent.

## Synopsis

```text
topo-tools code-create [OPTIONS] INPUT_FILE [OUTPUT_FILE] [ISSUES_FILE]
```

## Description

Use it when there is no previous release to keep codes from, or with `--source-codes` embed to keep the source codes inside the new codes. The new codes replace each level's code column. OUTPUT_FILE defaults to INPUT_FILE with a "_coded" suffix. ISSUES_FILE lists parents with more children than the code width allows. It defaults to OUTPUT_FILE with an "_issues" suffix and is only written when there are any.

## Options

- `--root-code TEXT`: Code at the start of every code, e.g. a country code. With `--code-field`, also written to level 0's code column. Give this or `--root-code-field`.
- `--root-code-field TEXT`: Column holding each row's root code, for a file of several countries. Each value's units are numbered on their own, as if code-create ran once per value. Give this or `--root-code`.
- `--delimiter TEXT`: One character between the parts of a code, or '' for none. [required]
- `--min-width TEXT`: How many digits each level's number is padded to with zeros: one width for all levels (3), one per level from coarsest (2,2,4), or auto for as many as each level needs. [required]
- `--source-codes [replace|embed|copy]`: What to do with each level's existing code: replace it with a new number, embed it as that level's part of the new code (with no delimiter, whole-number codes are zero-padded to `--min-width` or the widest code), or copy it to a new column (adm1_code to adm1_code1) and then replace it. [default: replace]
- `--name-field TEXT`: Name column of each level, with {n} for the level number, e.g. 'adm{n}_name'. Give it with `--code-field`. Without both, levels are detected from the data.
- `--code-field TEXT`: Code column of each level, with {n} for the level number, e.g. 'adm{n}_code'. Give it with `--name-field`. Without both, levels are detected from the data.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|levels|assign|outputs]`: Run only this step of the tool, for debugging.

## Examples

Basic run, levels detected from the data:

```sh
topo-tools code-create admin2.geojson --root-code AFG --delimiter . \
  --min-width 3
```

Name the level columns, when detection is unsure:

```sh
topo-tools code-create admin2.geojson --root-code AFG --delimiter . \
  --min-width 3 --code-field adm{n}_code --name-field adm{n}_name
```

Keep existing codes inside the new code, no delimiter (AF01, AF0101, ...):

```sh
topo-tools code-create admin2.geojson --root-code AF --delimiter '' \
  --min-width 2 --source-codes embed --code-field adm{n}_code \
  --name-field adm{n}_name
```

Several countries in one file, each numbered under its own root:

```sh
topo-tools code-create world_admin2.parquet --root-code-field iso3 \
  --delimiter . --min-width 3
```
