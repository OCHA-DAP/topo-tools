---
title: "topo-clean"
sidebar:
  order: 7
  badge:
    text: Draft
    variant: caution
---

`topo-clean` detects and fixes coverage defects (gaps, overlaps) in a single
polygon layer using `ST_CoverageClean`. Ported from the sister JS app's
interactive Topology Cleaner tool
(`cod-site/src/lib/tools/topo-clean/`); this doc covers what
changed in the port and why, and the `ST_CoverageClean` parameter semantics
the design depends on.

`topo-clean` reuses none of `edge-extend`'s stage functions: `_01_inputs` calls the
shared `core.io.read_and_reproject()` directly (minus `edge-extend`'s own
auto-clean pre-check, see "Pipeline" below) and `_03_clean` calls the
shared `core.coverage.coverage_clean()`; both are tool-independent leaf
modules, so `core.topo_clean` never imports `core.edge_extend`. It does reuse
`core.topo_detect`'s own issue-detection stage function directly, the same
pattern `edge-match`/`change` use for `core.edge_extend`'s (see "Pipeline" below,
`docs/adr/0028`).

## Usage

```sh
topo-tools topo-clean example.geojson
```

```python
from topo_tools import topo_clean

topo_clean("example.geojson")
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with a
`_cleaned` suffix.

| Option | Description |
| --- | --- |
| `--issues-file` | Issues report path. Defaults to `OUTPUT_FILE` with an `_issues` suffix. |
| `--maximum-gap-width` | `thin` (fill thin/sliver-shaped gaps), `all` (fill every detected gap), or a number in decimal degrees. Omit for the default: fill only gaps at or below `SNAP_TOLERANCE`. |
| `--snapping-distance` | A number in decimal degrees. Omit for the default: `SNAP_TOLERANCE`. Noding robustness knob only. |
| `--overwrite` | Overwrite an existing output file. |
| `--threads` | DuckDB thread count. |
| `--debug` | Keep intermediate tables, export to Parquet, log timing/memory per query. |
| `--tmp-dir` | Intermediate DuckDB + Parquet location. |
| `--step` | Run only one named stage: `inputs`, `issues`, `topo-clean`, `outputs`. |

```sh
# Only fill thin/sliver-shaped gaps, leave the rest for review
topo-tools topo-clean example.gpkg --maximum-gap-width thin

