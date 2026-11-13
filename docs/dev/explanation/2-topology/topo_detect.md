---
title: "topo-detect"
sidebar:
  order: 6
  badge:
    text: Draft
    variant: caution
---

`topo-detect` scans a single polygon layer for gap/overlap coverage defects and
reports them, without fixing anything. Extracted out of `topo-clean`'s own
issue-detection stage (`core/topo_clean/_02_issues.py`, moved verbatim to
`core/topo_detect/_02_issues.py`) so it can be run standalone, the same way
`assign` and the clip (`core.clip`) were extracted as reusable primitives
out of `edge-match`. `topo-clean` still detects defects as its first real step, it just
calls `core.topo_detect`'s stage function directly instead of owning the logic
itself, the same pattern `edge-match`/`edge-mosaic` use for `core.assign`/
`core.clip`, and `edge-match`/`change` use for `core.edge_extend`'s.
See `docs/adr/0028`.

## Usage

```sh
topo-tools topo-detect example.geojson
```

```python
from topo_tools.api.topo_detect import detect

detect("example.parquet", "example_issues.parquet")
```

`OUTPUT_FILE` (positional, optional) defaults to `INPUT_FILE` with an
`_issues` suffix.

Run `topo-tools topo-detect --help` for the full, always-current option list.

## Pipeline

1. **`_01_inputs`**: reads and reprojects to EPSG:4326 via the shared
   `core.io.read_and_reproject()` helper, **without** an auto-clean
   pre-check. This is deliberate: `topo-detect`'s whole purpose is to report
   defects in the *raw* input, so the detection stage needs to see them,
   not a table `ST_CoverageClean` has already silently rewritten.
2. **`_02_issues`**: detects gap, overlap, micro-polygon and notch regions,
   writing one issues table (`{name}_02`). Gap detection always runs; overlap detection is skipped
   (written empty directly) whenever `has_invalid_edges()` is
   already False, see "Skipping overlap detection when the coverage is
   already valid" below.
3. **`_03_outputs`**: exports `{name}_02` to the issues file. No hard
   gate: unlike every other tool, `topo-detect` never modifies geometry, so
   there is nothing to validate against, the same "no topology hard-gate"
   precedent `change` already sets for its own read-only comparison.

## Skipping overlap detection when the coverage is already valid

`main()` checks `has_invalid_edges()` (`ST_CoverageInvalidEdges_Agg`,
the shared `core/coverage.py`) before running `_build_overlaps`
(`_02_issues.py`'s bbox-prefiltered O(n^2) self-join), and writes an empty
overlaps table directly when it's already `False`: a coverage with no
invalid edges cannot contain an overlapping or nested pair either. Gap
detection (`_build_gaps`) always runs regardless: unlike overlaps, no
cheaper GEOS primitive answers "are there any gaps" without doing the same
whole-table union `_build_gaps` itself needs to extract them. See
`docs/adr/0007-skip-overlap-detection-when-valid.md`.

`_build_overlaps`'s bbox columns are precomputed once on `{table}_narrow`,
not called inline per pairwise comparison in the join's `ON` clause:
the latter makes DuckDB recompute the envelope per comparison instead of
once per row, which hangs indefinitely on a table with even a few
very-high-vertex-count polygons. See
`docs/adr/0014-bbox-inline-recompute-in-join.md`.

`has_invalid_edges()` alone cannot stand in for gap detection: it
only detects overlaps/mismatched edges, never gaps.

## Near-miss boundaries and notches

Near-miss boundary mismatches in general are out of scope: closing one
needs a wider `ST_CoverageClean` snapping distance, which re-nodes the
whole coverage (see `docs/adr/0006`). The one near-miss reported is a
notch, a pair of units whose unshared boundary segments run within
`NOTCH_SPACING / 8` of each other along at least `NOTCH_MIN_SCORE`
spacings, summed over both units. `detect_notches()` (`core/coverage.py`)
finds them with a bbox range join over unshared segments only, and each
notch row's geometry is the buffered cluster of its close-running pieces.
A thin gap or overlap's own tips have the same shape, so a notch that
intersects a reported gap, or an overlap between the same two units, is
left out of the report (see `docs/adr/0132`, `docs/adr/0136`).

## Issues table schema

`key VARCHAR, kind VARCHAR, area_m2 DOUBLE, max_width_m DOUBLE,
thinness_ratio DOUBLE, near_length_m DOUBLE, unit_a BIGINT, unit_b BIGINT,
geom GEOMETRY`. `kind` is `'gap'`, `'overlap'`, `'micro-polygon'` or
`'notch'`. `thinness_ratio` is populated only for gap rows, `near_length_m`
(the close-running length, in metres) only for notch rows, and
`unit_a`/`unit_b` for overlap and notch rows. Geometry
is always Polygon, so any of `edge-extend`'s four export formats (including
Shapefile) can hold the issues file. `topo-clean`'s own issues output extends
this schema further with measured fix outcomes (`fixed`,
`filled_area_m2`, `unit_a_area_change_m2`, `unit_b_area_change_m2`), see
`docs/dev/explanation/2-topology/topo_clean.md`; `topo-detect`'s own output never has those
columns, since nothing was fixed.

