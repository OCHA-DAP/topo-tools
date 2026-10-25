---
title: "assign"
sidebar:
  order: 9
  badge:
    text: Draft
    variant: caution
---

`assign-many` and `assign-one` are two internal crosswalk strategies in
`core/assign/`; neither is a standalone CLI/API tool. Both build a
`(input_fid, overlay_fid)` pairing from a bbox-prefiltered, part-exploded
overlap-area join; they differ only in how that pairing gets finalized
once per-pair shared area is known.

`assign-one` is `edge-mosaic`'s only mode and what `edge-match`'s default `auto` runs
unless fewer than half the input polygons overlap the winner:
every input polygon in one input file is forced onto **one** shared overlay polygon, chosen
by majority vote of that file's input polygons, once that file has any winner at
all. This is the safest choice for a large group of polygons resolving
against a single true overlay polygon, since a per-polygon rule could otherwise let a
handful of stragglers (a border-crossing overshoot on extended geometry, or
a genuinely non-overlapping outlying island on raw geometry) get pulled
onto the wrong neighbor or dropped outright, even though the rest of the
file clearly belongs together.

`assign-many` is `edge-match`'s `--assign many`, also picked by the default
`--assign auto` when fewer than half of a file's input polygons overlap the
`assign-one` winner: each input polygon
decides independently which overlay polygon it overlaps most, so one input file's
input polygons MAY scatter across **many** different overlay polygons. Use it only when
that's actually true of the input, e.g. a poorly-digitized admin4 layer
whose input polygons genuinely belong to many different admin3 overlay polygons. It is
never used for `edge-mosaic`/`edge-clip`, whose already-extended (overshoot)
geometry makes per-feature voting unsafe (see `docs/adr/0019`).

## Modules

`core/assign/` has no `api.*()`/CLI pipeline of its own; every function
below is called directly by another tool's own `api.*()` orchestrator
(`api.edge_mosaic`, `api.edge_clip`, `api.edge_match`), never from inside
`core.edge_mosaic`/`core.edge_match` themselves.

`core/assign/_inputs.py` loads the single overlay layer raw via
`core.io.read_and_reproject`, and the (possibly multi-file) input layer via
one combined query built from `core.io.reproject_select_sql()` per file
(`UNION ALL BY NAME`, no table-per-file materialization, see
`docs/adr/0044`); neither is coverage-checked nor -cleaned (consistent
with `edge-clip`/`edge-stitch`; these are all purely mechanical primitives). Every
input polygon row is tagged with a `source_file` column recording the exact path
it came from (basename alone can't distinguish same-named files across
directories).
`api.edge_mosaic` and standalone `api.edge_clip` (its multi-file loop, see
`docs/dev/explanation/3-edge/edge_clip.md`) both call its `load_input()`/
`load_overlay()` directly, since ADR-0023 split those apart; `api.edge_match`
loads its own input polygons via `core.edge_match._01_inputs` instead.

`_many.py` / `_one.py` hold the actual assignment logic (see below):
`api.edge_mosaic`, standalone `api.edge_clip`, and `api.edge_match` (`one`,
or `auto`) all call `core.assign.assign_one()`; `api.edge_match` calls
`core.assign.assign_many()` under `--assign many`, or when `auto` switches. All call
directly on their own already-loaded tables. `edge-mosaic` calls it once
per run; `edge-clip`'s multi-file loop calls it once per input file,
reusing a cached overlay-tile decomposition across every call (see below and
`docs/adr/0024`). Neither function runs a coverage hard gate: an unclipped
crosswalk is expected to still overlap/gap between neighboring input polygons,
that's `edge-clip`'s and `edge-stitch`'s job downstream.

## `assign_many`: largest-overlap assignment

Both layers are exploded into parts (`UNNEST(ST_Dump(geom))`) before
computing bbox candidates, a multi-part overlay polygon (a country with offshore
islands) would otherwise get one bbox spanning everything and defeat the
prefilter. Shared area per `(input, overlay)` fid pair is summed across
every part-pair (a multi-part input polygon can overlap a multi-part overlay polygon in
more than one place), ranked in an equal-area CRS (`EQUAL_AREA_CRS`)
rather than raw EPSG:4326 degree-area, which would bias plurality
assignment toward higher-latitude overlay polygons. Only the intersection geometry
is transformed, not the whole layer, to bound the cost.

