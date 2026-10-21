# CLAUDE.md

## Project Overview

`topo-tools` is a Python package of DuckDB-powered geospatial topology utilities,
`pip install`-able and importable, mirroring the organization of the sister JS app
at `../topo-tools-js` (a DuckDB-WASM web app with the same tools). It ships twenty-two
tools, all used for improving administrative boundary datasets and matching
sub-national boundaries to national boundaries (import-linter contracts
governing which tool may depend on which are in `docs/dev/shared.md`).
Tools are named `{group}-{verb}`: **edge** (boundary-fitting between layers),
**topo** (single-layer topology defects), **schema** (column crosswalking and hierarchy checks),
**name** (unit-name checks) and **package** (cartographic derivatives for web maps); `change` and bare
`package` stand alone for now, as do `code-detect`, `code-create`, `code-update` and `validate`. Four
are primitives, each standalone AND reused internally by the composite tools
below them:

- **edge-extend**: extends polygon boundaries outward using Voronoi diagrams, producing a complete coverage layer that fills gaps (coastlines, disputed areas, water bodies).
- **edge-clip**: assigns each input polygon to its overlay polygon (always `assign-one`), then clips it to that overlay polygon's geometry, one `overlay_fid` at a time in its own subprocess; a strict one-input-file/one-overlay-file/one-output primitive (`edge-mosaic` owns batching many input files against one shared overlay load, see `docs/adr/0080`). See `docs/dev/explanation/3-edge/edge_clip.md`.
- **edge-stitch**: closes seams in an already-tiled layer with one whole-table `ST_CoverageClean` pass. See `docs/dev/explanation/3-edge/edge_stitch.md`.
- **topo-detect**: scans a single polygon layer for gap/overlap coverage defects and reports them, without fixing anything. See `docs/dev/explanation/2-topology/topo_detect.md`.
- **package-polygons**: dissolves a polygon layer into every detected coarser admin level in a single call, levels auto-detected structurally by default (or via an explicit `--name-field`/`--code-field` pair), reusing the `core.dissolve` leaf's stage functions directly, once per level. See `docs/dev/explanation/6-packaging/package_polygons.md`.
- **package-points**: one label point per admin unit per detected level, using the pole of inaccessibility (always interior, unlike a centroid), combined into a single output file. See `docs/dev/explanation/6-packaging/package_points.md`.
- **package-lines**: one deduplicated line network of admin boundaries (shared between two units, or exterior to all of them), tagged by adjacency and the coarsest admin level each segment belongs to. See `docs/dev/explanation/6-packaging/package_lines.md`.
- **package**: runs `package-polygons`, `package-points`, and `package-lines` against one input in a single call, for the common case of wanting the full cartographic bundle at once. See `docs/dev/explanation/6-packaging/package.md`.
- **edge-match**: `--assign auto|one|many` (default `auto`: `assign-one`, forcing the whole input file onto one majority-vote overlay polygon, switching to per-polygon `assign-many` when fewer than half the input polygons overlap the winner, the mode used always logged; see `docs/adr/0082`) → per-group `edge-extend` (own subprocess) → batched `edge-clip` → `edge-stitch`, fitting an input layer into a coarser overlay layer (e.g. admin4 into admin0). The input role MAY span multiple raw files per call, combined via a memory-bounded per-file `inputs`+`assign` loop, groups/clip/stitch/outputs running once over the combined result so cross-file input polygons sharing an overlay polygon extend together (`--assign many`/`step` rejected outright for a multi-file call, `auto` runs `one`, see `docs/adr/0084`). Also accepts an opt-in `--merge` (a plain boolean, plus `--overlay-include`/`--overlay-exclude`/`--input-include`/`--input-exclude`/`--prefer` narrowing flags, the same design `edge-mosaic` uses), which both groups every input polygon with no overlay polygon overlap at all into one orphan group of its own (sentinel `PASSTHROUGH_OVERLAY_FID`), extending it like any other group and keeping it unclipped in the output (materially weaker safety profile than `edge-mosaic`'s own input polygon passthrough, see `docs/adr/0081`), and gap-fills an overlay polygon matched by zero input polygons via the shared `fill_unmatched_overlays()` helper, identically to `edge-mosaic` (see `docs/adr/0088`). See `docs/dev/explanation/3-edge/edge_match.md`.
- **edge-mosaic**: `assign-one` → `edge-clip` → `edge-stitch`, fitting an already-extended input layer (a prior `edge_extend()` output) into a new/different overlay layer, skipping Voronoi extension entirely. See `docs/dev/explanation/3-edge/edge_mosaic.md`.
- **topo-clean**: `topo-detect` → fixes the reported coverage defects (gaps, overlaps) with `ST_CoverageClean`, reporting the fix outcome in the issues file for manual review. See `docs/dev/explanation/2-topology/topo_clean.md`.
- **name-detect**: checks a coded layer's unit names (blanks, placeholders, duplicates under a parent, encoding errors, invisible characters, spacing, case, mixed scripts) and writes a Parquet report, rule-based DuckDB SQL, never modifying the input. See `docs/dev/explanation/5-names/name_detect.md`.
- **name-clean**: `name-detect` → fixes only the safe name defects (spacing, invisible characters, NFC, verified encoding repairs) via `name-detect`'s own `name_clean()` macro, marking them `fixed` in the issues report; case, spelling and duplicates are left for review. See `docs/dev/explanation/5-names/name_clean.md`.
- **change**: compares an old/new polygon layer pair and classifies every unit (unchanged/renamed/modified/relocated/split/merge/complex/created/removed) via spatial overlap and optional code/name identity linking; writes a tabular changelog plus a colored spatial overlay layer. See `docs/dev/explanation/4-codes/change.md`.
- **schema-detect**: checks one admin layer's column schema and hierarchy nesting and writes a Parquet report, never modifying the input: levels that can't be resolved or are skipped, a code under several parents or a blank parent, columns set aside as coarser groupings, and level columns named or present unlike the other levels'. Level membership follows the code columns' shared naming (`adm{n}_pcode`) once structural detection finds one. See `docs/dev/explanation/1-schema/schema_detect.md`.
- **schema-map**: maps a source-column → target-schema crosswalk by inferring the admin hierarchy structurally (cardinality/containment, never column names) and classifying code vs. name by value shape, deterministically, no LLM, then renames/drops columns per it into a `_mapped` copy, writing the crosswalk CSV beside it. `--csv` applies a hand-edited crosswalk instead (columns in its row order), `--map-only` writes only the crosswalk (see `docs/adr/0116`). See `docs/dev/explanation/1-schema/schema_map.md`.
- **schema-fill**: stamps a new `adm_lvl` column (overridable via `--depth-column`) with each row's real depth, then cascades each admin-hierarchy column down to that depth, pinned per row so a genuine NULL at a row's own real depth is never backfilled from a shallower ancestor; levels derived structurally by default, or via an explicit `--name-field`/`--code-field` pair; run against an already-clipped/stitched layer, then `package-polygons` to dissolve every level normally. See `docs/dev/explanation/1-schema/schema_fill.md`.
- **schema-join**: copies a join layer's admin-hierarchy columns onto each input polygon it overlaps most (`core.assign`'s `assign_many`, per-polygon plurality), never touching geometry; a shared column that differs is kept on the input polygon with the join polygon's values added as the next free numbered sibling (`adm2_name1`), never raised on or overwritten (see `docs/adr/0109`); writes `no-overlap`/`low-overlap`/`value-mismatch` issue rows. See `docs/dev/explanation/1-schema/schema_join.md`.
- **code-detect**: checks a coded layer's codes and writes a Parquet report, rule-based DuckDB SQL, never modifying the input: blank codes, a code with several names, a finest-level code on several features (`split-unit` warning when they share name and parent), a code not starting with its parent's, and a code shaped unlike its level's (letters as `A`, digits as `9`), the last two only where 90% of a level's codes follow the rule. Owns `blank-code`/`name-conflict`, which `name-detect` doesn't report (see `docs/adr/0128`). See `docs/dev/explanation/4-codes/code_detect.md`.
- **code-create**: cold-starts a hierarchical code on a flat, finest-level input, levels resolved structurally by default (or via an explicit `--name-field`/`--code-field` pair), each level's units ranked under their parent and assigned a fresh sequential code in a configurable `--root-code`/`--delimiter`/`--min-width` format (`--min-width` one width, one per level, or `auto`, see `docs/adr/0123`; `core.code`, a shared leaf), with `--source-codes replace|embed|copy` discarding, embedding, or copying each level's existing source code (see `docs/adr/0122`). See `docs/dev/explanation/4-codes/code_create.md`, `docs/dev/explanation/4-codes/code.md`.
- **code-update**: reconciles an already-coded OLD layer against an uncoded NEW candidate, classifying every unit via `core.change`'s own engine and applying a changelog-driven retention policy (retain/replace/retire) per unit, cascading a changed parent's new code prefix down to every unchanged/renamed descendant. Format (`root_code`/`delimiter`/`min_width`, the width per level) auto-detects off OLD's own existing codes unless overridden. See `docs/dev/explanation/4-codes/code_update.md`, `docs/dev/explanation/4-codes/code.md`.
- **validate**: runs `schema-detect` → `topo-detect` → `code-detect` → `name-detect` against one layer through their own `api.*()` functions, each writing its own report, then writes a `_validate_summary.csv` with one row per stage, kind and severity; a stage that raises is recorded as a `failed` row and the rest still run, `code-detect`/`name-detect` are skipped when `schema-detect` reports `levels-undetected`, and the CLI exits 1 on any error row. See `docs/dev/explanation/6-packaging/validate.md`.

