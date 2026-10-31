---
status: draft
title: "topology"
---

## DuckDB vs `gdal vector clean-coverage`

The pipeline previously called `gdal vector clean-coverage` (GEOS `GEOSCoverageSimplify`/repair) at the inputs and merge stages. It has been removed. This section records what DuckDB can and cannot replicate, and the approach that was chosen.

## Merge: union + difference + whole-table `ST_CoverageClean`

`merge.main` produces `_05` by:

1. Unioning each fid's original geometry with its Voronoi extension (`_04`) minus a bbox-prefiltered union of nearby originals (per-fid `ST_Difference`, not one global union used as the operand, see "Why not a single global union" below).
2. Dissolving to one row per fid and reattaching original attributes.
3. Running a single whole-table `ST_CoverageClean` call (via the shared `coverage_clean` helper in `utils.py`, also used by `inputs.main`'s coverage-clean pass) to close the floating-point-scale seams that the independent per-fid `ST_Difference` calls leave behind, the same failure mode described below under "Why the naive approach creates gaps," now fixed by the native function instead of hand-rolled noding.

This replaced an earlier `ST_Node` + `ST_Polygonize` design (kept below for historical context) that existed only because DuckDB spatial had no native `ST_CoverageClean` at the time it was built, confirmed by recovering the original PostGIS implementation (`git show f6a3b67:app_postgis/merge.py`), which used exactly this union+difference+coverage-clean pattern (via PostGIS's `ST_CoverageClean(geom) OVER ()` window function) long before the DuckDB port. Byte-exact preservation of original polygon vertices is not a goal of the current design: `ST_CoverageClean` may shift any polygon's boundary, including ones that weren't touched by the union/difference step.

### Why not a single global union as the `ST_Difference` operand

Using `ST_Union_Agg(_01)` (a single dissolved reference geometry) as the second argument to `ST_Difference` for every fid individually OOMs outright at Chile scale: the union can hold millions of vertices, and GEOS pays that cost on every row (`failed to allocate data of size 16.0 MiB (12.7 GiB/12.7 GiB used)`, observed during development). The fix is the same bbox-prefiltered neighbor-union self-join pattern `_02_lines.py` already uses for exterior-edge extraction: join `_04` against per-part (not per-fid) bboxes of `_01`, since a single Chile fid's multipolygon bbox can span mainland to a remote island and defeat the prefilter if not exploded into parts first.

### Tightening the bbox prefilter with `ST_Intersects`

`_05_merge.py`'s `neighbor_union` join (and `_02_lines.py`'s equivalent neighbor-union self-join) both add an exact `ST_Intersects` predicate on top of the existing bbox prefilter. It plans as a `FILTER` after a `PIECEWISE_MERGE_JOIN`, not as a `SPATIAL_JOIN`.

This matters far more in `_05_merge.py` than in `_02_lines.py`, because Voronoi cells (unlike original polygons) can have a bounding box spanning nearly the whole file: a coastal cell's cell boundary can legitimately run the length of a country's coastline while the cell itself only truly overlaps a small fraction of the original polygons whose bbox falls inside that huge rectangle. Measured on `chl_admin3` (345 fids): specific cells' bbox-only candidate counts were up to 23x their true `ST_Intersects` count (one cell pulled in 9765 candidate parts, only 924 of which actually touched it). Adding the filter reduced `_05_merge.py`'s merge-stage peak RSS by 34% (2846MB -> 1882MB) and `_02_lines.py`'s lines-stage peak by 11% (2149MB -> 1920MB), both confirmed output-identical to the unfiltered version (exact geometry equality). Smaller countries (Burundi, Sri Lanka, Malawi, Senegal, Haiti, Guatemala) saw smaller but still nonzero reductions (1-13%) with zero downside.

### Snapping the Voronoi cell before `ST_Difference`

`merge.main`'s per-fid `ST_Difference(voronoi_cell, neighbor_union)` calls independently invent a new crossing-point vertex wherever a Voronoi cell boundary crosses an original polygon edge. Two adjacent fids computing "the same" crossing get slightly different floats, and that's most of what the final whole-table `ST_CoverageClean` pass exists to fix (see above). Snapping the Voronoi cell onto `neighbor_union`'s real vertices with `ST_Snap(v.geom, n.geom, SNAP_TOLERANCE)` *before* the difference, in its own CTE, measurably shrinks how much of that fixing is needed. `SNAP_TOLERANCE` lives in the shared `core/constants.py`, not `edge_extend`'s own `_constants.py`, since `core/edge_stitch/_02_clean.py` reuses it too, as a `gap_maximum_width` for `edge-match`'s and `edge-mosaic`'s own cross-tile seam cleanup (see `docs/dev/explanation/3-edge/edge_stitch.md`):

- Tested on Burundi + 5 other countries + Chile: `coverage_clean` afterward touches far fewer fids that weren't actually extended, from 0% reduction (Sri Lanka, where nearly every fid was extended, so there's no untouched territory to protect) up to 95% (Guatemala, mostly untouched interior polygons) and 92% (Burundi). Chile, the stress-test file, saw a more modest 14% reduction (277 -> 239 fids touched), likely because its one 3796-part fid means many more independently-computed crossings feed into any single cell's difference.
- It does **not** eliminate the need for `coverage_clean`: on every file tested, invalid edges on the pre-clean table barely moved (e.g. Chile: 399 -> 395). The remaining mismatches come from crossings that land mid-segment on a straight original edge, not at an existing vertex, confirmed with a synthetic repro (`ST_Snap` only pulls existing vertices together within tolerance; it doesn't insert a new vertex into a segment just because another geometry crosses it there).
- Snapping was also tried the other way: `ST_Snap(ST_Difference(v.geom, n.geom), n.geom, tol)`, i.e. snapping the already-differenced remainder onto the original, and it's worse, not just ineffective: at tolerances above `SNAP_TOLERANCE` it started snapping unrelated nearby vertices onto the wrong target, distorting fids that the unsnapped baseline never touched at all (one fid's spurious movement grew from ~0.6 m² to 127 m² as tolerance loosened from 1e-8 to 1e-4). The invented crossing vertex from `ST_Difference` generally isn't within tolerance of any real vertex in `neighbor_union`, so post-hoc snapping has nothing correct to grab.
- Memory cost: nesting the snap inside the same expression as `ST_Difference` roughly doubled `_05_merge.py`'s merge-stage overhead on Chile (+22%). Pulling the snap into its own CTE (`snapped`, evaluated before `remainder`) halved that penalty (+11%) with identical output, and combined with the `ST_Intersects` tightening above, the net effect is a 34% *reduction* versus the original unmodified baseline, not a regression.

