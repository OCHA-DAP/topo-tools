---
status: draft
title: "shared"
---

Rules here apply across more than one tool; a tool's own page references
this one by name instead of repeating them. Reference pages use RFC 2119
keywords:

- **MUST** / **MUST NOT**: required; a violation is a bug.
- **SHOULD** / **SHOULD NOT**: expected default behavior.
- **MAY**: explicitly allowed, not required.

Tests enforce a subset of these rules, and a code change must keep both
in sync.

## Import boundaries (mechanically enforced)

- Core tool logic MUST NOT depend on the command-line interface.
- The public API layer MUST NOT depend on the command-line interface.
- The `edge-match` tool MAY reuse `edge-extend`'s logic; `edge-extend` MUST NOT depend on
  `edge-match`.
- The `edge-mosaic` tool MUST NOT depend on `edge-extend` or `edge-match`, and neither
  MUST depend on `edge-mosaic` (see `docs/dev/explanation/3-edge/edge_mosaic.md`).
- The `topo-clean` tool MAY reuse `topo-detect`'s logic; `topo-detect` MUST NOT depend on
  `topo-clean` (see `docs/dev/explanation/2-topology/topo_detect.md`, `docs/adr/0028`).
- The shared constants, coverage-validation, file I/O, database-connection,
  units, assign, edge-clip, topo-detect, and edge-stitch helpers MUST NOT depend on any
  of the five tool packages (`edge-extend`, `edge-match`, `topo-clean`, `change`,
  `edge-mosaic`); they are leaf building blocks usable by all of them.
- The admin-column naming helpers (`core.admin_columns`) MUST NOT depend on
  any tool package.
- `schema-map`'s apply stages (`core.schema_crosswalk`) MAY reuse
  `core.schema_map`'s and `core.schema_refactor`'s logic directly; neither
  of those MUST depend on `core.schema_crosswalk`, or on each other (see
  `docs/dev/explanation/1-schema/schema_map.md`).
- `schema-detect`, `schema-fill`, `schema-join`, `package-polygons`,
  `package-points`, and `package-lines` MAY all depend on `schema-map`'s
  `name_field`/`code_field`/level-detection helpers
  (`core/schema_map/_levels.py`, `core/schema_map/_level_columns.py`);
  `schema-map` MUST NOT depend on any of them (see `docs/adr/0075`,
  `docs/adr/0092`).
- `schema-join` MAY depend on `core.assign`'s stage functions directly, and
  MUST NOT depend on `edge-extend`, `edge-match`, `edge-mosaic`,
  `topo-clean`, or `change`; `core.assign` MUST NOT depend on `schema-join`.
- `package-polygons`, `package-points`, and `package-lines` MAY all depend
  on `core.dissolve`'s stage functions directly; `core.dissolve` MUST NOT
  depend on any of them.
- `edge-stitch`, `edge-match`, and `edge-mosaic` MAY opt into `schema-fill`'s
  fill logic from their own `api.*` layer only, via the private
  `api._schema_fill_compose` helper; `core.edge_stitch`, `core.edge_match`,
  and `core.edge_mosaic` themselves MUST NOT depend on `core.schema_fill`
  or `core.schema_map` (see `docs/adr/0095`).
- `core.code` (the shared code-format/cascade/rewrite primitive) MUST NOT
  depend on `code-create` or `code-update`.
- `code-detect`, `code-create` and `code-update` MAY depend on `schema_map`'s
  `name_field`/`code_field`/level-detection helpers; `schema_map` MUST NOT
  depend on either.
- `code-update` MAY depend on `core.dissolve`'s stage function directly;
  `core.dissolve` MUST NOT depend on `code-update`.
- `code-update` MAY depend on `core.change`'s overlap and classify stage
  functions directly; `core.change` MUST NOT depend on `code-update`.
- `code-update` MAY depend on `core.assign.assign_many()`; `core.assign`
  MUST NOT depend on `code-update`.
- `code-update` MUST NOT depend on `edge-extend`, `edge-match`,
  `edge-mosaic`, or `topo-clean`, and none of them MUST depend on
  `code-update`.
