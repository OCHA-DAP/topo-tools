---
title: "schema-join"
sidebar:
  order: 9
  badge:
    text: Draft
    variant: caution
---

`schema-join` copies a join layer's admin-hierarchy columns onto every
input polygon it overlaps most, without touching geometry (e.g. an admin3 file
carrying only `adm3_code`/`adm3_name` gets `adm2_code`/`adm2_name`/
`adm1_code`/`adm1_name` from an admin2 file that carries its own
ancestors). Given an explicit `name_field`/`code_field` pair, it finds the
join layer's hierarchy columns through `schema-map`'s `TargetSchema` mechanism
(`docs/dev/explanation/1-schema/schema_map.md`); omitting both triggers structural
auto-detection on the join layer instead. Only the join layer is scanned for
hierarchy columns: the input layer's own columns are compared against them, never
detected.

## Why this exists

A normalized schema needs every ancestor code on every row, but a source
file often carries only its own level's code, sometimes with its ancestors'
names. `schema-fill` can't help, since it only cascades values already on
the row. `edge-match --merge --overlay-include` does copy overlay columns, but
it also extends and clips every input polygon to its overlay polygon, which is the wrong
tool for a layer whose geometry is already final. Truncating an input polygon's code
to derive its join polygon's only works when codes nest, and fails silently when
they don't. `schema-join` derives the join polygon from geometry instead, and
reports every case where that derivation is weak or disagrees with a value
the input polygon already has.

## Chaining levels

A join file that already carries its own ancestors fills every level in
one call. Otherwise, chain calls coarsest-first: admin2 against admin1,
then admin3 against the joined admin2 output, so each call's join layer already
has everything above it.

## Pipeline

Tables are named `{input}_schema_join_*`.

1. **`_01_inputs`**: loads the input layer via `core.assign.load_input()`
   (`{name}_input_01`, a fresh `fid` plus `source_file`) and the join layer via
   `core.assign.load_overlay()` (`{name}_overlay_01`), both reprojected to
   EPSG:4326.
2. **`_02_assign`**: calls `core.assign.assign_many()` unchanged, pairing
   each input polygon with the join polygon it shares the most area with
   (`{name}_02_assign`, `{name}_02_pairs`, `{name}_02_unassigned`), then
   builds `{name}_02_share`: each input polygon's own equal-area area and the
   share its assigned join polygon covers.
3. **`_03_join`**: resolves the join layer's hierarchy columns, then builds
   `{name}_03` by left-joining each input polygon to its assigned join polygon, adding
   each column absent from the input layer, skipping each one identical on every
   assigned input polygon, and adding the join polygon's values under a numbered sibling
   name for each one that differs (`{name}_03_mismatch` records the
   differing values). Geometry passes through unchanged. The input layer's
   columns keep their order, each added sibling follows its own column,
   and every other added column goes, in
   `core.admin_columns.canonical_order()`, into its own level's block (a
   name before the level's code), or before the first coarser level for a
   level the input lacks; rows are sorted by the deepest level's code.
4. **`_04_outputs`**: builds `{name}_04` (`no-overlap`, `low-overlap`,
   `value-mismatch` rows, in the shared issues-table column schema) and
   exports the joined layer and its issues file. Rounding to the output
   grid is the only geometry change, so it runs no topology check.

## Why per-polygon plurality, not per-file majority

`edge-match` and `edge-mosaic` default to `assign_one`, forcing a whole
input file onto one majority-vote overlay polygon, since each of their input files
normally sits inside a single overlay polygon. A `schema-join` input file is the
opposite case: an admin3 file spans every admin2 unit, so each input polygon needs
its own join polygon, which is exactly `assign_many`'s per-polygon plurality
(`docs/dev/explanation/3-edge/assign.md`).

## Why a low-overlap input polygon is flagged, not rejected

When an input polygon's best join polygon covers less than `min_overlap` (default `0.5`)
of its area, no single join polygon holds a majority of it, which usually means
the two layers' boundaries were digitized differently or the input polygon
genuinely straddles a join boundary. The plurality pick is still the most
likely join polygon, so it is kept; the `low-overlap` row, with the uncovered
area in `area_m2`, marks it for review rather than blocking the run.

## Why a conflicting value is kept side by side

An input polygon's own value and its join polygon's are both source data, and neither is
reliably correct (e.g. a name spelled with an accent in one layer and
without it in the other). `schema-join` keeps the input layer's column untouched,
adds the join polygon's values as the next free numbered sibling (`adm2_name1`),
and writes a `value-mismatch` row per differing input polygon, leaving the choice
to a later review step (see `docs/adr/0109`).
