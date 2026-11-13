---
title: "Packaging"
sidebar:
  order: 2
  label: "How-to"
---

Run `package` on the name-cleaned output of [step 5](../5-names/how-to.md).

    topo-tools package admin3_names_edited.parquet --output "release/{x}.parquet" \
      --name-field "adm{n}_name" --code-field "adm{n}_code" \
      --output-code-field "adm{n}_pcode"

It first coverage-cleans the layer if it has gaps or overlaps, then runs
`schema-detect`, `topo-detect`, `code-detect` and `name-detect` on it,
writing each report plus a summary into `release/`. Any error stops the run
before packaging, with exit status 1. Open
`release/admin3_names_edited_validate_summary.csv` for one row per stage and
kind found; each row names the report with the details. Fix the errors and
rerun, or pass `--no-validate` to package anyway.

Earlier steps work with `adm{n}_code`; `--output-code-field` writes the
release's code columns as `adm{n}_pcode`.

Writes one `release/admin{n}.parquet` per detected level,
`release/points.parquet` (one label point per admin unit), and
`release/lines.parquet` (the deduplicated boundary network).

`topo-clean`, `edge-match`, and `code-create` each write an issues file
only when they find something to report; load one as a map layer, not
just a table, to see exactly which polygons it touched.