- `validate` MAY depend on every detect tool's `api.*.detect()`;
  `core.validate` MUST NOT depend on any tool package.

## Multi-file combine ordering

- `edge-stitch`, `edge-match`, and `edge-mosaic` MUST sort multi-file inputs
  by column count descending (ties by filename) before unioning/folding
  them. Output column order MUST NOT depend on caller-supplied file order
  (see `docs/adr/0094`).

## Output column and row order

- Every geometry output MUST write `geometry` as its first column (see
  `docs/adr/0111`).
- `schema-map` MUST order columns by the
  `name_field`/`code_field` templates: levels deepest first, and within
  each level its name-template columns, then any other column with that
  level's prefix and number, then its code-template columns, numbered
  siblings in numeric order. Every other column follows in input order.
  `schema-map` applying a crosswalk via `--csv` follows its row order instead
  (see `docs/dev/reference/1-schema/schema_map.md`).
- `schema-join` MUST keep the input layer's columns in input order. It MUST
  place each numbered sibling it adds right after the last existing column
  of that sibling's family, and every column absent from the input layer
  after all input columns, in template order (see `docs/adr/0119`).
- Every other tool MUST keep its input's column order. A column it adds
  MUST go next to its own level's columns: a code column seeded from names
  right after that level's name columns, and a numbered sibling right after
  its source column. A layer in `schema-map`'s order (levels finest first,
  names before codes within a level) stays in that order through every
  later tool.
- A numbered sibling of a column (a second same-level name, or a join layer's
  differing value in `schema-join`) MUST be named by appending an integer
  starting at 1, separated by `_` when the column ends in a digit
  (`adm2_name` → `adm2_name1`, `GID_2` → `GID_2_1`), so a sibling never
  reads as a deeper level.
- `schema-map` and `schema-join` MUST sort rows by the deepest level's own code column,
  by value (text codes as text), NULLs last, ties in input order.
- With no code-template column, rows MUST keep their input order, and the
  tool MUST log a warning. `schema-map` then keeps crosswalk-row column
  order.

## Geometry CRS

- Every tool MUST tag geometry `EPSG:4326` at read time (a CRS-less source is
  assumed to already be EPSG:4326), keep it tagged in every internal table,
  and write every Parquet file, intermediate or final, as GeoParquet V2.

## CSV outputs

- Every CSV a tool writes (`schema-map`'s crosswalk, `change`'s changelog,
  `code-create`'s issues, `code-update`'s changelog) MUST be UTF-8 with a
  byte-order mark, so spreadsheet apps detect the encoding.

## Coverage-topology checks

- The shared overlap/mismatched-edge check MUST NOT be treated as a gap
  check: it reports "no violations" both when a real, fully-enclosed gap
  exists with no overlaps, and when the data has collapsed to nothing.
- The shared gap check MUST detect fully-enclosed interior holes only, in
  the union of a layer's geometries.
- The shared gap check MAY be scoped to a maximum hole width: given one, it
  MUST report only holes at or below that width, treating a wider hole as a
  possible legitimate absence rather than a defect. Omitting the width MUST
  preserve the unscoped, any-size-hole behavior.

## Common settings