## Deployment Targets

The pipeline targets two memory-constrained environments: **DuckDB-WASM in
the browser** (no disk, JS heap only, and the Python pipeline documents the SQL
approach for eventual JS/TS porting) and **memory-limited containers**
(typically 2–4 GB RAM, no swap; pip-install this package into whatever image
you need, no Dockerfile ships here). Prefer approaches that minimize
intermediate materializations, avoid platform-specific calls (`os.sysconf`,
`/proc`, `subprocess`), and work with small buffer budgets.

## Architecture

Each tool's pipeline is a sequence of stages, each a standalone module in its
own `topo_tools/core/{tool}/` package. All stages of one `edge_extend()`/
`edge_match()` call share a single file-backed DuckDB connection; tables are
the IPC mechanism between stages (per-group subprocesses inside `edge-match`
are the one exception, see `docs/dev/explanation/3-edge/edge_match.md`). Three layers,
each with a specific job (mirroring `geoparquet-io`'s `core`/`api`/`cli`
split):

- `topo_tools/core/{edge_extend,assign,edge_clip,edge_stitch,topo_detect,dissolve,code_detect,schema_detect,schema_fill,schema_join,package_polygons,package_points,package_lines,edge_match,edge_mosaic,topo_clean,change,code_create,code_update,name_detect,name_clean,validate}/`:
  stage implementations. `core.edge_match`/`core.edge_mosaic` call
  `core.edge_clip`/`core.edge_stitch` stage functions directly (not through
  their own `api.*()`), the same pattern `core.edge_match` uses to call
  `core.edge_extend`'s stage functions directly, `core.topo_clean` uses to
  call `core.topo_detect`'s issue-detection stage function directly (see
  `docs/adr/0028`), `core.package_polygons`/`core.package_points`/
  `core.package_lines` use to call `core.dissolve`'s stage functions
  directly, once per detected admin level, and `core.code_update` uses to
  call `core.dissolve`'s, `core.change`'s, and `core.assign`'s stage
  functions directly (see `docs/adr/0103`). `core.assign` has no
  `api.*()`/CLI pipeline of its own, so it's called one layer up instead,
  directly from `api.edge_mosaic`/`api.edge_clip`/`api.edge_match`/
  `api.code_update`. `core.assign`/`core.edge_clip`/`core.edge_stitch`/
  `core.topo_detect`/`core.dissolve`/`core.code` are themselves neutral
  leaves, alongside `core.constants`/`core.coverage`/`core.io`/
  `core.duckdb_utils`/`core.units`/`core.admin_columns`; every tool package
  may import any of these twelve, none of them may import back, except `core.dissolve`'s one
  narrow, explicit carve-out below. `core.schema_map` is not a neutral leaf
  but MAY be imported by `core.code_detect`, `core.schema_detect`, `core.schema_fill`, `core.schema_join`,
  `core.dissolve`, `core.package_polygons`, `core.package_points`,
  `core.package_lines`,
  `core.code_create`, `core.code_update`, and `core.name_detect` specifically (the
  `name_field`/`code_field`/level-detection mechanism,
  `core/schema_map/_levels.py`, `core/schema_map/_level_columns.py`,
  `core/schema_map/_resolve_levels.py`), never the reverse (see `docs/adr/0075`,
  `docs/adr/0092`); `schema-fill` does not call `core.dissolve` itself, a
  caller runs `package-polygons` separately after filling (see
  `docs/dev/explanation/1-schema/schema_fill.md`).