```sql
ROW_NUMBER() OVER (PARTITION BY input_fid ORDER BY shared_area DESC, overlay_fid ASC)
```

picks the plurality overlay polygon per input polygon; ties break on the lowest overlay fid.
Input polygons with zero overlap with any overlay polygon are dropped with a logged
warning, not an error.

## `assign_one`: per-file majority-vote assignment

`assign_one` cannot use `assign_many`'s pairs join unmodified: it runs
against already-extended (often massively overshot) geometry, and a plain
`ST_Intersection`-based area-sum join against a huge single overlay part
(e.g. a country-scale admin0 polygon) is exactly the failure mode `edge-clip`
splits around. So `assign_one` builds its own pairs table (`_build_pairs`),
splitting any overlay part of at least `SUBDIVIDE_MIN_VERTICES` vertices with
`subdivide_sql` before intersecting, the same helper and budget `edge-clip`
uses.

That tiling is split into its own function, `prepare_overlay_tiles()`, which
takes an optional `input_bbox` (`xmin, ymin, xmax, ymax`) that skips any
overlay part whose bbox can't overlap it, so a handful of input polygons matched
against one much larger shared overlay polygon (e.g. a global admin0 file) doesn't
tile parts nothing will ever be assigned to (see `docs/adr/0085`). A caller
processing one input file per run (`edge-mosaic`, single-file
`edge-clip`) never notices: `assign_one()`'s default `use_cached_tiles=False`
calls it internally with the already-loaded input polygon's own bbox. `edge-mosaic`'s
multi-file loop instead pre-scans every input file's bbox, unions them,
and calls `prepare_overlay_tiles()` once before iterating with the combined
bbox, passing `use_cached_tiles=True` on every file's `assign_one()` call, so
the same overlay polygon's pieces aren't split from scratch on every one of
possibly hundreds of files. See `docs/adr/0024` for the profiling
discrepancy that motivated the caching split, and `docs/adr/0085` for the
bbox prefilter. Each file's loop iteration loads only the overlay polygons
whose bbox is near that file's input (`load_nearby_overlay`), which
`assign_one` then narrows to the polygons it pairs with.

For each `source_file`, `assign_one` counts how many of that file's input polygons
intersect each candidate overlay polygon (a count of intersecting input polygons, not
summed overlap area) and assigns every input polygon in the file to whichever
overlay polygon wins that count, since a single file's input polygons are always one
group. This guards against cross-country border overshoot misassigning a
file by per-feature area alone; see `docs/adr/0019-mosaic-per-file-majority-vote.md`
for why a per-polygon rule isn't enough and what it was measured to break.