Every tool's public API function takes these in addition to its own
tool-specific settings (see the tool's own file for those):

- `tmp_dir`: intermediate DuckDB + Parquet location; MUST default to a
  fresh temporary directory when unset, and MUST be cleaned up after the
  call unless `debug` is set.
- `threads`: DuckDB thread count; unset MUST defer to DuckDB's own
  default.
- `overwrite`: whether to overwrite an existing output path; MUST default
  to `True`, logging `"overwriting existing output: {path}"` when it
  does; passing `False` (CLI: `--overwrite=false`) MUST raise
  `FileExistsError` instead if any output path already exists. Every
  `api.*()` function MUST route this check through
  `core.io.check_overwrite()` rather than an inline check, so the
  behavior stays in one place (see `docs/adr/0063`).
- `debug`: MUST keep intermediate tables, export all of them to Parquet,
  and log timing + memory delta per query.
- `step`: if given, MUST run only the one named stage; any value outside
  that tool's own stage names MUST raise `ValueError`.

A read-role file argument (input, overlay, join, old/new) MAY be an
`http://`/`https://` URL to a `.parquet` file, resolved via
`core.io.resolve_input_path()`/`input_basename()` (see `docs/adr/0043`);
behavior for a non-parquet remote URL is unverified. An output-role
argument (`output_path`, `issues_path`, `overlay_path`) MUST always be a
local filesystem path.

No module-level `argparse`/env parsing exists anywhere; settings flow in
as plain keyword arguments on each tool's own `api.*()` function, and the
CLI maps flags/env vars onto those same kwargs 1:1.

## Micro-polygons

- A micro-polygon is any single polygon part (after splitting
  MultiPolygons) whose maximum inscribed circle is at most
  `SNAP_TOLERANCE` across, the same width measure the micro-gap rule
  uses. A wider part MUST be kept, however small its area (e.g. a real
  islet).
- A tool that modifies geometry MUST NOT output a micro-polygon. Where it
  finds one, it MUST merge the part into the polygon whose non-micro part
  it overlaps most once buffered by `SNAP_TOLERANCE` (ties to the lowest
  fid, including the part's own polygon), or drop it when it touches no
  polygon. A polygon left with no parts MUST be removed.
- Every `coverage_clean` call merges micro-polygons before
  `ST_CoverageClean` runs, so `edge-extend`, `edge-stitch`, `edge-match`,
  `edge-mosaic`, `topo-clean` and every auto-cleaned input apply this
  rule. `edge-clip` applies it to its clipped output, `topo-clean` again
  after its fix, and `package-polygons`, `package-points` and
  `package-lines` to their input.
- Each merged or dropped part MUST be reported as a `micro-polygon` row
  (see Issues report schema) by every tool that writes an issues report.
- `schema-join` and `schema-map` MUST NOT apply this rule, since they
  never modify geometry. `change` applies it only through its auto-cleaned
  inputs; its overlay drops intersections below `SNAP_TOLERANCE` squared
  instead.

## Clip-detached pieces

- A clip-detached piece is any polygon part of a clipped polygon other than
  the kept piece of its own pre-clip part (the extended part holding the
  piece's interior point). The kept piece is the largest piece on the
  unit's original footprint, or the largest piece when none is.
- A piece is on the original footprint when its interior point falls on an
  original part of the same polygon, or when at least
  `DETACHED_MAX_ORIGINAL_SHARE` (50%) of its area is original land. An
  original polygon belongs to the pre-clip part holding its interior point.
- `edge-clip`, `edge-match` and `edge-mosaic` MUST merge a piece under
  `DETACHED_MERGE_MAX_RATIO` (1%) of its kept piece's area into the polygon,
  assigned to the same overlay polygon, it shares the longest edge with
  (ties to the lowest fid), when under 50% of the piece is original land or
  when the original land clipped away beside it is at least
  `DETACHED_MIN_NECK_RATIO` (0.1) of its area. Otherwise the piece MUST
  stay, reported as `kept: matches original shape`.
- Without an original layer, such a piece MUST stay, reported as
  `kept: no original layer`. `edge-match` always uses its own
  pre-extension input; `edge-clip` and `edge-mosaic` take one via
  `original_path`/`original_paths` (CLI: `--original`).
- A destination MUST be a kept piece, a single-part polygon, or a piece
  kept as too large. A point contact, or a neighbour that is any other
  clip-detached piece, MUST NOT count as sharing an edge.
- A piece MUST stay on its own polygon when it is 1% or larger, when it
  shares no edge with any destination, or when merging would leave the
  receiving polygon with an extra part.
- A piece that shares an edge with a same-overlay polygon MUST be reported
  as a `detached-part` row, merged or kept. A piece sharing no edge with any
  polygon MUST NOT be reported.
- In `edge-mosaic`'s per-file loop, only polygons from the same input file
  are candidate neighbours.

## Hard gates at each tool's output stage

- Every tool below that runs a topology hard gate MUST also raise if its
  final output has any micro-polygon.

- `edge-extend` MUST raise if its final output has any overlap or any gap of any
  size: it has no overlay layer, so any gap is unambiguously a defect
  in its own coverage (see `docs/adr/0035`).
- `edge-match` and `edge-mosaic` MUST raise if their final output has any overlap, or
  any gap at or below `SNAP_TOLERANCE`. A wider gap MUST NOT raise (see `docs/adr/0035`). One whose interior
  point falls inside an overlay polygon the output was clipped to MUST be
  logged as a warning and recorded in the issues report described in each
  tool's own file. Any other wider gap (a hole in the overlay layer itself,
  or an area no input was clipped into) MUST NOT be reported.
- `topo-clean` MUST raise if its final output has any overlap, or any unfilled
  gap at or below the `gap_maximum_width` actually used for that run (see
  `docs/adr/0037`). It MUST NOT raise over a gap wider than that: gaps
  above the requested fill width may legitimately remain by design and
  are only logged.
- `edge-stitch` MUST raise if its final output has any overlap, or any gap at
  or below `SNAP_TOLERANCE` (see `docs/adr/0038`). It MUST NOT raise over
  a wider gap, but MUST log a warning and record it in the issues report
  described in `docs/dev/reference/3-edge/edge_stitch.md`.
- `edge-clip` performs no topology hard gate at all: it clips an input polygon to its
  assigned overlay polygon's geometry one `overlay_fid` at a time and does not
  itself validate whole-layer coverage. It MAY still produce an issues
  report (a `clip-empty` row for any input polygon whose clip result was empty,
  a `detached-part` row for each clip-detached piece with an edge neighbour, plus `code-mismatch`/`code-fallback` rows when a match column is
  supplied, see below).
- `change` performs no topology hard gate at all; it is a read-only
  comparison between two inputs, not a fix.
- `topo-detect` performs no topology hard gate at all; it is a read-only
  inspection, not a fix.

## Issues report schema

`topo-clean`, `edge-match`, `edge-mosaic`, `edge-clip`, and `edge-stitch` each MAY
produce an issues report alongside their main output, sharing one column
schema: `key`, `kind`, `area_m2`, `max_width_m`, `thinness_ratio`,
`near_length_m`, `unit_a`, `unit_b`, `overlay_fid`, `reason`, `unit_a_area_change_m2`,
`unit_b_area_change_m2`, `filled_area_m2`, `fixed`, `source_file`, `geom`.
A tool MUST leave any column inapplicable to a given row's `kind` as null.
`unit_a` MUST record whichever single fid is primarily associated with the
row, for any kind that has one (a dropped input polygon, one side of an overlap,
etc.); `unit_b` MUST be used only where a second fid is meaningfully
involved (e.g. the other side of an overlap). `edge-match` MUST populate
`source_file` with the row's originating input file, shortened to its
parent directory plus filename (never the full input path), for every
kind that has one (`unassigned`, `dropped_group`, `clip-empty`,
`detached-part`, `passthrough`), null only for `gap` (see `docs/adr/0084`,
`docs/adr/0087`). `schema-detect`, `code-detect` and `name-detect` each
write their own schema (see their references), always, even with zero
rows, as CSV by default; `validate` summarizes all four reports in its own
schema (see its reference).

None of `edge-match`/`edge-mosaic`/`edge-clip`/`edge-stitch`'s *main*
output carries a `source_file` column at all, even though every one of
them tags it internally on the input table: it exists only to let
`assign-one` group a file's input polygons for its per-file majority vote (see
`docs/dev/explanation/3-edge/assign.md`), not as a user-facing column, and each
tool's outputs stage strips it before export (see `docs/adr/0087`).
`topo-clean`'s issues report keeps a `source_file` column for schema
compatibility, always null (it's a single-layer tool with no per-polygon
origin file).

`edge-match`, `edge-mosaic`, and `edge-clip` all share a `kind='clip-empty'`
row for any input polygon whose clip intersection with its assigned overlay polygon came
back empty (see `docs/adr/0082`): `unit_a` MUST hold the input polygon's fid,
`overlay_fid` its assigned overlay polygon's fid, `reason` MUST explain the
intersection was empty. `edge-mosaic` and `edge-match` both additionally
have a `kind='gap-fill'` row (see `docs/dev/reference/3-edge/edge_mosaic.md`,
`docs/dev/reference/3-edge/edge_match.md`) for an overlay polygon matched by zero input polygons,
kept unclipped in the output when `merge` is set: `overlay_fid` MUST hold
the gap-filled overlay polygon's fid, `unit_a` and `source_file` MUST be null (see
`docs/adr/0083`, `docs/adr/0088`).

