---
title: "schema-detect"
sidebar:
  order: 7
  badge:
    text: Draft
    variant: caution
---

`schema-detect` checks that one admin layer's columns form a consistent
hierarchy, without changing it. Every check is DuckDB SQL over a table of
level columns, so the same queries run in DuckDB-WASM.

## Stages

| Stage | Table | What it does |
| --- | --- | --- |
| `_01_inputs` | `{name}_01` | reads and reprojects the input |
| `_02_levels` | `{name}_02` | one row per level column (level, family, spelling, code or not), plus one row per level-wide problem |
| `_03_checks` | `{name}_03` | one SQL query per kind (`{name}_03_tmp{n}`), unioned |
| `_04_outputs` | `{name}_04` | keys and severity; writes CSV or Parquet |

## Level membership by naming

Structural detection (`detect_level_columns()`) groups columns by how
their values nest, which is right for finding levels but can fold one
level's columns into the next when a level's codes don't embed its
parent's and nothing else places it in the chain. A layer then reads as
skipping that level. So once the detected code columns share one naming
with the level number in it (`adm{n}_pcode`), `schema-detect` takes that
naming as the template and assigns every column to a level by name, the
same path an explicit `--code-field` takes. Structural membership remains
the fallback for namings without a level number (`state_code`,
`county_code`).

Names are compared ignoring case and separators, so `ADM2_PCODE` still
counts as level 2's code column and `adm1name1` as level 1's `name1`: both
are reported as `column-naming` instead of missing.

## Families and the column set

A column's family is its name without the level's prefix and number:
`adm2_name1` and `adm3_name1` are both `name1`. `column-set-mismatch`
reports a level missing a family that two or more other levels have. A
family at one level only is a level-specific attribute (`adm3_ref_name`,
`adm0_en`) and isn't reported, since in a two-level layer there is no way
to tell which level is the odd one out.

## Supplemental columns

Columns that `schema-map`'s matcher sets aside as a coarser grouping
(AFG's `regioncode`, CPV's `island`, VNM's `adm1_type_en`) are reported
as `supplemental-column` warnings. Such a grouping can be a missed
intermediate level whose codes are local to each parent, but
`code-detect` and `name-detect` only report, so `resolve_levels()` runs
them on the detected levels and the warning prompts the check. A missed
level costs at most extra `duplicate-name` warnings, names compared under
a parent one level too coarse. `resolve_levels()` refuses a layer with a
skipped level or no level that has both a code and a name, and
`schema-detect` reports that refusal as `levels-undetected`, the one place
a layer's level resolution is judged, so `package` can skip
`code-detect` and `name-detect` with that reason.

## Real-data results

Run structurally on every latest original admin1 to admin5 layer in the
portolan catalog (281 layers): 36 rows in 17 layers, none
`levels-undetected`. 26 are `supplemental-column` warnings in 14 layers:
real coarser groupings (AFG regions, AZE economic regions, BFA's previous
units, CPV islands, NGA senatorial districts, MMR admin5's `adm2_name1`)
and unit types or near-empty columns (VNM `adm1_type_en`, ETH
`adm2_alt_name`, SLE `adm4_ref`). The other 10 rows are all real:
two adm4 codes under two adm3 codes each in MMR admin4 (7 rows), BGR's
`adm0_bg`/`adm1_bg` with no `adm2_bg`, and ZAF's `adm1_id` to `adm3_id`
with no `adm0_id` or `adm4_id`.