- `topo_tools/api/{edge_extend,edge_clip,edge_stitch,topo_detect,code_detect,schema_detect,schema_map,schema_fill,schema_join,package_polygons,package_points,package_lines,package,edge_match,edge_mosaic,topo_clean,change,code_create,code_update,name_detect,name_clean,validate}.py`:
  public API functions; each chains its own tool's stages for exactly one
  file (or file pair) per call, except `edge-mosaic`'s and `edge-match`'s
  input roles, which MAY span multiple files (see
  `docs/dev/reference/3-edge/edge_mosaic.md`, `docs/dev/reference/3-edge/edge_match.md`).
  `edge-clip` is a strict one-input-file/one-overlay-file/one-output
  primitive (see `docs/adr/0080`).
- `topo_tools/cli/main.py`: the click CLI, mapping flags/env vars onto one
  `api.*()` call per invocation, one file (or pair) at a time, except
  `edge-mosaic`'s and `edge-match`'s input arguments (MAY be a glob pattern)
  and their `--input` options (repeatable, comma-splittable), no directory
  batching.

Import boundaries between these layers, and between tools, are mechanically
enforced by `pyproject.toml`'s import-linter contracts, see
`docs/dev/shared.md` for the MUST/MAY rules.

### Pipeline, Configuration & Table Naming

