---
title: "edge-match"
sidebar:
  order: 13
  badge:
    text: Draft
    variant: caution
---

`edge-match` fits an **input** polygon layer into a coarser **overlay** layer
(e.g. admin4 into admin0), reusing `edge-extend`'s Voronoi gap-filling pipeline
under the hood, then the same `assign`/`edge-clip`/`edge-stitch` primitives `edge-mosaic`
uses. This doc covers the parts that are specific to `edge-match`: the
overlap/assignment algorithm, the per-group subprocess design, and the
constraints it inherits from `edge-extend`. See `docs/dev/topology.md` for the
coverage-clean background both tools share.

## Usage

```sh
topo-tools edge-match input.geojson overlay.geojson
```

```python
from topo_tools import edge_match

edge_match("admin4.geojson", "admin0.geojson", "admin4_matched.geojson")
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with a
`_matched` suffix; it is required when `INPUT_FILE` is a glob matching more
than one file, or when `--input` is given.

| Option | Description |
| --- | --- |
| `--input` | Extra input file(s), repeatable and comma-separable, combined with `INPUT_FILE`. |
| `--overwrite` | Overwrite an existing output file. |
| `--threads` | DuckDB thread count. |
| `--debug` | Keep intermediate tables, export to Parquet, log timing/memory per query. |
| `--tmp-dir` | Intermediate DuckDB + Parquet location. |
| `--step` | Run only one named stage: `inputs`, `assign`, `groups`, `edge-clip`, `edge-stitch`, `outputs`. Unavailable when multiple input files are given. |

```sh
# Fit an admin4 layer into a single country boundary
topo-tools edge-match adm4.geojson adm0.geojson

# Fit admin3 into admin2 groups, each cleaned against its own overlay polygon
topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg

# Combine several raw countries' admin1 layers, matched and extended
# together against one shared overlay polygon
topo-tools edge-match sen_adm1.parquet world_adm0.geojson out.parquet \
  --input gmb_adm1.parquet,gnb_adm1.parquet
