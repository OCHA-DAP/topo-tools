---
title: "edge-mosaic"
sidebar:
  order: 14
  badge:
    text: Draft
    variant: caution
---

`edge-mosaic` fits an **input** layer that is already the finished output of a
prior `edge_extend()` run into a new/different **overlay** layer, without
re-running Voronoi extension. It exists because `edge-match` redundantly redoes
extension for input polygons that were already extended by an earlier pipeline
run: `edge-match`'s own Colombia-scale profiling (see `docs/dev/explanation/3-edge/edge_match.md`)
shows the `groups` stage (where per-overlay-polygon extension happens) at ~85% of
total wall time. If the geometry is already extended, only assignment and clipping need to happen: the same two primitives
`assign` and `edge-clip` expose standalone (see their own explanation docs);
`edge-mosaic` is a thin wrapper chaining `assign-one` → `edge-clip`, with both
sides of every overlay border sharing border points, so no stitch is needed
(see `docs/adr/0144`).

## Pipeline

1. **`_01_inputs`**: loads both layers raw via `core.io.read_and_reproject`,
   once per input file plus once for the overlay polygon (`{name}_input_01`,
   `{name}_overlay_01`). Neither side is coverage-checked or -cleaned; see
   "Why neither input is coverage pre-checked" below.
2. **assign**: calls `core.assign.assign_one()` directly, the same
   per-file majority-vote pairing `assign-one` exposes standalone, see
   `docs/dev/explanation/3-edge/assign.md`. Also narrows `{name}_overlay_01` down to
   only the overlay fids any input polygon was actually assigned to, before clip
   runs.
3. **`_01_clip`**: a thin wrapper joining `overlay_fid` onto
   `{name}_input_01` from `{name}_02_assign`, then calling
   `core.edge_clip.main()`, the same per-`overlay_fid`-subprocess, split
   clip `edge-clip` exposes standalone, see `docs/dev/explanation/3-edge/edge_clip.md`.
   When `merge` is set (see "Overlay gap-fill and input passthrough" below),
   this stage also `UNION ALL BY NAME`s every whole unmatched input file's
   own already-extended geometry into `{name}_03` immediately after the
   normal clip; the api layer then calls the shared
   `core.assign.fill_unmatched_overlays()` to append every unmatched
   overlay polygon's own geometry into the same table, before the outputs stage.
4. **`_02_outputs`**: no coverage gate (see `docs/adr/0144`). A bounded
   invalid-edge check (`core.coverage.invalid_edge_issues`, passthrough rows
   excluded) and a gap check (`gap_issues_table`) add their rows to an
   issues report listing unassigned/passthrough input polygons (no dropped-group
   kind, since there are no groups), any gap-filled overlay polygons, plus any
   leftover gap wider than `SNAP_TOLERANCE` inside an overlay polygon the output
   was clipped to, a warning log if any such gap
   remains, and export (only when the issues report has rows).

## Multi-file input

Unlike every other tool here, the input role MAY span multiple files in one
call (the portolan catalog stores one `extended.parquet` per country, never
pre-combined); the overlay layer stays single-file. `output_path` MUST
be given explicitly whenever multiple paths are passed, since there's no
single filename to default one from, and `step` MUST be `None` (see
`docs/adr/0079`).

With more than one input path, `_mosaic_multi_file()` (`api/edge_mosaic.py`)
runs `inputs`+`assign`+`clip` in a per-file loop instead of combining every
file into one table up front, the same memory-safe pattern standalone
`edge-clip`'s own multi-file mode used before it was reverted to a strict
1:1 primitive (ADR-0023, ADR-0024, ADR-0080): the overlay layer is loaded once
into a pristine snapshot and its heavy-part tile decomposition cached
(`core.assign.prepare_overlay_tiles()`), then each input file is loaded,
assigned, and clipped alone against a fresh copy of that snapshot, one at a
time. Each file's clip result and its assign/drop/detached tables are
written to Parquet under `tmp_dir` and combined once after the loop
(`read_parquet(..., union_by_name = true)`), and each iteration's original
layer covers only the original files whose extent overlaps that input file,
keeping peak memory to roughly one overlay layer plus one input file at a
time, not one overlay layer plus every input file. `fid` is kept
globally unique across the whole run via a running offset applied right
after each file loads (not by the union-time `row_number()` a
single-file/combined-table run uses), then reassigned fresh via
`ROW_NUMBER()` over the fully accumulated result once the loop ends,
preserving the same "fid is renumbered fresh after the union" contract a
combined-table run also guarantees. Shared border points are picked once, from every file's candidates, before any
file is clipped, and `outputs` runs once over the fully accumulated result, so
files that meet at a border share vertices and are checked together.
Every row on the internal `{name}_03` table still carries a `source_file`
column tagging its origin file, whichever assembly path produced it; it's
an `assign-one` working column, stripped before the exported output (see
`docs/adr/0087`).

## Overlay gap-fill and input passthrough

Both opt-in via the single boolean `merge` flag (CLI: `--merge`), off by
default; `--overlay-include`/`--overlay-exclude`/`--input-include`/
`--input-exclude`/`--prefer` further narrow which columns survive (see
`docs/dev/explanation/3-edge/assign.md`).