A `kind='micro-polygon'` row (see Micro-polygons) MUST hold the part's
own polygon fid in `unit_a`, the receiving polygon's fid in `unit_b` (null
when dropped), `reason` MUST say whether it was merged or dropped,
`fixed` MUST be true, and `geom` MUST be the part itself. `topo-detect`
reports the same kind unfixed: `fixed` false, `unit_b` and `reason` null.

A `kind='detached-part'` row (see Clip-detached pieces) MUST hold the
piece's own polygon fid in `unit_a`, the fid of the polygon it shares the
longest edge with in `unit_b` (the receiving polygon when merged), the
piece's assigned overlay polygon in
`overlay_fid`, and the piece itself as `geom`, with `area_m2`,
`max_width_m` and `thinness_ratio` measured on the piece. `reason` MUST be
one of `merged into neighbouring polygon`, `kept: too large to merge`,
`kept: matches original shape`, `kept: no original layer` or
`kept: merge did not attach`, and `fixed` MUST be
true only for a merged piece.

A tool MUST NOT write an issues file at all when the run produced zero
issues rows; if a file already exists at the destination path from a
previous run, it MUST be deleted rather than left in place.

## Code-based assignment override

`edge-match`, `edge-mosaic`, and standalone `edge-clip` all MAY accept a `match_column`
name (same column on both layers) or an `overlay_match_column`/
`input_match_column` pair (different names), mutually exclusive with each
other; supplying only one of the pair MUST raise `ValueError`. When given,
`core/assign`'s exact code join wins over the
default spatial-overlap assignment wherever a code match exists, even when
it disagrees with the spatial result, and falls back to the spatial result
when an input polygon's (or, for `assign-one`, a file's) code has no
overlapping-overlay match at all (see `docs/adr/0045`,
`docs/dev/explanation/3-edge/assign.md`). Both outcomes MUST be recorded as issues
rows, reusing the schema above:

- `kind='code-mismatch'`: the code match won but disagreed with the spatial
  result. `unit_a` MUST hold the input polygon's own fid, `overlay_fid` the code
  match's overlay polygon.
- `kind='code-fallback'`: no code match existed; the spatial result was
  used instead. `unit_a` and `overlay_fid` MUST be populated the same way.

This gives standalone `edge-clip` its only issues-report capability: it produces
one only when `match_column`/`overlay_match_column`/`input_match_column` is
supplied and it yields at least one row (see `docs/dev/reference/3-edge/edge_clip.md`).

## Hierarchical code format and retention

`code-create` and `code-update` (`docs/dev/reference/4-codes/code_create.md`,
`docs/dev/reference/4-codes/code_update.md`) share one `CodeFormat` primitive
(`core.code`, `root_code`/`delimiter`/`min_width`, no default values) and
its supporting functions:

- `resolve_code_format(root_code, delimiter, min_width)` MUST raise
  `ValueError` unless `root_code` is non-empty, `delimiter` is exactly one
  character (or empty, when the caller allows it), and `min_width` is one
  positive width, a comma list of positive widths, or `auto`. `root_code` MUST NOT be
  shape-checked otherwise; a disputed-territory or other non-ISO3 string
  works identically to an ISO3 one.
- `assign_new_codes()` MUST always rank rows per parent into a fresh
  sequential integer before formatting; it MUST NOT reformat or pass
  through a raw source value as-is, since that value may be non-numeric,
  gappy, or duplicated across siblings.
- `CodeFormat.check_level_count()` MUST raise `ValueError` when a
  per-level `min_width` list doesn't have exactly one width per numbered
  level.