```

Each overlay polygon's group of input polygons runs in its own isolated subprocess, so a run
with many overlay polygons (e.g. matching a nationwide admin4 layer against dozens of
admin2 units) scales without one large group's memory use affecting another's.

Run `topo-tools edge-match --help` for the full, always-current option list.

## Pipeline

1. **`_01_inputs`**: closes notches in the input layer, then coverage-cleans
   it, via the shared
   `core.io.read_reproject_and_clean()` helper (`{name}_input_01`), and
   loads the overlay layer raw via `core.assign.load_overlay()`
   (`{name}_overlay_01`), the same loader `edge-mosaic` uses (see
   `docs/adr/0086`).
2. **assign**: calls `core.assign.assign_many()` directly: assigns each
   input polygon to the overlay polygon it shares the largest area with (plurality, not
   majority); drops and logs input polygons with zero overlap with any overlay polygon,
   keeping their geometry for the issues report (or, when `merge` is set,
   for the orphan group below instead; see "Overlay gap-fill and input
   passthrough"). See `docs/dev/explanation/3-edge/assign.md` for the algorithm.
3. **`_02_groups`**: groups input polygons by their assigned overlay polygon (always, even
   a group of exactly one input polygon), runs `edge-extend`'s pipeline within each group
   in an isolated subprocess, then reassembles the survivors, tagging each
   row with its group's own `overlay_fid`. No clipping happens here anymore
   (see "Two subprocess generations" below). A failed group's input polygons are
   recorded, with the overlay polygon id and failure reason, for the issues report.
   When `merge` is set and any input polygons were unassigned, one more orphan
   group runs afterward (see "Overlay gap-fill and input passthrough").
4. **`_03_clip`**: one batched call into `core.edge_clip.main()` over every
   real group's rows at once, clipping each to its own `overlay_fid`'s
   geometry. See `docs/dev/explanation/3-edge/edge_clip.md` for the algorithm. The
   orphan group's rows (when present) are split out first and unioned back
   in afterward, unclipped. When `merge` is set, the api layer then calls
   the shared `core.assign.fill_unmatched_overlays()` to append every
   unmatched overlay polygon's own geometry, before stitch ever runs.
5. **`_04_stitch`**: calls `core.edge_stitch._02_clean.main()` directly: a
   single whole-table `ST_CoverageClean` pass over the clipped output to
   close cross-group seams. See `docs/dev/explanation/3-edge/edge_stitch.md`.
6. **`_05_outputs`**: validates topology (any overlap, or a gap at or below
   `SNAP_TOLERANCE`, raises), builds the issues report from the dropped
   input polygons collected in stages 2/3 (or the passthrough input polygons and
   gap-filled overlay polygons, when `merge` is set) plus any leftover gap wider
   than `SNAP_TOLERANCE`, logs a warning if any such gap remains, and
   exports both the final layer and the issues report (only when it has
   rows).

## Multi-file input

The input role MAY span multiple files in one call (e.g. one raw admin
boundary file per country); the overlay layer stays single-file.
`output_path` MUST be given explicitly whenever multiple paths are passed,
and `step`/`per_feature` MUST both be `None`/`False` (see `docs/adr/0084`).

With more than one input path, `_match_multi_file()` (`api/edge_match.py`)
runs only `inputs`+`assign` in a per-file loop, mirroring `edge-mosaic`'s own
`_mosaic_multi_file()` memory-bounded pattern (`docs/dev/explanation/3-edge/edge_mosaic.md`)
but stopping one stage earlier: the overlay layer is loaded once into a pristine
snapshot with its heavy-part tile decomposition cached
(`core.assign.prepare_overlay_tiles()`), then each input file is loaded and
assigned alone against a fresh copy of that snapshot, one at a time, folding
`{name}_input_01`, `{name}_02_assign`, and `{name}_02_unassigned` into running
accumulators (`UNION ALL BY NAME`) rather than holding every file until a
final combine. `fid` is kept globally unique via a running offset applied
right after each file's assign step. `groups`, `clip`, `stitch`, and
`outputs` all run exactly once afterward, over the fully accumulated result,
not per file: grouping is keyed purely by `overlay_fid`
(`_02_groups.py::list_groups`), so input polygons from different files sharing a
`overlay_fid` extend together as one Voronoi group only if groups runs after
every file's input polygons have landed in `{name}_02_assign`, which is the whole
point of combining files here (unlike `edge-mosaic`, whose clip step is
embarrassingly per-file and folds directly into its loop instead).

`assign_one` narrows `{name}_overlay_01` to only that iteration's matched
fids at the end of every call, so the loop resets it from the full snapshot
at the start of every iteration, and restores it once more from the
snapshot after the loop ends, before `groups` runs; otherwise `groups`/`clip`
would only see the last file's matched overlay polygons, not the union across all
files. Every row on the internal `{name}_05` table still carries a `source_file`
column tagging its origin file; it's an `assign-one` working column,
stripped before the exported output (see `docs/adr/0087`).

## Two subprocess generations: edge-extend, then batched edge-clip

Before the `assign`/`edge-clip`/`edge-stitch` extraction, each group's subprocess ran
`edge-extend`'s pipeline *and* clipped to that group's overlay polygon in the same
process. `core.edge_clip` now always isolates per distinct `overlay_fid` in its
own spawned subprocess, uniformly for every caller, so `edge-match` moved to two
subprocess generations per run instead: a per-group `edge-extend`-only
subprocess (unchanged in count/shape from before), followed by a second,
later generation of per-`overlay_fid` clip subprocesses (`edge-clip`'s own
mechanism, batched over the whole reassembled table). See
`docs/adr/0020-match-clip-two-subprocess-generations.md` for the empirical
re-verification this required and its results.

One consequence: a single bad `overlay_fid` in the `edge-clip` step now aborts
the whole run, rather than edge-match's old per-group continue-past-failure
behavior for clip failures specifically. Per-group `edge-extend` failures (OOM,
or exhausting `attempt.py`'s 10 retries) still continue, dropping just that
group, as before; only clip's own hard-fail-on-first-bad-`overlay_fid`
semantics are new, and apply uniformly to every `edge-clip` caller including
`edge-match` (see `docs/dev/explanation/3-edge/edge_clip.md`).

## Overlay gap-fill and input passthrough

Both opt-in via the single boolean `merge` flag (CLI: `--merge`), off by
default, the same flag `edge-mosaic` uses (see
`docs/dev/explanation/3-edge/edge_mosaic.md`); `--overlay-include`/`--overlay-exclude`/
`--input-include`/`--input-exclude`/`--prefer` further narrow which
columns survive (see `docs/dev/explanation/3-edge/assign.md`).

**Input passthrough.** `edge-match` uses `assign-many`, a per-polygon (not
per-file) assignment strategy, so its passthrough granularity is
per-polygon: an input polygon with zero overlap with any overlay polygon is dropped by
default, the same as always; with `merge` set, that input polygon (along with
every other zero-overlap input polygon, if any) is instead grouped into one
orphan group of its own, tagged with the reserved sentinel
`PASSTHROUGH_OVERLAY_FID` (`-1`, guaranteed absent from real overlay fids)
instead of a real `overlay_fid`, and run through the identical per-group
`edge-extend` subprocess as every other group. Extension only needs a
group's own input polygons, never an overlay polygon, so a group made of nothing but
orphans extends exactly like any other group; it just never gets clipped
afterward (`_03_clip` splits sentinel rows out before calling
`core.edge_clip.main()`, unions them back into `{name}_04` afterward,
unclipped), since there is no overlay polygon to clip against. A
successfully-extended orphan is reported as a `kind='passthrough'` issues
row rather than `unassigned`; an orphan group whose extension itself
fails still becomes a `kind='dropped_group'` row, same as any other
failed group. Any merged overlay columns (see `docs/dev/explanation/3-edge/assign.md`)
are NULL on passthrough rows, since there's no overlay polygon to join against;
`_02_groups.py`'s `INSERT INTO ... BY NAME` fills them in automatically
once the orphan group is appended after every real group (ordering
matters: appending it first would create `{name}_03a}` without the
carried columns, and a later real group's `INSERT ... BY NAME` would then
fail with extra, unmatched columns).

**Overlay gap-fill.** An overlay polygon matched by zero input polygons is dropped by
default; with `merge` set, that overlay polygon's own geometry and carried columns
are kept in the output unclipped instead, reported as a `kind='gap-fill'`
row. This is the shared `core.assign.fill_unmatched_overlays()` helper
(the same one `edge-mosaic` calls), called from the api layer right after
`_03_clip`'s `core.edge_clip.main()` call returns, against a
`{name}_overlay_full` snapshot taken before assign narrows
`{name}_overlay_01` to only-matched fids. Both mechanisms are identical in
outcome to `edge-mosaic`'s own `merge`, given an equivalent
raw/already-extended input polygon set against the same overlay polygon (see
`docs/adr/0088`).

**Materially weaker safety profile than `edge-mosaic`'s input
passthrough.** `edge-mosaic`'s passthrough geometry was already a
finished, validated `edge_extend()` output before the run even started.
`edge-match`'s orphan group is extended fresh, alone, with zero
neighboring-overlay context of any kind, and its own per-group extension
has no majority/plurality vote to fall back on if the extension
misbehaves (there was nothing to vote on, unlike a normal multi-feature
group where other input polygons can outvote one bad one). Treat the two
input passthrough modes as different risk profiles, not interchangeable;
a passthrough-heavy `edge-match` run is worth a visual spot-check (e.g.
via the `geo-preview` skill) before trusting it the way a normal matched
group would be trusted. Overlay gap-fill carries no such asymmetry: both
tools keep the same unclipped overlay geometry the same way.

## Per-group subprocess isolation (edge-extend)

Each group's `edge-extend` pipeline (`_02_lines` → `attempt` → `_05_merge`) runs in
its own fresh `multiprocessing` (`spawn` context) subprocess, not the parent
`edge_match()` call's shared connection. This is a deliberate design choice, not
an afterthought: CLAUDE.md documents a real, previously-confirmed finding
that GEOS's native heap isn't fully released between files even after
closing the DuckDB connection, which is exactly why `edge_extend()` processes one
file per OS process today. A many-overlay-group `edge_match()` run (e.g.
admin4-into-admin2 for a country with dozens or hundreds of admin2 units)
would hit the identical failure mode in-process, just with groups
substituting for files, if groups shared one process. Building the same
per-file-per-process isolation guarantee down to group granularity avoids
that outright rather than hoping in-process cleanup is sufficient.

Data crosses the process boundary as small Parquet files (`input.parquet`,
`overlay.parquet` in, `output.parquet` out), never a shared connection: a
DuckDB file is single-writer, and the group's own DuckDB file
(`group.duckdb`) lives entirely inside that group's private temp directory,
discarded when the group finishes (unless `--debug`). These files are
written with `PARQUET_OPTS` (`core/constants.py`, GeoParquet V2), which keeps
each geometry's `EPSG:4326` CRS exact through the round trip.

`edge_match()` raises only if **no** group produced any output at all (see
"Two subprocess generations" above for the separate, stricter clip-stage
failure behavior).

A freshly-spawned process has no logging configuration of its own
(`basicConfig` only ever runs in `cli/main.py`, in the parent process); the
worker puts a success/error signal on a `multiprocessing.Queue` instead of
relying solely on its own log output, and only configures logging locally
(mirroring `cli/main.py`'s own call, teed to a per-group log file) when
`--debug` is set, so `ProfiledConnection`'s per-query timing/RSS output isn't
silently dropped during a debug run.

**Real-world smoke test**: `bdi_admin4.gpkg` (3,067 polygons) matched against
`bdi_admin2.parquet` (119 overlay polygons) completed successfully end-to-end: 119
subprocess spawns, zero dropped input polygons, zero failed groups, valid output
coverage (see verification steps in the project's implementation history).

**Colombia-scale profiling** (portolan `col/latest/adm3` → `col/latest/adm2`,
`--debug`, Apple Silicon/10 logical cores): 31,880 input polygons against 1,122
overlay polygons, 1,120 of them with at least one assigned input polygon (the other 2 overlay polygons
had zero overlapping input polygons (not a failure, no adm3 unit fell inside
them). All 1,120 groups and all 1,120 clip subprocesses succeeded: zero
dropped input polygons, zero failed groups. Wall time 38m23s, peak RSS 7.23 GB
(see `docs/adr/0020` for the full before/after comparison against the
pre-extraction fused-subprocess design). Stage breakdown:

| Stage    | Wall time | Share |
| -------- | --------- | ----- |
| inputs   | 59s       | 3%    |
| assign   | 10s       | 0%    |
| groups   | 29m24s    | 77%   |
| clip     | 5m13s     | 14%   |
| stitch   | 46s       | 2%    |
| outputs  | 1m51s     | 4%    |

`groups` still dominates as expected (1,120 sequential subprocess spawns,
each running the per-group `edge-extend` pipeline) but no longer accounts for
the clip work it used to do inline; that now shows up as its own `edge-clip`
stage, batched over all 1,120 overlay fids after every group has finished.

## Rejected: `ST_Snap` around the clip step's `ST_Intersection`

`_05_merge.py` snapping the Voronoi cell onto its neighbor union before
`ST_Difference` (see `docs/dev/topology.md`) measurably reduces how many
untouched fids the final whole-table `ST_CoverageClean` pass has to touch,
because many *independent* per-fid `ST_Difference` calls against
*independently computed* neighbor unions invent slightly different
floating-point crossing points for what should be the same vertex. Two
variants of the same idea were tried in what was then `edge-match`'s own inline
clip step (now `core.edge_clip`), on the theory that a shared, exact overlay polygon
boundary (the overlay layer was coverage-cleaned in `_01_inputs.py` at the
time, so two adjacent overlay polygons' shared edge was vertex-identical) should let
two independently-clipped groups tile seamlessly if their output vertices
land on that same exact reference:

1. Snap each group's pre-clip geometry onto the overlay polygon's vertices, *before*
   `ST_Intersection(t.geom, p.geom)`.
2. Snap the clipped *result* onto the overlay polygon's vertices, *after* the
   intersection: the mirror image, covering the case where `ST_Intersection`
   itself perturbs the overlay polygon's inherited edge vertices during overlay
   processing rather than `t.geom`'s own vertices being the problem.

Tested both on Burundi, Sri Lanka, Malawi, Senegal, Haiti, Guatemala, and
Chile (admin2-into-admin1 for the first six, admin3-into-admin2 for Chile):
**zero measurable difference**, either direction, on every metric checked
(pre-clean invalid-edge flag, count of fids the clean pass actually
touches, whole-table clean peak RSS, wall time): identical or noise-level
(<10%, no consistent direction) on all seven files, including Chile's
56-group stress test (213/213 fids touched both ways, ~2550 MB peak all
three variants).

Root cause, found by extracting the actual invalid-edge geometries on
Burundi (`ST_CoverageInvalidEdges_Agg`, unnested, joined back to nearby
fids): all 171 invalid edges border exactly the fid pairs the assign step
places in two *different* overlay polygon groups, confirming these are genuinely
cross-group seams, but their lengths run from slivers up to **0.0058°
(~645 m)**, averaging **~12 m**. `SNAP_TOLERANCE` is `1e-8°` (~1.1 mm),
five to six orders of magnitude smaller. This isn't the same failure mode
as `_05_merge.py` at all: it's not two computations of the same crossing
point disagreeing by float noise, it's two *different* groups' Voronoi
extensions, built independently, with no knowledge of each other,
genuinely disagreeing about how far to reach near their shared overlay polygon
border. No vertex-snapping tolerance in a sane range closes a
meters-to-hundreds-of-meters gap; that's real gap-filling work, which is
exactly what `edge-stitch`'s whole-table `ST_CoverageClean` pass is for (see
`docs/dev/explanation/3-edge/edge_stitch.md`). Reverted both variants; the clip step stays
a plain `ST_Intersection`. This null result is also why `_01_inputs.py`
later dropped coverage-cleaning the overlay layer entirely: seam quality
never came from overlay polygon vertex identity in the first place (see
`docs/adr/0086`).

## `check_valid_topology` and overlay-layer gaps

`_05_outputs.py` calls `check_valid_topology(conn, f"{name}_05")`, relying
on its default `gap_maximum_width=SNAP_TOLERANCE` (see `docs/adr/0039`): it
still raises on any overlap or mismatched edge, but only raises on a gap
at or below `SNAP_TOLERANCE`, not any gap.

A zero-tolerance gate here breaks on a real case: matching South Africa's
admin4 into South Africa's own admin0 boundary correctly reproduces the
interior hole for Lesotho, a country fully enclosed by South Africa's
territory. That hole isn't a defect `edge-match` introduced, it's the overlay
layer's own legitimate shape, and the old strict gate raised
`RuntimeError` over it. `edge-match` has no way to distinguish that case from
an actual coverage defect by size alone (both can be wide), so instead of
guessing, it stops treating "wide gap" as fatal and reports it: any gap
wider than `SNAP_TOLERANCE` whose interior point falls inside an overlay
polygon the output was clipped to gets a `kind='gap'` row in the issues report
(width, area, thinness ratio) and a warning log, for a human to review. A hole
outside every such overlay polygon, like Lesotho's, is the overlay layer's own
shape and isn't reported.
Only a leftover gap at or below `SNAP_TOLERANCE` still raises, since
nothing that small should ever survive the pipeline's own noise-floor
cleaning passes; a leftover one there is unambiguously a bug, not a real
absence. See `docs/adr/0035`.

## Debug tables

`--step=groups --debug` exports everything currently in the connection
(`{name}_input_01`, `{name}_overlay_01`, `{name}_02_pairs`, `{name}_02_assign`,
`{name}_02_unassigned`, `{name}_03a`, `{name}_03b`), the same as a full run:
group ids
aren't known ahead of time, so there's no static table list to filter to for
that step. Per-group internal detail (the group's own `group.duckdb`,
`group.log`, `input.parquet`, `overlay.parquet`, `output.parquet`) is
preserved under `{tmp_dir}/{name}_g{overlay_fid}/` when `--debug` is set,
inspectable independently of the main connection's exports. `--step=clip
--debug` similarly preserves each `overlay_fid`'s own
`{tmp_dir}/{name}_04_p{overlay_fid}/` directory.

## Opt-in `schema-fill` composition (`fill_schema`)

`edge-match` MAY invoke `schema-fill`'s own fill logic itself, right after
stitching and before export, via `fill_schema=True` (CLI:
`--fill-schema`). This lives entirely in `api/edge_match.py`, at both
insertion points (the single-file step loop's `outputs` branch and
`_match_multi_file()`'s own final stage), calling
`core.schema_fill._02_fill.main()` directly through the private
`api._schema_fill_compose` helper; `core.edge_match` itself is unchanged
and still MUST NOT depend on `core.schema_fill`/`core.schema_map` (see `docs/adr/0095`).

`fill_schema` and `merge` are conceptually complementary but
independently gated flags, not aliases: `merge`'s own
`fill_unmatched_overlays()` (`docs/adr/0083`) fills a *geometry-coverage*
gap, an overlay polygon with zero matched input polygons, by keeping its own unclipped
geometry in the output; `fill_schema` fills a *schema-depth* gap, a row
whose admin-hierarchy columns don't reach as deep as some other row's,
by cascading each column family down to the row's own real depth. Both
can be set together freely; neither implies the other.