**Input passthrough.** A whole input file with no overlap with any overlay polygon
(a country genuinely missing from the overlay layer) is dropped by
default; with `merge` set, that file's own already-extended geometry is
kept in the output unclipped instead, reported as a `kind='passthrough'`
issues row rather than `unassigned`. Scope is whole-file only: an
individual input polygon dropped from an otherwise-matched file (`core.assign`'s
per-file majority vote already decided it doesn't belong there) is
unaffected either way. `_01_clip.py`'s `main()` builds this directly as a
`UNION ALL BY NAME` selecting every input polygon row whose fid is in
`{name}_02_unassigned`, immediately after the normal clip.

**Overlay gap-fill.** An overlay polygon matched by zero input polygons is dropped by
default; with `merge` set, that overlay polygon's own geometry and carried columns
are kept in the output unclipped instead, reported as a `kind='gap-fill'`
row. This is the shared `core.assign.fill_unmatched_overlays()` helper
(relocated from a mosaic-local `fill_gaps()` this session so `edge-match`
can call it too, see `docs/adr/0088`), called from the api layer right
after `_01_clip.main()` returns, against a `{name}_overlay_full` snapshot
taken before assign narrows `{name}_overlay_01` to only-matched fids.

Both mechanisms are identical in outcome to `edge-match`'s own `merge`,
given an equivalent already-extended/raw input polygon set against the same
overlay polygon (see `docs/dev/explanation/3-edge/edge_match.md`). `edge-clip` has no
equivalent option (it stays a strict 1:1 primitive with no drop/keep
decision to make). `merge` always couples attribute carry-forward with
both passthrough mechanisms; there is no way to get one without the
other (see `docs/adr/0079`, `docs/adr/0088`).

## Why neither input is coverage pre-checked

Neither the overlay layer nor the input layer is checked or cleaned for coverage
violations before assign/clip runs (see ADR-0018). `_02_outputs.py`'s invalid-edge and gap checks write what a dirty overlay or
input layer leaves in the output as issue rows, so it doesn't reach export
unreported. The
input layer is additionally assumed to already be a finished `edge_extend()`
output, which `edge-mosaic` never re-verifies.

## edge-match vs. edge-mosaic

| | `edge-match` | `edge-mosaic` |
| --- | --- | --- |
| Input assumption | Input layer is raw, unextended | Input layer is already a finished `edge_extend()` output |
| Assign strategy | `assign-many` (per-polygon plurality) | `assign-one` (per-file majority vote) |
| Cost driver | Per-group Voronoi extension (~85% of wall time) | Assign + clip only, no extension |
| Isolation | Two subprocess generations: per-group `edge-extend`, then batched per-`overlay_fid` `edge-clip` (`docs/adr/0020`) | Per-`overlay_fid` subprocess only, heavy overlay polygons split with `ST_Subdivide` (`docs/adr/0015`, `docs/adr/0140`) |
| When to use | Input layer hasn't been extended yet | Reusing pre-extended layers against a new/different overlay polygon |

## Caveats

**Assign runs against overshoot geometry.** See `docs/dev/explanation/3-edge/assign.md`'s
Caveats section for the full detail on `assign-one`'s per-file majority vote
and its residual risk (a file too small to form a real majority).

**Clip runs one overlay fid at a time, each in its own subprocess, heavy
overlay polygons split with `ST_Subdivide`.** See `docs/dev/explanation/3-edge/edge_clip.md` for the
full detail.

**Cross-provenance seam risk.** `docs/dev/explanation/3-edge/edge_stitch.md` documents
genuine (non-float-noise) seam disagreements up to ~645m between two tiles
computed independently. `edge-mosaic`'s input polygons can come from wholly different
tool versions or vintages (the portolan catalog has per-country pipeline
drift, e.g. `phl` has v01 through v03), with no guarantee that any two
`extended.parquet` files being combined into one `edge_mosaic()` input were even
produced by compatible `edge_extend()` versions (only empirically-confirmed
schema/CRS compatibility). Seams between input files show up as `invalid-edge` rows, but a
multi-provenance mosaic run should still have its overlay-to-overlay boundaries spot-checked visually (e.g. via
the `geo-preview` skill), not just trusted because the hard gate passed.
Use `--debug` to inspect the `{name}_03` table's `source_file` column
(dropped from the exported output, see `docs/adr/0087`) to find which two
files actually meet at a flagged seam.

**Unclipped geometry meeting clipped neighbors.** With `--merge` set, a
passthrough file's boundary was never intersected against the overlay layer,
unlike every clipped neighbor
around it; any seam disagreement there is on top of the ordinary
cross-provenance risk above, not instead of it. Passthrough rows are left out of the invalid-edge check, since nothing ties
their edges to their clipped neighbours, so a passthrough run is worth the same visual spot-check as a
multi-provenance one.

## Opt-in `schema-fill` composition (`fill_schema`)

`edge-mosaic` MAY invoke `schema-fill`'s own fill logic itself, right
after clipping and before export, via `fill_schema=True` (CLI:
`--fill-schema`). This lives entirely in `api/edge_mosaic.py`, at both
insertion points (the single-file step loop's `outputs` branch and
`_mosaic_multi_file()`'s own final stage), calling
`core.schema_fill._02_fill.main()` directly through the private
`api._schema_fill_compose` helper; `core.edge_mosaic` itself is
unchanged and still MUST NOT depend on `core.schema_fill`/
`core.schema_map` (see `docs/adr/0095`).

`fill_schema` and `merge` are conceptually complementary but
independently gated flags, not aliases: `merge`'s own
`fill_unmatched_overlays()` (`docs/adr/0083`) fills a *geometry-coverage*
gap, an overlay polygon with zero matched input polygons, by keeping its own unclipped
geometry in the output; `fill_schema` fills a *schema-depth* gap, a row
whose admin-hierarchy columns don't reach as deep as some other row's,
by cascading each column family down to the row's own real depth. Both
can be set together freely; neither implies the other.