- A parent whose live/assigned child count exceeds `10 ** width - 1` (its
  level's fixed width; `auto` never overflows) MUST NOT have its
  already-assigned, lower-numbered children's codes repadded; an
  overflowing child's own tail component MUST simply grow past the width
  instead (`lpad` truncates an over-width string, unlike Python's `zfill`,
  so the target width is widened to the tail's own length first). With an
  empty delimiter, the overflow MUST raise `ValueError` instead.
- `next_available_integer()` MUST derive a parent's next unused integer
  only from a given list of currently-live codes, never a persisted
  registry. `code-update` MUST pass every OLD code at that level, retained
  or retired, so a code is never reissued within or across consecutive
  releases; a code retired two or more releases back isn't tracked (see
  `docs/adr/0126`).
- `detect_code_format()` MUST infer `delimiter` as the single
  non-alphanumeric character common to every sampled code, `root_code` as
  the shared first delimiter-split component, and `min_width` as the
  **mode** (most common), not the min or max, width at each level's
  component position, one width if every level agrees; it MUST raise `ValueError` if
  any of the three can't be confidently inferred, never falling back to a
  hardcoded literal.
- `rewrite_child_code(old_code, new_parent_code, fmt)` MUST reattach
  `old_code`'s own final (tail) component onto `new_parent_code`,
  unchanged otherwise.

## Overlay-column carry-forward

`edge-match`, `edge-mosaic`, and standalone `edge-clip` all MAY copy named
overlay-layer columns onto every matched input polygon. Names are always
caller-specified, never inferred from either layer's schema (see
`docs/adr/0077`). A name colliding with `core.assign`'s own reserved
columns (`input_fid`, `overlay_fid`, `assignment_method`, `spatial_agrees`)
MUST raise `ValueError`; a name colliding with the input layer's own
schema MUST also raise `ValueError` (an explicit pre-check, not left to
the SQL layer to reject on its own, see `docs/adr/0077`).

Standalone `edge-clip` exposes this as a plain `carry_columns` list (CLI:
repeatable, comma-splittable `--carry-column`), attribute-carrying only,
with no gap-fill concept of its own (`edge-clip` is a strict 1:1
primitive, see `docs/dev/reference/3-edge/edge_clip.md`).

`edge-mosaic` and `edge-match` both expose this as a plain boolean
`merge: bool = False` (CLI: `--merge`), coupled with two passthrough
mechanisms rather than independent of them: `False` (omitted) turns both
off; `True` carries every overlay column (excluding `fid`/`geom`) onto
every matched input polygon, keeps an overlay polygon matched by zero input polygons unclipped in
the output using its own geometry (`kind='gap-fill'`), and keeps a whole
unmatched input file unclipped in the output using its own geometry
(`kind='passthrough'`). `overlay_include`/`overlay_exclude`/
`input_include`/`input_exclude` (CLI: `--overlay-include`/
`--overlay-exclude`/`--input-include`/`--input-exclude`) narrow which
overlay/input columns survive; `prefer` (CLI: `--prefer [overlay|input]`)
auto-resolves a real overlay/input column-name collision instead of
raising. All five require `merge`; the four narrowing flags are each
mutually exclusive with their own pair, and mutually exclusive with
`prefer` (see `docs/adr/0079`, `docs/adr/0083`, `docs/adr/0088`). An input polygon
that never matched any overlay polygon (dropped as `unassigned`) never gains
carried columns through a join; a gap-filled overlay polygon's own row carries
them directly, since the row is the overlay polygon itself, not a joined input polygon
(see `docs/dev/reference/3-edge/edge_mosaic.md`).

The two tools' input passthrough implementations differ, since their
pipelines do: `edge-mosaic`'s passthrough geometry is already a finished,
validated `edge_extend()` output, unioned in directly. `edge-match`'s
passthrough groups every zero-overlap input polygon (whole file under
`assign-one`, individual input polygon under `assign-many`,
see `docs/dev/explanation/3-edge/assign.md`) into one orphan group of its own and
extends it fresh, alone, with zero neighboring-overlay context and no
majority/plurality vote to catch a bad extension, a materially weaker
safety profile than `edge-mosaic`'s (see `docs/adr/0081`). Overlay
gap-fill has no such asymmetry: both tools call the same shared
`core.assign.fill_unmatched_overlays()` helper (see `docs/adr/0088`).
