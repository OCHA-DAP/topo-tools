---
title: "schema-detect"
description: "Find problems in the column schema and hierarchy of one admin layer."
sidebar:
  order: 3
---

Find problems in the column schema and hierarchy of one admin layer.

## Synopsis

```text
topo-tools schema-detect [OPTIONS] INPUT_FILE [ISSUES_FILE]
```

## Description

Checks that admin levels can be detected with none skipped, that every level has the same set of columns named the same way, that each code sits under exactly one parent code, that no code has a blank parent, that no text column outside the levels has several values within one coarsest-level unit, and that packaging can give every level column family its own output name. Writes the problems found without changing the layer, even when there are none. ISSUES_FILE defaults to INPUT_FILE with a "_schema_issues" suffix, as Parquet, or CSV for a .csv name.

## Options

- `--name-field TEXT`: Name column of each level, with {n} for the level number, e.g. 'adm{n}_name'. Give it with `--code-field`. Without both, levels are detected from the data.
- `--code-field TEXT`: Code column of each level, with {n} for the level number, e.g. 'adm{n}_code'. Give it with `--name-field`. Without both, levels are detected from the data.
- `--overwrite BOOLEAN`: Replace output files that already exist. Pass `--overwrite=false` to stop with an error instead. [default: True]
- `--threads INTEGER`: Number of threads DuckDB uses (default: all CPU cores).
- `--debug`: Keep intermediate tables, export them to Parquet, and log the time and memory each query takes.
- `--tmp-dir TEXT`: Folder for the working DuckDB database and intermediate files (default: a new temporary folder, deleted afterwards unless `--debug` is set).
- `--step [inputs|levels|checks|outputs]`: Run only this step of the tool, for debugging.

## Examples

Basic run, report named automatically:

```sh
topo-tools schema-detect admin3.parquet
```

Explicit level columns:

```sh
topo-tools schema-detect admin3.parquet admin3_schema_issues.parquet \
  --name-field adm{n}_name --code-field adm{n}_pcode
```
