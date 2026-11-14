---
title: "schema-detect"
sidebar:
  order: 3
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `schema-detect` MUST read the one input and reproject it to EPSG:4326 via
  `core.io.read_and_reproject()`, columns untouched. It takes one admin
  layer, hierarchy embedded as columns.

## Level resolution

- With an explicit `name_field`/`code_field` pair (given together or not at
  all), a level MUST exist for every number `n` with a column matching
  `code_field`, compared ignoring case and any character other than a
  letter or digit; an exact match wins as the level's code column. Every
  column starting with the level's own prefix and number (`adm2`), or ending
  with its number and suffix (`2_pcode`), MUST belong to that level, in the
  same comparison.
- Without the pair, levels MUST come from structural detection
  (`core.schema_map.detect_level_columns()`). When every detected level's
  code column names its level by the same number in one shared naming
  (`adm{n}_pcode`), level membership MUST then follow that naming, as with
  an explicit pair. Otherwise each level MUST hold its detected identity
  columns plus any column matching its naming anchor ignoring case and
  separators, and a whole-table-constant root level
  (`core.schema_map.detect_root_level()`) MUST be added one level above the
  coarsest.
- A level's code column MUST be the one in the naming family most levels
  use for their code.
- `levels-undetected` MUST also be reported, with the same reason, when
  `core.schema_map.resolve_levels()` (the resolution `code-detect` and
  `name-detect` use) raises for the same `name_field`/`code_field`.
- Unresolvable levels MUST be reported as rows, never raised.

## Checks

- `schema-detect` MUST report each of these kinds:

  | kind | severity | finding |
  | --- | --- | --- |
  | `levels-undetected` | error | no admin level can be resolved, or `code-detect` and `name-detect` can't resolve them |
  | `level-skipped` | error | a level number between the coarsest and finest found has no columns |
  | `multiple-parents` | error | one code under more than one code of the next coarser level |
  | `orphan-child` | error | a code whose next coarser level's code is blank |
  | `varying-column` | error | a non-numeric column outside the level columns with more than one value within one unit of the coarsest level |
  | `unusable-column-name` | error | a level column family packaging would give an empty, reserved or duplicate output name, e.g. bare `adm1`/`adm2` columns, or `adm1_name` beside `adm1name` |
  | `supplemental-column` | warn | a column structural detection sets aside as a coarser grouping, not a level |
  | `column-naming` | warn | a level column named unlike its match at other levels, by case, separators or the level's naming anchor |
  | `column-set-mismatch` | warn | a level lacking a column family found at two or more other levels |

- A column family MUST be a level column's name minus the level's own
  prefix and number (or number and suffix), compared ignoring case and
  separators. A family found at one level only (a reference name, a unit
  type) MUST NOT be reported.
- For `column-naming`, the expected spelling of a family MUST be the one
  most levels use, ties broken by the separator and case style most of the
  layer's level columns share.
- Nesting MUST be checked between each level and the next coarser one
  found, row by row in the input.
- If one check fails, `schema-detect` MUST still report the others, and MUST
  report the failure as a `check-failed` row (severity `error`) whose
  `reason` names the check and its error.

## Outputs

- `schema-detect` MUST always write the issues report, even with zero
  rows. It never modifies the input.
- Columns: `key`, `kind`, `severity`, `level`, `column`, `code`, `reason`.
  `column` is null for a level-wide row, `code` for any row not about one
  unit. A `.csv` report MUST be UTF-8 with a BOM.

## Configuration (`api.schema_detect.detect()` / CLI)

- `schema-detect` MUST process exactly one input file per call.
- `issues_path` MUST default to the input path with a `_schema_issues` stem
  suffix and a `.parquet` extension, and MUST end in `.csv` or `.parquet`,
  raising `ValueError` otherwise.
- `schema-detect` MUST raise `FileExistsError` if `issues_path` already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `levels`, `checks`, `outputs`;
  any other value MUST raise `ValueError`.

## Examples

### Example 1: basic run, report named automatically

    topo-tools schema-detect admin3.parquet

### Example 2: explicit level columns

    topo-tools schema-detect admin3.parquet admin3_schema_issues.csv \
      --name-field adm{n}_name --code-field adm{n}_pcode