# Cap gap-filling at ~0.0001 degrees (~11m at the equator)
topo-tools topo-clean example.parquet --maximum-gap-width 0.0001
```

Run `topo-tools topo-clean --help` for the full, always-current option list.

## Pipeline

1. **`_01_inputs`**: reads and reprojects to EPSG:4326 via the shared
   `core.io.read_and_reproject()` helper, **without** `edge-extend`'s own auto-clean
   pre-check. This is deliberate: `topo-clean`'s whole purpose is to detect
   defects in the *raw* input, so the detection stage needs to see them, not
   a table `ST_CoverageClean` has already silently rewritten.
2. **issues**: `topo-clean` calls `core.topo_detect`'s own `_02_issues.main()` stage
   function directly, writing `{name}_02`, the same pattern `edge-match`/
   `edge-mosaic` use for `core.assign`/`core.edge_clip`/`core.edge_stitch`'s stage
   functions. See `docs/dev/explanation/2-topology/topo_detect.md` for how detection itself
   works (this used to be `topo-clean`'s own `_02_issues.py`; see `docs/adr/0028`).
3. **`_03_clean`**: fixes gaps/overlaps via a single `ST_CoverageClean`
   call (gated: a no-op copy only if the input has no coverage violations
   *and* no detected gap qualifies to fill under the resolved
   `gap_maximum_width`, see "`has_invalid_edges()` alone cannot
   stand in for gap detection" below; that check is only about
   overlaps/mismatched edges, so a gap-only input still needs
   `ST_CoverageClean` to actually run), validated against invalid edges, a
   total-area floor, and a per-fid collapse/geometry-type check, raising
   immediately if any fails (see "Validating a coverage-clean result"
   below). `coverage_clean` merges micro-polygons before cleaning, and a
   second `merge_micro_polygons` pass runs on the fixed output; both
   passes' merge rows land in `{name}_03_micro`. When `{name}_02` has a
   notch row, `close_notches()` first moves each flagged endpoint onto the
   other unit, on a copy of `{name}_01`, and the clean runs on that copy.
   An endpoint touching a third unit stays put, since moving it would cut
   a wedge out of one unit that no other unit fills.
4. **`_04_outputs`**: validates overlaps are gone (hard gate), logs
   (never raises on) any gaps still unfilled by design, and exports the
   cleaned dataset plus the issues report (only when it has rows).

## Detection itself lives in `topo-detect` now

See `docs/dev/explanation/2-topology/topo_detect.md` for how gap/overlap detection works
(skipping overlap detection when the coverage is already valid, the
bbox-precompute-before-join requirement, why sliver detection/fixing was
removed entirely). `has_invalid_edges()` alone cannot stand in for
gap detection: it only detects overlaps/mismatched edges, never gaps. `_03_clean.py`'s fix-stage gate also checks
whether any detected gap qualifies to fill under the resolved
`gap_maximum_width`, not `has_invalid_edges()` alone (see "Pipeline"
above). See `docs/adr/0008-has-coverage-violations-misses-gaps.md`.

## `ST_CoverageClean` parameter semantics: verified against upstream source

This mattered enough to check the actual GEOS/duckdb-spatial source rather
than assume JS's UI defaults translate directly (`duckdb-spatial`'s
`src/spatial/modules/geos/geos_module.cpp`/`geos_geometry.hpp`, and GEOS's
own `include/geos/coverage/CoverageCleaner.h`/`src/coverage/CoverageCleaner.cpp`):

- **`snapping_distance`** has a real computed auto-default:
  `extent_diameter / 1e8` (`computeDefaultSnappingDistance`).
  `setSnappingDistance(x)` is a no-op when `x < 0`, so omitting the argument
  (DuckDB's `Bind()` defaults a missing arg to `-1.0`) keeps this
  auto-computed value. `0` explicitly disables snapping; a positive value
  overrides it.
- **`gap_maximum_width` has NO computed auto-default**: the C++ class
  member is hardcoded to `0.0` ("a width of zero prevents gaps from being
  merged"). `setGapMaximumWidth(x)` is also a no-op when `x < 0`, so an
  omitted argument leaves it at `0.0`, i.e. **no gap-filling at all**. This
  is why `edge-extend`/`edge-match`'s existing `coverage_clean()` calls (which never
  pass a positive `gap_maximum_width`) never fill gaps, and why JS's "fill
  up to 2x the widest detected gap" was purely a client-side slider-seeding
  heuristic, not anything GEOS computes on its own. It's also why `topo-clean`'s
  own `--maximum-gap-width thin` mode has to compute a real width itself
  (see below): there's no GEOS-side default to lean on the way both flags'
  own default (omitted-flag) behavior leans on this codebase's own fixed
  `SNAP_TOLERANCE` instead (see `docs/adr/0032`, `docs/adr/0033`).
- **Naming**: `coverage_clean()` (the shared `core/coverage.py`) calls
  `ST_CoverageClean` positionally (`geoms, snapping_distance,
  gap_maximum_width`), passing `-1` for either argument a Python `None`
  omits; DuckDB binds named arguments to compiled scalar functions by
  position, not name, so a conditional named-arg call is unsafe here (see
  `docs/adr/0003-st-coverageclean-positional-args.md`). The CLI/API layer
  (`--maximum-gap-width`, `--snapping-distance`, and the matching
  `api.topo_clean.clean()` kwargs) follows GDAL's `gdal vector clean-coverage`
  word order for the gap one, since that's what a human actually types;
  `api.topo_clean.clean()` is the one place the two namings meet.
- `ST_CoverageClean`'s gap-merge only fills **fully-enclosed** holes: a
  ring of polygons surrounding missing area (a lake, a missing admin unit).
  An open "inlet" gap between two side-by-side, non-enclosing polygons is
  left untouched regardless of `gap_maximum_width` (demonstrated with an
  isolated 2-polygon fixture: identical output whether
  `gap_maximum_width` was `-1`, a tiny value, or 1 full degree). GEOS's own
  class doc says as much: "gaps which are not fully enclosed ... are not
  removed." This is also why `topo-detect`'s gap-detection query (interior
  rings of the whole-table union, see `docs/dev/explanation/2-topology/topo_detect.md`) misses
  open inlets, they aren't fillable "gaps" by this tool's or GEOS's own
  definition.

## `--maximum-gap-width thin|all|<degrees>` (default: omit the flag)

- Default, reached only by omitting the flag, not by naming it (`"auto"`
  raises `ValueError`, see `docs/adr/0033`): fills a gap only if its width
  is at or below `SNAP_TOLERANCE`, the same fixed noise floor used
  everywhere else in the pipeline. Unconditionally safe regardless of
  shape, since anything that small is floating-point noise by construction.
- `thin`: fills only gaps whose *shape* looks like a digitization sliver,
  regardless of their absolute width. `gap_maximum_width` is a pure width
  cutoff with no shape concept of its own (verified live against GDAL's
  `gdal vector clean-coverage` docs), so this computes the width to feed
  into it: the widest gap's own width *among only the gaps classified as
  thin* (`thinness_ratio <= DEFAULT_THINNESS_RATIO`), measured on the gaps
  of the table `ST_CoverageClean` runs on, after notch closing, since
  closing a notch trims the sliver part off a gap (`gap_issues_sql()`'s
  `max_width_m`, from `(ST_MaximumInscribedCircle(geom)).radius * 2`, GEOS's
  own width metric)
  plus a small epsilon (`AUTO_GAP_WIDTH_EPSILON_FACTOR` in `_constants.py`)
  so the widest thin gap itself clears the `<=` comparison. "Thin" is a
  Polsby-Popper compactness score (`4*pi*Area/Perimeter^2`, 1.0 = circle,
  ->0 = elongated crack) at or below `DEFAULT_THINNESS_RATIO = 0.3`
  (`core/topo_clean/_constants.py`), the same formula and cutoff guidance
  ArcGIS Pro's own "Polygon Sliver" data-quality check uses to flag slivers.
  Not user-configurable: a gap's absolute width says nothing about whether
  it's a digitization artifact or a real feature (a small real pond and a
  narrow real strait both exist), but its shape, independent of scale, is
  what the sliver-detection literature already uses to make this call; if
  a dataset needs different behavior than the fixed cutoff produces, tune
  `--maximum-gap-width <degrees>` directly instead of the thinness cutoff.
  Known, accepted imprecision: since GEOS only ever compares by width, a
  non-thin gap narrower than the widest thin gap would also get swept in.
  This imprecision, applied globally across a whole table rather than
  per-polygon, is why this mode was demoted from the default, see
  `docs/adr/0033`. When no gap qualifies as thin, the argument is omitted
  entirely (no gap-filling).
- `all`: fills every gap the detection stage found, using a fixed
  `GAP_MAXIMUM_WIDTH_ALL_DEG = 360` (`_constants.py`), confirmed safe across
  the full `0`-`360°` range once `coverage_clean()`'s named-argument bug was
  fixed (see `docs/adr/0003-st-coverageclean-positional-args.md`).
- A bare number is an explicit cap in decimal degrees, passed straight
  through to `ST_CoverageClean` with no conversion.

## Validating a coverage-clean result

`_03_clean.py`'s `main()` makes a single `ST_CoverageClean` call at the
resolved target width (from the default/`thin`/`all`/an explicit value) and
validates
it against **both** `has_invalid_edges()` *and* a total-area sanity
floor: the invalid-edges check alone passes a totally empty result as
"no violations," confirmed directly, so it can't catch a collapsed output
on its own. `main()` raises immediately if either check fails.

An earlier escalation-ladder retry approach was removed after the two
failure modes it worked around both turned out to be
`coverage_clean()`'s named-argument bug (see "Naming" above), not
independent `ST_CoverageClean` instability. See
`docs/adr/0003-st-coverageclean-positional-args.md`.

### The total-area floor is anchored to detected overlap area, not dataset size

The floor scales with what was actually detected, not a flat fraction of
dataset size (which can't distinguish a large, legitimate overlap-resolution
cost on a small dataset from real corruption, see
`docs/adr/0009-area-floor-anchored-to-overlap-area.md`):

```
min_area = input_area * (1 - AREA_NOISE_FACTOR) - overlap_area * OVERLAP_LOSS_HEADROOM
```

- `AREA_NOISE_FACTOR = 0.02` bounds baseline loss when **no** overlaps are
  detected: double the ~1% per-fid renoding drift confirmed on real
  defect-dense data (see "Collapse vs. drift" below), so a
  defect-unrelated fid drifting by its normal amount can't tip a small,
  overlap-free dataset below the floor.
- `overlap_area` is `SUM(ST_Area(geom))` over `{name}_02`'s `kind =
  'overlap'` rows, in the same degree² units as `input_area`/`output_area`.
- `OVERLAP_LOSS_HEADROOM = 3.0` allows resolving an overlap to cost up to
  3x its own detected footprint: `ST_CoverageClean` can redraw a fid's
  boundary well beyond the immediate overlap it's resolving, confirmed up
  to ~1.5x on a real regression case (see "Defect-adjacent exemption"
  below).

### The total-area floor alone misses a small, localized collapse

The total-area floor only bounds the *summed* output area, so a single
small polygon collapsing entirely can hide inside it if the rest of the
dataset is much larger. The result is also checked per fid:

- **Defect-adjacent exemption.** A fid touching a gap that gets filled, or
  party to a detected overlap, is exempt from both checks below, by any
  amount; `ST_CoverageClean` can redraw that fid's whole neighborhood, not
  just the immediate defect.
- **Collapse vs. drift, for everything else.** A defect-unrelated fid
  collapsing to empty fails this rung outright. A defect-unrelated fid that
  merely shifts area (nonzero, nonempty) only logs a warning:
  `ST_CoverageClean`'s whole-table renoding measurably shifts even fully
  unrelated boundaries on defect-dense real data. Both units of a detected
  notch count as defect-adjacent.
- **Geometry type.** `ST_Area()` sums only the polygonal members of a mixed
  `GEOMETRYCOLLECTION`, so a fid partly reduced to a stray line or point
  during the fix could otherwise still measure as area-preserving. Any fid
  whose fixed geometry isn't a `POLYGON`/`MULTIPOLYGON` fails validation.

Both checks compare against exact, defect-derived bounds rather than a
fraction of the whole dataset: a percentage floor on a per-fid basis was
tried and rejected, since full containment (one fid entirely absorbed by
another) and gap-neighborhood redistribution are both legitimate
100%-loss outcomes for the fid actually involved.

## `--snapping-distance <degrees>` (default: omit the flag)

Default, reached only by omitting the flag, not by naming it (`"auto"`
raises `ValueError`, same reasoning as `--maximum-gap-width`'s default,
see `docs/adr/0033`): resolves to `SNAP_TOLERANCE`, not `ST_CoverageClean`'s
own extent-relative computed default (`extent_diameter / 1e8`): that
default scales with the whole input's bounding-box diagonal, undershooting
`SNAP_TOLERANCE` on small territory files and ballooning to hundreds of
meters on country- or continent-scale ones, see `docs/adr/0032`. An
explicit value (decimal degrees, passed straight through) overrides it.
This is a **noding-robustness knob only**, per GEOS's own doc comment, "a
large snapping distance may introduce undesirable data alteration."

## Issues file schema

`topo-clean`'s issues output extends `topo-detect`'s own base schema (see
`docs/dev/explanation/2-topology/topo_detect.md`, "Issues table schema") with four more
columns, all *measured outcomes* of the fix, not the defect as originally
detected: `fixed BOOLEAN`, `unit_a_area_change_m2 DOUBLE`,
`unit_b_area_change_m2 DOUBLE`, `filled_area_m2 DOUBLE`. `filled_area_m2`
is populated only for gap rows; `unit_a`/`unit_b_area_change_m2` only for
overlap and notch rows. `overlay_fid` and `source_file` are always null for
`topo-clean`, and `reason` is set only on `micro-polygon` rows: they exist
so the schema matches `edge-match`/`edge-mosaic`/`edge-stitch`'s own issues
tables column-for-column. Each `micro-polygon` row comes from `{name}_03_micro`, the merge
outcome (receiving fid in `unit_b`, `fixed=TRUE`), replacing
`topo-detect`'s detection row for it.

All four are computed by `_04_outputs.py`'s `_add_outcome_columns` from
`{name}_01` and `{name}_03` after `_03_clean.py` has run, since that's the
first point both the pre- and post-fix geometry are available together.
For an overlap row, each `_area_change_m2` is that unit's own real area
change (output minus input), and `fixed` is unconditionally `TRUE`:
`check_invalid_edges(conn, f"{name}_03")` already gated the output as
overlap-free before this runs, so any overlap row reaching the issues
file was necessarily resolved. For a gap row, `filled_area_m2` is how much
of the gap's own area ended up covered by the cleaned output (`0` if left
unfilled by design), and `fixed` is TRUE only when no gap still open in
`{name}_03` (`gap_issues_sql()`, wider than `SNAP_TOLERANCE`, the same
gaps `topo-detect` reports) overlaps it with positive area. An open gap
that overlaps no detected gap gets its own `gap-new-{n}` row, `fixed`
FALSE, reason `gap opened by the fix`. `_warn_on_unfilled_gaps` counts
the unfixed rows, which then match `topo-detect`'s gap count on the
output.
For a notch row, `fixed` is FALSE if `detect_notches()` on `{name}_03`
still finds a notch between the same two units that overlaps the row's
geometry.
`_03_clean.py`'s own success log line reports the same idea at the
whole-dataset level (total area gained/lost, as a percentage) regardless
of which individual defects caused it.

## Units: decimal degrees in, meters out

`--maximum-gap-width` and `--snapping-distance` are decimal degrees, passed
straight through to `ST_CoverageClean` with zero conversion: the same
units `_01` is already stored in (EPSG:4326), and the same convention
GDAL/OGR uses for any distance parameter on an unprojected layer (native
CRS units, not meters). The value passed is exactly what GEOS receives, with
no dataset-dependent scaling in between; important when tuning these
values against a specific `ST_CoverageClean` result, since a hidden
conversion factor would mean the number on the CLI and the number GEOS
actually sees are never quite the same thing.

The *output* side still reports in meters for human readability: the
issues file's `area_m2`/`max_width_m` columns are computed from raw
degree-space geometry via a centroid-latitude `cos_lat_factor`
(`core/units.py`, shared with `topo-detect`, see
`docs/dev/explanation/2-topology/topo_detect.md`'s "Units" section), approximate over very
large north-south extents, but adequate for a reporting column, not a
cleaning tolerance.

## The gap gate is bounded by topo-clean's own fill target

`_03_clean.py`'s post-fix validation raises if the output has any overlap,
or any unfilled gap at or below `gap_maximum_width_deg`, the width it
actually asked `ST_CoverageClean` to close for this run (`has_gaps(conn,
out_table, gap_maximum_width=gap_maximum_width_deg)`, skipped when
`gap_maximum_width_deg is None`, meaning no fill was requested at all;
see `docs/adr/0037`). A gap left wider than that by design (a real gap
under the default, `--maximum-gap-width thin` on a compact/non-thin gap,
or a numeric cap narrower than some detected gap) does not raise: `topo-clean`
can legitimately leave those unfilled. `_04_outputs.py` logs a warning
with a count of how many detected gaps remain uncovered regardless of
width, tested via `ST_Contains` against a point on each gap's surface;
visibility for the issues file, not a second failure condition.

`check_invalid_edges()` **is** reused as a hard gate in `_04_outputs.py`
too, but by the time it runs, `_03_clean.py`'s own validation (above) has
already confirmed `{name}_03` has no invalid edges, or raised before
returning. This check is a defensive double-check for `--step outputs`
(a persisted `_03` table from a prior run), not the primary safety net:
any survivor here would mean `_03_clean.py`'s own validation had a bug,
not that `ST_CoverageClean` itself misbehaved. The new gap-width check
has no such double-check in `_04_outputs.py`: `gap_maximum_width_deg` is
a `_03_clean.py`-local value with no persisted form to re-check against.

## Resilience

Detection's own per-kind retry-to-empty resilience (and its profiling at
portolan scale) is now `topo-detect`'s concern, see `docs/dev/explanation/2-topology/topo_detect.md`.

A precision-reduction retry (via `ST_ReducePrecision`) is not used as a
fallback in the fix stage: the actual driver of the coverage-clean
instabilities once observed here was `coverage_clean()`'s named-argument
bug (see `docs/adr/0003-st-coverageclean-positional-args.md`), not
floating-point precision, so there's no remaining case for a second,
unvalidated retry lever that silently perturbs input geometry.