Each tool's stages are numbered modules in its own `topo_tools/core/{tool}/`
package (`_01_...py`, ...), each with its own docstring; behavior contracts
live in `docs/pages/{phase}/reference/{tool}.md`, stage-by-stage detail in
`docs/pages/{phase}/explanation/{tool}.md`.

Settings flow in as plain keyword arguments on each tool's own `api.*()`
function, mapped 1:1 from CLI flags/env vars. There's no module-level `argparse`/env
parsing anywhere. Common settings (`tmp_dir`, `threads`, `overwrite`,
`debug`, `step`) are in `docs/dev/shared.md`; per-tool
paths/arguments/`step` values are in `docs/pages/{phase}/reference/{tool}.md`. Pure
literals live in `topo_tools/core/{tool}/_constants.py` and
`topo_tools/core/constants.py`.

Tables are named `{name}_{stage}[suffix]`: no suffix means one persistent
table; a letter suffix (`_03a`, `_03b`) means multiple and **all** get a
letter, never one left bare; `_tmp{n}` is dropped before the function
returns, invisible unless `--debug`. Each tool uses its own `name` (e.g.
`{input}_edge_match`) so tools never collide on the same input/`tmp_dir`;
per-tool table names are in `docs/pages/{phase}/explanation/{tool}.md`.

### Key Patterns