Once a file has a winner, every input polygon in that file rides it unconditionally,
including an input polygon with literally zero overlap with the winning overlay polygon (e.g.
a tiny offshore island whose true overlay polygon it can only reach after
`edge-match`'s own post-assign Voronoi extension). Only a file whose
input polygons have **no** overlap with any overlay polygon at all lands in
`_02_unassigned`. A forced-in input polygon that still doesn't physically reach its
overlay polygon by clip time produces an empty `ST_Intersection` and is dropped
there instead, reported as a `kind='clip-empty'` issue row rather than an
`assign`-time one (see `docs/dev/explanation/3-edge/edge_clip.md`).

## Code-column join (optional)

Both `assign_many` and `assign_one` accept optional `overlay_match_column`/
`input_match_column` kwargs. `None` (the default) keeps `_02_assign`'s
schema and values exactly as before, `input_fid`/`overlay_fid` only, no
behavior change. When both are given, the existing spatial result above is
still always computed (it's needed as the cross-check), alongside an exact
code join restricted to `(input, overlay)` pairs that actually overlap (a
code match against a non-overlapping overlay polygon doesn't count). The code result
wins whenever one exists; `_02_assign` gains two more columns recording
which path won:

- `assignment_method`: `'code'` when a code match existed, `'spatial_fallback'`
  when it fell back to the spatial result.
- `spatial_agrees`: for `method='code'` rows, whether the spatial result
  agreed (`True`)/disagreed (`False`) with the code match; `NULL` for
  `spatial_fallback` rows.

`assign_many` computes this per input polygon (its usual per-polygon plurality feeds
the cross-check); `assign_one` computes it per source file (its usual
per-file majority vote feeds the cross-check, so every input polygon in a
code-mismatched or code-fallback file shares the same `assignment_method`).
Neither function derives issues rows itself; that's each calling
`api.*()`'s job (`kind='code-mismatch'`/`'code-fallback'`). See `docs/adr/0045` for why code wins on
disagreement instead of spatial, and why an unmatched code falls back
instead of dropping the input polygon/file.

## Carry-forward columns (optional)

Both `assign_many` and `assign_one` accept an optional `carry_columns` list
of overlay column names and an optional `input_columns` list of input column
names. `None` (the default, for both) leaves `_02_assign`'s schema
unchanged. When `carry_columns` is given, each name is projected from
`{name}_overlay_01` onto every matched input polygon row, joined on the
already-resolved `overlay_fid` (after any code-column join above has picked
a winner), so it costs one extra join regardless of which path assigned
the overlay polygon. Column names are always caller-specified, never inferred from
either layer's schema, matching this project's structural (not
name/value-based) matching philosophy elsewhere (see
`docs/dev/explanation/1-schema/schema_map.md`). A name colliding with `_02_assign`'s own
reserved columns (`input_fid`, `overlay_fid`, `assignment_method`,
`spatial_agrees`) raises `ValueError`; a name already present on the
input polygon's own resolved column list (`input_columns` when given, else the raw
`{name}_input_01` schema via `DESCRIBE`) also raises `ValueError`, not left
to the SQL layer to reject on its own (DuckDB silently renames/dedups a
duplicate `SELECT` column instead of erroring, see `docs/adr/0077`).
Checking against the resolved `input_columns` rather than the raw schema
means a column narrowed away by `--input-exclude` no longer false-positives
this collision check.

Both lists are resolved once per call by `core.assign.resolve_merge_columns()`
(itself built on `resolve_column_selection()`, and gated by
`validate_merge_flags()`'s mutual-exclusion/require-`merge` checks), shared
by `edge-mosaic`'s and `edge-match`'s api layers: `overlay_include`/
`overlay_exclude` narrow `carry_columns` (always dropping `fid`/`geom`),
`input_include`/`input_exclude` narrow `input_columns` (always keeping
`fid`/`geom`/`source_file`), and `prefer` (`"overlay"`/`"input"`) drops the
losing side's column wherever both lists name the same column, resolving a
collision automatically instead of raising.

Input polygons with no overlay polygon match (`_02_unassigned`) never gain the carried
columns. A caller that keeps such rows in its own output regardless (input
passthrough, or an unmatched overlay polygon kept via `core.assign.fill_unmatched_overlays()`;
see `docs/dev/explanation/3-edge/edge_mosaic.md`/`docs/dev/explanation/3-edge/edge_match.md`)
gets `NULL` for all of them automatically via that caller's own `UNION ALL
BY NAME`.

## Comparison

| | `assign-many` | `assign-one` |
| --- | --- | --- |
| Used by | `edge-match` (`many`, or `auto`'s switch) | `edge-mosaic`, `edge-clip`, `edge-match` (`one`, or `auto`) |
| Decision granularity | Per input polygon | Per source file |
| Zero-overlap input polygon in an otherwise-matched file | Dropped, logged, `_02_unassigned` | Forced onto the file's winner; dropped later at clip time if it still doesn't reach it |
| Vote signal | N/A (each input polygon stands alone) | Count of intersecting input polygons per file |
| Misassignment risk | None from overshoot (there is none) | A file too small to form a real majority (single-input file, tied vote) |

## Caveats

**`assign-one` runs against overshoot geometry (for `edge-mosaic`/`edge-clip`).**
It never sees an input polygon's pre-extension footprint, only the already-extended
geometry, so the bbox prefilter is less selective than `assign-many`'s, and
in principle the per-polygon overlap signal underlying the vote could be
skewed by an input polygon whose overshoot bulges further into a neighboring overlay polygon
than into its true one. The per-file majority vote mitigates this because a
file's other, unaffected input polygons still outvote a single misbehaving one by
count, but a single-input file has no other vote to correct it, and a tied
vote (e.g. a two-input file split one-and-one between two overlay polygons) falls
back to the lower overlay polygon id rather than any geometric signal.

**`assign-one` forces every input polygon onto its file's winner, even a
non-overlapping one.** This is intentional (see above), but it means
`edge-match` under `one` or `auto` doesn't drop a straggler at assign time; a
caller relying on `_02_unassigned` to catch every non-overlapping input polygon
must also check the `kind='clip-empty'` issue rows produced downstream.