## Units: decimal degrees in, meters out

`area_m2`/`max_width_m` are computed from raw degree-space geometry via a
centroid-latitude `cos_lat_factor` (`core/units.py`, one degree of
longitude shrinks by `cos(latitude)`, scaled by the dataset's centroid
latitude), approximate over very large north-south extents, but adequate
for a reporting column. `topo-detect` reports every detected gap and overlap
regardless of size; no floating-point noise floor is applied, after
testing found no native GEOS jitter to guard against. See
`docs/adr/0010-noise-floor-removed-no-jitter-found.md`.

## Resilience

Each of the two detection queries (gap/overlap) falls back to an empty
result for that one kind (logged) on a GEOS failure, rather than raising,
except a DuckDB out-of-memory error, which raises so a run too large to check
is never reported as clean; the fallback is consistent with `edge-match`'s "failed group is logged and dropped, not fatal"
precedent, applied per-detection-kind here instead of per-group.

## Portolan-scale profiling

Real admin-boundary layers, `--debug`, Apple Silicon/10 logical cores (run
before sliver detection/reporting was removed from what was then `topo-clean`'s
own issues stage; the wall time/RSS figures below no longer include a
sliver-detection pass, which was the most expensive part at scale; expect
faster/lighter runs now):

| Dataset            | fids  | Wall time | RSS peak   | Gap/overlap defects found |
| ------------------ | ----- | --------- | ---------- | -------------------------- |
| Burundi admin2     | 122   | 1.1s      | 118 MB     | 0                           |
| Chile admin3       | 345   | 132s      | 1.07 GB    | 0                           |
| Indonesia admin3   | 7,069 | 503s      | 1.58 GB    | 8 gap                       |
| Philippines admin3 | 1,642 | 396s      | **5.15 GB**| 16 gap                      |
| Colombia admin3    | 31,880| 322s      | 4.87 GB    | 1,440 gap, 30 overlap       |

This run surfaced and fixed two scale bugs in `_02_issues.py`'s
`_build_overlaps`, both only visible past a few thousand fids: the overlap
join predicate was too permissive, and the self-join ran against a wide
table instead of a narrow projection. See
`docs/adr/0011-overlap-detection-scale-bugs.md`.

A later run against Colombia admin3 surfaced a third scale bug in the same
function: inline `ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` calls in the
overlap self-join's `ON` clause, which never finished (57+ min, killed) on
a table with a few 20k-54k-vertex polygons, fixed by precomputing bbox
columns instead of calling the envelope functions inline. See
`docs/adr/0014-bbox-inline-recompute-in-join.md`.

**Philippines admin3 exceeded the 4 GB container target** (5.15 GB peak) on
this run, driven mostly by the (since-removed) sliver-detection pass;
gap/overlap detection alone is expected to be substantially lighter.
`topo-detect` has no resampling knob to fall back on (nor does `edge-extend`
anymore, see `docs/dev/explanation/3-edge/voronoi-memory.md`), so per this repo's
"document, don't gate" policy this is noted here rather than
runtime-checked.

A more recent real-world run, detecting issues on a 56,550-row/111-country
combined layer (portolan `adm0` subset), measured `_02_issues`'s
gap-union + overlap self-join at ~17 minutes, independent of and before
any fix stage. That standalone cost, and the value of inspecting a layer's
defects without committing to fixing them, is what motivated pulling
detection out of `topo-clean` into its own tool. See `docs/adr/0028`.