### Historical: why `ST_Node` + `ST_Polygonize` was used instead (now removed)

### What DuckDB spatial exposes

| Function                      | Purpose                                                                                        |
| ----------------------------- | ---------------------------------------------------------------------------------------------- |
| `ST_CoverageInvalidEdges_Agg` | Detects edges that don't match between adjacent polygons (validation only, no repair)          |
| `ST_CoverageSimplify_Agg`     | Topology-safe simplification (does not fix gaps or overlaps)                                   |
| `ST_CoverageUnion_Agg`        | Fast union for already-valid coverages (crashes on invalid input)                              |
| `ST_ReducePrecision`          | Snaps vertices to a grid; makes edge mismatch worse when applied to only one layer            |
| `ST_Node`                     | Computes all intersection points between a collection of lines, adding them as shared vertices |
| `ST_Polygonize`               | Builds polygons from a planar noded edge network                                               |
| `ST_MemUnion_Agg`             | Memory-efficient union aggregate                                                               |

`ST_CoverageClean` is available as of DuckDB spatial 1.5.3 (used by `inputs.main`'s coverage-clean pass and by `merge.main`). `ST_Snap(geom, snap_to, tolerance)` shipped in spatial 1.5.5, see "Snapping the Voronoi cell before `ST_Difference`" below for how `merge.main` uses it.

### Why the naive approach creates gaps

The previous merge used `ST_Difference(voronoi_cell, ST_Union_Agg(nearby_originals))` per cell. This recomputed the original polygon boundary independently for each Voronoi cell. GEOS floating-point arithmetic produces slightly different crossing-point coordinates each time, creating sub-nanometer seam gaps that appear as visible diagonal lines in QGIS.

Applying `ST_ReducePrecision` to only the extension pieces (not originals) makes the problem **worse**: it snaps extension vertices to a grid that doesn't align with the original polygon coordinates, increasing mismatches.

### The solution: `ST_Node` + `ST_Polygonize`

`merge.main` now:

1. Collects **all original polygon boundaries** (`ST_Boundary` of `_01`) and **all Voronoi cell boundaries** (`ST_Boundary` of `_04`) into one edge set.
2. Calls `ST_Node` on the combined edge set: every crossing point (where a Voronoi boundary crosses an original polygon edge) becomes a shared vertex in both geometries simultaneously. No crossing point is ever computed twice.
3. Calls `ST_Polygonize` on the noded edges: produces a clean planar partition of the entire extent with no gaps or overlaps.
4. Assigns each piece to a `fid` via `ST_PointOnSurface` + point-in-polygon: original polygon assignment takes priority (preserving authoritative boundaries exactly), complement pieces fall back to the enclosing Voronoi cell.
5. Unions pieces by `fid`.

This produces **0 gaps, 0 overlaps, 0 `ST_CoverageInvalidEdges`** on all tested datasets. Original polygon vertex coordinates are never modified: the noding only adds collinear intermediate vertices where Voronoi edges cross original polygon edges, which is geometrically identical.

### Topology checks (`_06_outputs.py`)

`check_valid_topology()` (`core/coverage.py`) runs on the final `_05` table and raises `RuntimeError` on failure: it chains `check_invalid_edges()` (`ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL`, via `has_invalid_edges`) then `check_gaps()` (via `has_gaps`). `has_invalid_edges` hands the validator only parts with another part within `SNAP_TOLERANCE` (`_near_part_pairs`): an isolated part can't hold an invalid edge, and GEOS rebuilds a large neighbour's index for every part near it (see `docs/adr/0141`). Both unnest MultiPolygon rows into single-polygon parts first, so a coverage split into multiple parts (e.g. mainland + offshore islet) doesn't hide a real interior-ring gap. There is no separate area-based check or epsilon-based warning tier; these are the only two topology gates in the pipeline.

`check_valid_topology()` defaults `gap_maximum_width` to `SNAP_TOLERANCE` (the same name GEOS's own `CoverageCleaner` uses, see `docs/adr/0002`), tolerating a wider hole: `topo-clean`/`edge-stitch` rely on this default, since a wider hole in their output may be a legitimate hole in the overlay layer's own shape (or a real gap left by design) rather than a defect (see `docs/adr/0035`, `docs/adr/0039`, `docs/dev/explanation/3-edge/edge_match.md`'s "Overlay-layer gaps"). `edge-extend` is the one exception, passing `gap_maximum_width=0` explicitly, so any interior hole of any size still raises: it has no overlay layer, so its own footprint is the only thing it's guaranteeing coverage of, and a leftover hole here is always a bug.

`has_gaps()` dumps the whole-table union into individual polygon parts before checking `ST_NumInteriorRings` on each. Calling `ST_NumInteriorRings` on the raw union result directly (the original implementation) is a latent bug: the union of any real multi-part dataset is almost always a `MultiPolygon`, and `ST_NumInteriorRings` silently returns `NULL` (never `> 0`) on a `MultiPolygon`, confirmed on `cod_admin4.parquet` (9,658 fids), where the undumped query returned `NULL` while the dump-then-max version correctly returned `0`. The undumped version would never have raised on a real interior-ring gap in any multi-part dataset, which is most real admin-boundary layers. The gap check unions a table that has passed the invalid-edge check with `ST_CoverageUnion_Agg`, and raw input with `ST_Union_Agg` (see `docs/adr/0143`).

---

## Micro-polygons

A polygon part at most `SNAP_TOLERANCE` wide is treated like a micro gap:
a defect, never real geography. At that scale it's a digitization or
processing artifact, typically a sliver fragment beside a polygon's large
part, or reprojection noise that turns a shared border into a sliver
overlap. A small but wider part, such as a real islet, is kept. Width
uses the maximum inscribed circle, the same measure as the micro-gap
check; `2 * area / perimeter` is a lower bound on that diameter, so it
cheaply skips every clearly wide part before the costly call.

Merging into the neighbour the buffered part overlaps most handles both
shapes of the defect: an overlap sliver goes to the polygon it overlaps
(no change in area), and a touching fragment goes to the polygon it
borders, which is usually its own large part. Dropping a touching
fragment instead would open a micro gap. Only an isolated fragment,
touching nothing, is dropped.

`ST_CoverageClean` absorbs micro parts at the default snapping distance,
but returns a polygon that is entirely micro as an EMPTY geometry,
`ST_CoverageInvalidEdges_Agg` doesn't flag a micro polygon that only
touches its neighbours, and its snap can narrow a part just over
`SNAP_TOLERANCE` wide to under it. `coverage_clean` therefore merges
micro-polygons before the clean and again on its own output, and
`has_valid_topology` checks for them directly, so an auto-cleaned input is
cleaned whenever it has one. See `docs/adr/0142`.