- **DuckDB spatial extension** handles all geometry operations (`ST_*` functions), one file-backed connection per input file (`topo_tools/core/duckdb_utils.py`), returned as a `ProfiledConnection` proxy that logs timing/memory per query when `--debug` is set.
- **DuckDB tables as IPC**: stages read and write named tables on the shared connection; no Parquet between stages.
- **Topology validation** (`check_valid_topology()` in every tool's outputs stage, chaining `check_invalid_edges`/`check_gaps`, backed by `has_invalid_edges`/`has_gaps` in `topo_tools/core/coverage.py`) always unnests MultiPolygons first. No byte-exactness check, see the next bullet.
- **`has_gaps()`/`check_valid_topology()` default `gap_maximum_width` to `SNAP_TOLERANCE` (GEOS's own `CoverageCleaner` parameter name, see `docs/adr/0002`), tolerating a wider gap**: a wider leftover gap may be a real hole in the overlay layer's own shape (e.g. Lesotho inside South Africa), a real unfilled-by-design gap, or a real unbatched absence, not a defect, so `edge-match`/`edge-mosaic`/`topo-clean`/`edge-stitch` all rely on this default and report any such gap (for `edge-match`/`edge-mosaic`, only one inside an overlay polygon the output was clipped to) as a `kind='gap'` row in the issues report instead of raising (see `docs/adr/0035`, `docs/adr/0037`, `docs/adr/0038`, `docs/adr/0039`). `edge-extend` is the one outlier, passing `gap_maximum_width=0` explicitly for its zero-tolerance check: it has no overlay layer, so any gap in its own coverage is unambiguously a bug. `topo-clean`/`edge-match`/`edge-mosaic`/`edge-stitch` share one issues-table column schema and skip writing the file entirely when it would be empty (see `docs/adr/0035`, `docs/adr/0036`).
- **Geometry column names**: `geom` in DuckDB tables, `geometry` in final output, always its first column. `duckdb_memory()` profiling caveats are in `docs/dev/performance.md`.
- **`core.io.read_and_reproject()` raises `ValueError` on invalid source geometry `ST_MakeValid` can't repair, and on a 0-row read.** Re-export via `gdal vector convert` or `pyogrio` to GeoParquet/GPKG first as the workaround (see `docs/adr/0053`).
- **`_05_merge.py` joins against nearby originals via bbox-prefiltered, part-exploded join, never a global `ST_Union_Agg` operand** (`_02_lines.py`'s neighbor-union join and `_03_points.py`'s shared-boundary-zone difference both use whole-fid bboxes instead, not interchangeable with part-exploded). `_03_points.py` raises if its differencing drops a fid entirely, and `_06_outputs.py` raises if the extended geometry no longer covers its original footprint (`SNAP_TOLERANCE`-buffered `ST_Covers`). See `docs/adr/0001`, `docs/adr/0090`.
- **Never call `ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` inline inside a JOIN's `ON` clause**, can hang indefinitely on high-vertex-count tables. Precompute bbox columns on the joined table/CTE first, as `_05_merge.py` does (see `docs/adr/0014`).
- **`_04_voronoi.py` clips a Voronoi cell to valid WGS84 range (`-180/-90/180/90`) only when that cell's own bbox already exceeds it**, leaving every interior cell byte-identical to its unclipped geometry. A peripheral boundary point's cell is mathematically unbounded; GEOS closes it using an auto-sized envelope based on the whole point cloud's own extent, which overshoots valid range for a country-scale point cloud with a remote exclave or polar-adjacent territory (e.g. Chile's Isla de Pascua/Antártica Chilena, Indonesia's eastern Papua group). Clipping the whole raw diagram as one blob before the per-cell dump OOM's on Chile-scale input; clipping every cell unconditionally perturbs even well-within-bounds cells enough to break `edge-match`'s coverage-clean step. See `docs/adr/0096`.
- **Byte-exact preservation of original polygon vertices is not a goal.** `ST_CoverageClean` may shift any polygon's boundary, including previously-untouched ones (see `docs/dev/topology.md`).
- **`edge-match` reuses `edge-extend`'s stage functions per-group, each in an isolated subprocess** (GEOS's native heap isn't fully released between files). Two subprocess generations per call: per-group `edge-extend`, then a separate batched `edge-clip` pass (see `docs/adr/0020`, `docs/dev/explanation/3-edge/edge_match.md`).
- **`edge-mosaic` skips Voronoi extension entirely**, assuming the input layer is already a finished `edge_extend()` output; chains `assign-one` → `edge-clip` → `edge-stitch` directly, no per-group subprocess. `edge-mosaic` also accepts an opt-in `--merge` (a plain boolean, plus `--overlay-include`/`--overlay-exclude`/`--input-include`/`--input-exclude`/`--prefer` narrowing flags), which both keeps an overlay polygon matched by zero input polygons in the output unclipped (using the overlay polygon's own geometry, via the shared `fill_unmatched_overlays()` helper) and keeps a whole unmatched input file unclipped in the output, coupled with copying named overlay columns onto every matched input polygon; `edge-match` performs the identical pair of behaviors (see `docs/adr/0088`); `edge-clip` has no equivalent, since only `edge-mosaic`'s input polygons are guaranteed to already be a complete coverage layer (see `docs/adr/0079`, `docs/adr/0083`, superseding `docs/adr/0078`'s input-orphan passthrough). See `docs/dev/explanation/3-edge/edge_mosaic.md`. With more than one input file, `edge-mosaic` assigns/clips one input file at a time against a cached overlay-tile decomposition (the memory-safe pattern standalone `edge-clip` used before it reverted to a strict 1:1 primitive, see `docs/adr/0079`, `docs/adr/0080`).
- **`core/edge_clip/`'s `_engine.main()` clips one `overlay_fid` at a time, each in its own subprocess, boundary adaptively grid-tiled**, uniformly for every caller including `edge-match`; a bad `overlay_fid` aborts the whole run. Tile size derives from each overlay polygon's own vertex density (small overlay polygons skip tiling) (see `docs/adr/0015`, `docs/adr/0016`, `docs/adr/0017`).
- **Standalone `edge-clip` never expects `overlay_fid` on its input**; it always assigns internally via `assign-one` (`api.edge_clip.clip()` calling `core.assign.load_input`/`load_overlay`/`assign_one` directly, no local wrapper) before clipping, no strategy flag. `edge-match`/`edge-mosaic` are unaffected, they call `core.edge_clip._engine.main()` directly with their own already-tagged tables (see `docs/adr/0021`).
- **Standalone `edge-clip` is a strict one-input-file/one-overlay-file/one-output primitive**; batching many input files against one shared overlay load moved to `edge-mosaic` once it adopted the same memory-safe per-file-loop pattern (see `docs/adr/0080`, superseding `docs/adr/0022`/`docs/adr/0023`/`docs/adr/0024`). Its per-`overlay_fid` subprocess working directory (`core/edge_clip/_engine.py`) is always cleared before use regardless of `--debug`, since two callers can land on the same `overlay_fid` and collide on a leftover catalog otherwise (see `docs/adr/0025`).
- **Any manually-declared DuckDB table schema fed by `bbox_columns_sql()` MUST insert `BY NAME`, never positionally.** `bbox_columns_sql()`'s emitted column order (`xmin, xmax, ymin, ymax`) doesn't have to match a hand-written `CREATE TABLE`'s order, and a positional insert won't catch a mismatch, it silently swaps values (see `docs/adr/0026`).
- **`core/assign/`'s `assign-one` (per-file majority vote, forcing every input polygon in a file onto its winner unconditionally once one exists) is `edge-mosaic`'s only mode and `edge-match`'s `--assign one`, which its default `auto` runs unless under half the input polygons overlap the winner.** `assign-many` (per-polygon plurality, dropping a zero-overlap input polygon individually) is `edge-match`'s `--assign many`, for files whose input polygons genuinely scatter across multiple overlay polygons (e.g. a poorly-digitized admin4 layer). An input polygon forced onto its file's winner with zero individual overlap is not dropped at assign time; it still gets clipped and MAY drop later at clip time if its result is empty, reported as a `kind='clip-empty'` issue row (see `docs/dev/explanation/3-edge/assign.md`, `docs/adr/0019`, `docs/adr/0082`).
- **`core.topo_clean` depends only on the shared leaf modules and `core.topo_detect`, not `core.edge_extend`.** `topo-clean`'s issue detection was extracted into its own standalone `topo-detect` tool, the same primitive-extraction pattern as `assign`/`edge_clip`/`edge_stitch`; `topo_clean` calls `core.topo_detect`'s stage function directly (see `docs/adr/0028`, `docs/dev/explanation/2-topology/topo_clean.md`, `docs/dev/explanation/2-topology/topo_detect.md`).
- **`ST_CoverageClean`'s `gap_maximum_width` has no GEOS-native auto-fill default.** `topo-clean`'s `--gap-width auto` mode computes an explicit width from the widest thin detected gap; `all` mode uses a fixed `GAP_MAXIMUM_WIDTH_ALL_DEG = 360` sentinel (see `docs/adr/0002`).
- **`coverage_clean()` (`core/coverage.py`) must call `ST_CoverageClean` positionally, never via DuckDB's `:=` named-argument syntax.** DuckDB binds named arguments to compiled/extension scalar functions purely by position, silently discarding the name (see `docs/adr/0003`).
- **Micro-polygons (a polygon part at most `SNAP_TOLERANCE` wide) are a hard-gate defect, like micro gaps**: `check_valid_topology` raises on one, and `merge_micro_polygons()` (`core/coverage.py`) merges each into the neighbour its buffer overlaps most, or drops it if isolated, writing `micro-polygon` issue rows. `coverage_clean()` always runs it first, since `ST_CoverageClean` returns an all-micro feature as an EMPTY geometry; `edge-clip`'s output, `topo-clean`'s output and `package-*` input call it directly (see `docs/adr/0117`).
- **Notches (two units' unshared edges running within `NOTCH_SPACING / 8` for at least `NOTCH_MIN_SCORE` spacings) are closed by moving each flagged segment's endpoints exactly onto the other unit and merging any gap that encloses between the two into the unit sharing more of its border, never by snapping or a windowed `ST_CoverageClean`**: `close_notches()` (`core/coverage.py`) runs in `read_reproject_and_clean(fix_notches=True)` (`edge-extend`/`edge-match` inputs) and in `topo-clean`'s `_fix`, then the existing whole-file clean handles the rest; `topo-detect` reports `notch` rows with `near_length_m` (see `docs/adr/0132`).
- **`edge-stitch`'s `_02_clean` stage calls `coverage_clean_escalating()`, not `coverage_clean()` directly**, widening `snapping_distance` past `SNAP_TOLERANCE` one `SNAP_ESCALATION_STEP` at a time (up to `SNAP_ESCALATION_MAX_STEPS`) only if invalid edges remain after the first pass; every other `coverage_clean()` call site is unaffected (see `docs/adr/0089`, `docs/dev/explanation/3-edge/edge_stitch.md`).
- **`ST_Distance(GEOMETRY, GEOMETRY)` is unreliable for two disjoint polygons at small separations.** Use `ST_XMin`/`ST_XMax`/`ST_YMin`/`ST_YMax` extent comparisons or `ST_MaximumInscribedCircle` instead (see `docs/adr/0004`).
- **`topo_detect/_02_issues.py`'s per-detection-kind retry falls back to an empty result table (logged) if both attempts fail, rather than leaving the table missing.** Any new call site of `_detect_or_empty` must supply `empty_sql` (see `docs/adr/0005`).
- **`change`'s classification runs in Python (`core/change/_03_classify.py`), not SQL**, polygon-count-scaled, not vertex-scaled, unlike `edge-extend`/`topo-clean`'s work. See `docs/dev/explanation/4-codes/change.md`.
- **`change` always uses exact `ST_Intersection`, never point-sampling**, unlike the sister JS app's WASM-only-bug workaround. See `docs/dev/explanation/4-codes/change.md`.
- **`topo-clean`'s `--maximum-gap-width`/`--snapping-distance` are decimal degrees, not meters** (`_01` is always EPSG:4326). See `docs/dev/explanation/2-topology/topo_clean.md`.
- **A read-role file argument MAY be an `http://`/`https://` URL to a `.parquet` file**, resolved via `core.io.resolve_input_path()`/`input_basename()`, never plain `Path()` (which mangles a URL's `//`); output-role arguments always stay local paths (see `docs/adr/0043`).
- **`schema-map`'s apply stages (`core.schema_crosswalk`) reuse `core.schema_map`'s and `core.schema_refactor`'s stage functions directly rather than re-implementing matching or renaming**, the same primitive-reuse pattern as `topo-clean`/`topo-detect` (`docs/adr/0028`), but split across two independent `name` sub-namespaces (`{name}` for the mapping tables, `f"{name}_apply"` for the rename tables) since both hardcode `"{name}_02"` for different data. The apply namespace's `_01` table is a DuckDB **view** over the already-loaded input, not a copy, per this project's memory-constrained deployment targets; it MUST be dropped before any `DROP TABLE IF EXISTS` targets that name, since DuckDB raises a Catalog Error dropping a view as a table even with `IF EXISTS`. See `docs/dev/explanation/1-schema/schema_map.md`.
- **`schema-fill`/`package-polygons`/`package-points`/`package-lines`/`package` all detect every hierarchy level structurally by default (`core.schema_map`'s cardinality/containment matcher, no naming convention assumed), never a hardcoded column-naming convention or column count**, or via an explicit `name_field`/`code_field` pair (`--name-field`/`--code-field`, given together or not at all). Structural auto-detection groups each level's columns by a shared naming anchor (`_level_anchors()`, `core/schema_map/_level_columns.py`): the text one column per level shares with every other level's, as a common prefix and (of what's left) a common suffix, picking whichever column per level shares the most, so the differing middle part (a digit like `1`/`2`/`3`, or a whole word like `state`/`county`) can anchor a sibling column at either end (`is_level_identity_column()`), never assuming a digit or a fixed position. `schema-fill` stamps a depth column (`adm_lvl` by default, overridable via `depth_column`/`--depth-column`) from the *original* (pre-fill) code columns so a caller can tell a genuine leaf-depth row from one only ever filled down; every level past a row's own stamped depth is pinned to that row's own value at (or nearest below) that depth, per row, never a shared file-wide level, so a genuine NULL at a row's own real depth is left untouched rather than backfilled from a shallower ancestor (see `docs/adr/0075`, `docs/adr/0093`); `package-polygons`'s own unmodified per-level `core.dissolve` call then carries the depth column through automatically via its existing auto-keep-constant-column behavior.
- **`package-polygons`/`package-points`/`package-lines` reuse `core.dissolve`'s stage functions directly rather than re-implementing aggregation**, the same primitive-reuse pattern `edge-match` uses on `edge-extend` (see `docs/dev/explanation/6-packaging/package_polygons.md`, `docs/dev/explanation/6-packaging/package_points.md`, `docs/dev/explanation/6-packaging/package_lines.md`).
- **`core.code`'s next-available integer per parent is derived only from the codes passed in, never a persisted registry.** `code-update` seeds the next available number with every OLD code at that level, retained or retired, so a code is never reissued within or across consecutive releases; a code retired two or more releases back isn't tracked. Codes without a delimiter are lenient (a `modified` 1:1 match keeps its code), codes with one are strict (see `docs/adr/0126`, `docs/dev/explanation/4-codes/code.md`).

### Supported Formats

Input/output: GeoParquet (`.parquet`), GeoPackage (`.gpkg`), Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.

## Commands

```bash
# Install dependencies
uv sync

# Run the edge-extend tool (processes exactly one file per call)
uv run topo-tools edge-extend example.geojson
# equivalently: uv run python -m topo_tools edge-extend example.geojson

# Run the edge-match tool (fits an input layer into an overlay layer)
uv run topo-tools edge-match input.geojson overlay.geojson

# Run the edge-mosaic tool (re-clips an already-extended input layer into a new overlay layer)
uv run topo-tools edge-mosaic extended.parquet new_overlay.geojson

# Run edge-clip/edge-stitch/topo-detect standalone (the primitives edge-match/edge-mosaic/topo-clean chain internally)
uv run topo-tools edge-clip input.parquet overlay.geojson
uv run topo-tools edge-stitch tiled.geojson
uv run topo-tools topo-detect example.geojson

# Run the schema-detect tool (check column schema and hierarchy nesting, CSV issues report)
uv run topo-tools schema-detect admin3.parquet

# Run the schema-fill tool (fill down admin columns, stamp each row's real depth)
uv run topo-tools schema-fill admin4.geojson

# Run the schema-join tool (copy join-layer hierarchy columns onto each overlapping input polygon)
uv run topo-tools schema-join admin3.geojson admin2.geojson

# Run the package-polygons tool (dissolve into every detected coarser admin level)
uv run topo-tools package-polygons admin3.geojson

# Run the package-points/package-lines tools (label points / deduplicated boundary lines)
uv run topo-tools package-points admin3.geojson
uv run topo-tools package-lines admin3.geojson

# Run the package tool (package-polygons + package-points + package-lines in one call)
uv run topo-tools package admin3.geojson

# Override auto-detected level naming with an explicit pair (any tool above)
uv run topo-tools package-polygons admin3.geojson --name-field adm{n}_name --code-field adm{n}_pcode

# Run the topo-clean tool (topo-detect, then fix gaps+overlaps, reporting the outcome in the issues file)
uv run topo-tools topo-clean example.geojson

# Run the change tool (compares an old/new polygon layer pair)
uv run topo-tools change old.geojson new.geojson

# Run the schema-map tool (map a crosswalk and apply it), then re-apply a hand-edited crosswalk
uv run topo-tools schema-map example.geojson
uv run topo-tools schema-map example.geojson --csv example_crosswalk.csv

# Run the code-create tool (cold-start a hierarchical code, ranked per parent)
uv run topo-tools code-create admin2.geojson --root-code AFG --delimiter . --min-width 3

# Run the code-detect tool (check unit codes, CSV issues report)
uv run topo-tools code-detect admin3.parquet

# Run the code-update tool (reconcile an already-coded OLD layer against an uncoded NEW candidate)
uv run topo-tools code-update admin1_old.geojson admin1_new.geojson

# Run the name-detect tool (check unit names, CSV issues report)
uv run topo-tools name-detect admin3.parquet

# Run the name-clean tool (fix safe name defects, report the rest)
uv run topo-tools name-clean admin3.parquet

# Run the validate tool (every single-layer detect, plus a summary; exits 1 on errors)
uv run topo-tools validate admin3.parquet --output-dir checks

# Format and lint
uv run ruff format && uv run ruff check
```

Pre-commit hooks run `uv-sync`, `ruff-format`, and `ruff-check` automatically.

## Test Datasets

| Dataset | Use |
| --- | --- |
| **West Africa cluster** (`sen`/`gmb`/`gnb`/`gin`/`civ`/`gha`/`tgo`/`ben`, portolan `adm2`) | Mutually neighboring countries, used for single-file tool tests (edge-extend/edge-match/topo-clean/change) and edge-mosaic's and edge-match's multi-file combine tests |

A full portolan catalog (real, large-scale admin boundary data, multiple
countries and admin levels, some with multiple historical versions) is
available for at-scale/real-data stress testing beyond the cluster above:

- **Local copy**: `/Users/computer/GitHub/OCHA-DAP/hdx-scraper-cod-ab-global/portolan`
- **Live/canonical source**: [source.coop/hdx/cod-ab](https://source.coop/hdx/cod-ab),
  STAC root catalog at `https://data.source.coop/hdx/cod-ab/catalog.json`
  (`id: portolan`; per-country `child` links, e.g. `./chl/catalog.json`)

**HARD RULE: the portolan catalog (local copy or live source) is read-only.**
Never write, modify, move, or delete anything inside
`/Users/computer/GitHub/OCHA-DAP/hdx-scraper-cod-ab-global/portolan`. No
`--output-path`/`--overwrite`, no `--tmp-dir`, no `--debug` exports, nothing.
Only ever read from it. Every output/tmp/debug path for a portolan-sourced
test run must point outside the catalog (e.g. the session scratchpad or
`/tmp`).

See the `at-scale-testing` skill for the STAC layout and how to pick a
file (or an old/new comparison pair, for `change`) from the catalog.

## Reference Docs

- `docs/pages/{phase}/`: one folder per COD-AB release phase (`1-schema`, `2-topology`, `3-edge`, `4-codes`, `5-names`, `6-packaging`), each with `index.mdx` (the phase's overview), `how-to.md` (the phase's step) and `reference/{tool}.md` (options and examples per tool, generated from the CLI's `--help` by `.github/scripts/gen_reference.py`, never hand-edited; `--check` runs in pre-commit and CI); `change` sits in `4-codes`
- `docs/dev/` (unpublished, like `docs/adr/`): `reference/{phase}/{tool}.md` (behavior contract per tool, MUST/SHOULD/MAY), `explanation/{phase}/{tool}.md` (stage-by-stage detail, plus an `overview.md`; `3-edge` also has `assign.md` and `voronoi-memory.md` (per-file resampling distance and memory ceilings for `phl_admin3`/`idn_admin3`), and `3-edge/edge_match.md` has the `check_gaps` caveat), `tutorials/{phase}/` (walkthroughs not yet ready to publish), `shared.md` (rules and settings common to every tool, the MUST/SHOULD/MAY convention), `topology.md` (coverage-clean merge, micro-polygons), `performance.md` (thread-scaling benchmarks + the RTREE experiment)
- `docs/pages/` is published at <https://ocha-dap.github.io/topo-tools-js/tools/docs/> by topo-tools-js (Starlight); `.github/workflows/docs.yml` triggers that deploy on push to `main`, and `npm run dev` in a sibling topo-tools-js checkout previews it
- `.claude/skills/`: `publishing` (PyPI release via OIDC), `verify-duckdb-function` (DuckDB/spatial function lookup), `at-scale-testing` (portolan catalog layout, picking a test file/pair), `manual-dev-testing` (running any tool against your own data)
- `docs/adr/README.md`: how to decide ADR vs. `docs/dev/explanation/` vs. CLAUDE.md's Key Patterns; `docs/adr/` itself holds the decision records
