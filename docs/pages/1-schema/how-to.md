---
title: "Schema"
sidebar:
  order: 2
  label: "How-to"
---

Run `schema-map` on each supplied level below admin0:

    topo-tools schema-map admin2.parquet --map-only

This writes `admin2_crosswalk.csv` without changing the input. `schema-map` matches
columns by their values, not their names, so check every row:

    source_column, target_column, unique_count, note
    NAME_2,        adm2_name,     8,
    PCODE_2,       adm2_code,     8,
    NAME_1,        adm1_name,     2,
    PCODE_1,       adm1_code,     2,

`schema-map` maps every column whose values fit a level, so check each
numbered sibling (`adm2_code1`, `adm2_name2`). It can be a translation or
alternate spelling, a reference number, or a code or name from another
source or an older version, and a code column can get a name target.
Decide whether the release keeps each one: blank out its `target_column`
to drop it, or correct its target to the next free
`adm2_code1`/`adm2_name1`. A per-level extra such as a unit type takes the
level's own prefix (`adm2_type`). Output columns follow the crosswalk's row
order, so move rows to rearrange them. Then apply it:

    topo-tools schema-map admin2.parquet admin2_mapped.parquet --csv admin2_crosswalk.csv

The output keeps only the columns with a `target_column`, plus `geometry`.
A column with one value in every row, such as the country's name and code,
stays blank and is dropped.

With more than one level below admin0, copy each parent's codes and names
onto its children, coarsest first, overwriting each child in place:

    topo-tools schema-join admin2_mapped.parquet admin1_mapped.parquet admin2_mapped.parquet

If any child's value differs from its parent's, `schema-join` keeps the
child's column and adds the parent's as the next free sibling
(`adm2_name1`), filled on every row. Its issues file lists:

- `value-mismatch`: a differing name or code, settled in
  [review names](../5-names/index.mdx);
- `no-overlap`, `low-overlap`: a unit outside or mostly outside its
  parent, settled with the data provider before coding.

Then check the finest level's schema and nesting:

    topo-tools schema-detect admin2_mapped.parquet

This writes `admin2_mapped_schema_issues.parquet` without changing the input.
Errors (`levels-undetected`, `level-skipped`, `multiple-parents`,
`orphan-child`) mean the hierarchy can't be trusted yet, and
`varying-column` marks a column with several values within one unit,
which packaging would drop, and `unusable-column-name` a column family
packaging can't name once its level number is removed: fix them before
coding. Warnings list columns named or present unlike the
other levels'
(`column-naming`, `column-set-mismatch`), and columns set aside as a
coarser grouping (`supplemental-column`). If such a grouping is a real
admin level, run it again with `--name-field`/`--code-field`.
