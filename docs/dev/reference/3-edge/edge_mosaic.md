---
title: "edge-mosaic"
sidebar:
  order: 7
  badge:
    text: Draft
    variant: caution
---

See [edge-match](edge_match.md) for the rules `edge-mosaic` shares with it.

## Inputs

- `edge-mosaic` MUST load the input layer and the overlay layer raw,
  unlike `edge-extend`'s own inputs stage: neither is coverage-checked or
  -cleaned before assign/clip. The input layer is expected to already be a
  finished `edge_extend()` output, but `edge-mosaic` does not verify this (see
  `docs/dev/explanation/3-edge/edge_mosaic.md`).
- Unlike every other tool here, the input role MAY span multiple files
  (e.g. one `edge_extend()` output per country), combined internally. The
  overlay layer MUST remain a single file.
- The main output MUST NOT carry a `source_file` column; it is an
  internal working column only, used by `assign-one`'s per-file grouping
  (see `docs/dev/explanation/3-edge/assign.md`), not exported. The issues report MAY
  carry `source_file`, shortened to its parent directory plus filename
  (e.g. `sen/adm2.parquet`), never the full input path (see
  `docs/adr/0087`).

## Assigning input polygons to overlay polygons

- `edge-mosaic` MUST assign every input polygon from one input file to a single overlay polygon
  polygon, shared by the whole file: a file's input polygons are one group
  (e.g. one country's admin2 units), not independently routed to whichever
  overlay polygon each one individually overlaps most.
- The file's overlay polygon MUST be whichever overlay polygon the largest number of that
  file's input polygons intersect (a majority vote by count of intersecting
  input polygons, not summed overlap area), so a handful of border-overshooting
  input polygons cannot misassign a file whose other input polygons overwhelmingly
  point to their true overlay polygon.
- A tie between two candidate overlay polygons MUST be broken by the lower overlay polygon id.
- Once a file has a winning overlay polygon, every input polygon in that file MUST be
  assigned to it unconditionally, including an input polygon with zero individual
  overlap with the winner; such an input polygon is not dropped at assign time (see
  `docs/dev/explanation/3-edge/assign.md`). A whole file with no input polygon overlapping
  any overlay polygon at all MUST be dropped, not treated as fatal, and
  `edge-mosaic` MUST log a warning naming its input polygons, unless `merge`
  is set (see Configuration), in which case that file's own
  already-extended geometry is instead kept unclipped in the output.
- An overlay polygon matched by zero input polygons MUST be dropped, unless `merge`
  is set (see Configuration), in which case that overlay polygon's own geometry
  and attributes are kept unclipped in the output instead. Either case
  MUST also be recorded in the issues report described under Outputs.

## Clipping

- Before clipping any file, `edge-mosaic` MUST collect shared border points from
  every input file and the overlay tiles, then snap both the tiles and the input
  polygons to them (see `docs/adr/0144`).
- `edge-mosaic` MUST NOT re-run Voronoi extension on any input polygon; the input polygon
  layer is assumed already extended.
- `edge-mosaic` MUST clip each assigned input polygon to its own assigned overlay polygon's
  geometry via `ST_Intersection`, one distinct assigned overlay fid at a
  time, each in its own spawned OS subprocess (see
  `docs/dev/explanation/3-edge/edge_mosaic.md`).
- `edge-mosaic` MUST split each heavy overlay polygon into tiles with `ST_Subdivide`
  once, before the per-fid subprocesses, and MUST join input polygons to tiles by
  bbox comparison.
- An input polygon whose clipped result is empty MUST be dropped from the output,
  not treated as fatal, and MUST be recorded in the issues report as a
  `kind='clip-empty'` row (see Outputs).
- `edge-mosaic` MUST merge or keep every clip-detached piece in the clipped result, recording each one with an edge
  neighbour as a `kind='detached-part'` row.
- `edge-mosaic` MUST raise if zero input polygons were ever assigned to any overlay polygon,
  unless `merge` gap-filled at least one overlay polygon or kept at least one
  unmatched input file as passthrough (see Configuration).

## Invalid-edge check

- `edge-mosaic` MUST NOT run a coverage-clean pass over the clipped output.
- `edge-mosaic` MUST check every pair of touching parts from different output rows,
  passthrough rows excluded, for invalid edges. It MUST report each row pair with
  invalid edges as a `kind='invalid-edge'` row, and MUST NOT raise on or fix them.
- A check query MUST stop after `INVALID_EDGE_BATCH_SECONDS`, and its pairs MUST be
  reported as unchecked.

## Outputs

- `edge-mosaic` MUST NOT run the coverage check on its output. If the gap union
  rejects the output, one `kind='gap'` row with key `gap-check-failed` MUST be
  written instead of gap rows.
- `edge-mosaic` MUST export the final merged layer.
- `edge-mosaic` MUST also export an issues report alongside it, using the same columns as every other tool's issues report, listing every input polygon dropped
  for an empty clip intersection, every clip-detached piece with an edge neighbour, every
  unassigned/passthrough input file,
  every gap-filled/passthrough overlay polygon (when `merge` is set), and every
  leftover gap wider than `SNAP_TOLERANCE` whose interior point falls inside an
  overlay polygon the output was clipped to, so a human can audit what
  didn't make it into the output or what may need review.
- Without `merge`, a whole unmatched input file MUST appear as an
  `unassigned` row: `unit_a` MUST hold the input polygon's own fid and
  `source_file` MUST record its origin file as a parent-directory-plus-
  filename (not the full path); overlay polygon id and reason fields MUST be null.
  With `merge` set, that file's input polygons MUST instead appear as
  `passthrough` rows (same `unit_a`/`source_file` shape, `reason` null),
  and MUST NOT also appear as `unassigned`. For a `clip-empty` row,
  `unit_a` MUST hold the input polygon's fid, `overlay_fid` MUST hold its assigned
  overlay polygon's fid, `source_file` MUST record its origin file the same
  shortened way, and `reason` MUST explain that the clip intersection
  came back empty. For a `gap-fill` row (`merge` set only), `overlay_fid`
  MUST hold the gap-filled overlay polygon's fid and `reason` MUST explain that the
  overlay polygon had no matched input polygons and was kept unclipped in the output;
  `unit_a` and `source_file` MUST be null, since the row is the overlay polygon
  itself, not an input polygon. For a `gap` row, `area_m2`, `max_width_m`, and
  `thinness_ratio` MUST be populated instead. A field that doesn't apply
  to a row's kind MUST be null.
- `edge-mosaic` MUST produce the issues report only when it has at least one
  row; when it would be empty, no file MUST be written (and a stale file
  from a previous run at that path MUST be removed).

## Configuration (`api.edge_mosaic.mosaic()` / CLI)

- `edge-mosaic` MUST accept one or more input files and exactly one overlay
  file per call. The CLI additionally accepts `--input` (repeatable and
  comma-separable) alongside the glob-capable `INPUT_FILE` positional, both
  usable together, matching `edge-clip`'s own `--input` idiom.
- With a single input file, the output path MUST default to that input
  path with a `_mosaicked` suffix. With multiple input files, `output_path`
  MUST be given explicitly. The issues-report path MUST default to the
  output path with an `_issues` suffix.
- `edge-mosaic` MUST raise `FileExistsError` if either output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `assign`, `clip`, `outputs`; any
  other value MUST raise `ValueError`. `step` MUST be `None` whenever more than
  one `input_paths` file is given; any other value MUST raise `ValueError` (see
  `docs/adr/0079`).
- `edge-mosaic` MAY accept `match_column`/`overlay_match_column`/`input_match_column`
  to override spatial assignment with an exact code join (see `docs/dev/explanation/3-edge/assign.md`).
- `edge-mosaic` MAY accept `merge: bool = False` (CLI: `--merge`, a plain
  boolean flag): `False` (default) copies no overlay columns and drops
  both an unmatched overlay polygon and a whole unmatched input file; `True`
  copies every overlay column (excluding `fid`/`geom`) onto every matched
  input polygon, keeps an unmatched overlay polygon's own geometry unclipped in the
  output (`kind='gap-fill'`), and keeps a whole unmatched input file's own
  geometry unclipped in the output (`kind='passthrough'`). There is no
  way to enable one behavior without the other.
- With `merge` set, `edge-mosaic` MAY additionally accept
  `overlay_include`/`overlay_exclude` (CLI: `--overlay-include`/
  `--overlay-exclude`, each a comma-separated column list) to narrow which
  overlay columns get copied onto matched input polygons (default: every overlay
  column except `fid`/`geom`), and `input_include`/`input_exclude` (CLI:
  `--input-include`/`--input-exclude`) to narrow which of the input layer's own
  columns survive in the output (default: every input column;
  `fid`/`geom`/`source_file` are always force-kept regardless). Each pair
  is mutually exclusive with itself; an overlay-side flag MAY be combined
  with an input-side flag. All four MUST raise `ValueError` if given
  without `merge`.
- With `merge` set, `edge-mosaic` MAY additionally accept `prefer:
  "overlay" | "input" | None = None` (CLI: `--prefer`) to auto-resolve a
  real overlay/input column-name collision: `"overlay"` keeps the overlay layer's
  column and drops the input layer's, `"input"` does the reverse. Omitting
  `prefer` (the default) preserves raising `ValueError` on a real
  collision. `prefer` MUST raise `ValueError` if given without `merge`,
  or combined with any of `overlay_include`/`overlay_exclude`/
  `input_include`/`input_exclude` (see `docs/adr/0077`, `docs/adr/0079`, `docs/adr/0083`, `docs/adr/0088`;
  supersedes the input-orphan passthrough of `docs/adr/0078`).
- `edge-mosaic` MAY opt into cascading admin-hierarchy columns via
  `fill_schema`/`--fill-schema`, right after clipping and before export
  (both the single-file step loop and the multi-file combine path).
  `name_field`/`code_field`/`--name-field`/`--code-field` (given together
  or both omitted; omitted falls back to structural auto-detection) and
  `depth_column`/`--depth-column` (default `adm_lvl`) narrow it; all MUST
  raise `ValueError` if given without `fill_schema=True`.
  `edge-mosaic` MUST raise `ValueError` if `depth_column` already names an
  existing column when `fill_schema` is set. `fill_schema` is independent
  of `merge`: it fills a per-row schema-depth gap left by the input data
  itself, while `merge`'s own gap-fill (`fill_unmatched_overlays()`, see
  `docs/adr/0083`) fills a per-overlay geometry-coverage gap left by the
  mosaic; the two compose freely (see `docs/adr/0095`).
- `edge-mosaic` MAY accept `original_paths` (CLI: `--original`,
  repeatable and comma-separable, env `ORIGINAL_FILES`), one or more
  pre-extension originals covering the input files, in any supported
  format or as URLs. Without them, no clip-detached piece merges.

## Examples

### Example 1: re-clip a pre-extended layer against a new overlay boundary, explicit output

    topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson adm3_mosaicked.parquet

### Example 2: custom issues report path

    topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson adm3_mosaicked.parquet \
      --issues-file mosaic_report.parquet

### Example 3: combine multiple pre-extended input files, then re-clip

`--input` MAY be repeated and/or comma-separated.

    topo-tools edge-mosaic afg.parquet world_adm0.geojson out.parquet \
      --input ago.parquet,are.parquet

### Example 4: cascade admin-hierarchy columns and stamp each row's depth before export

    topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson adm3_mosaicked.parquet --fill-schema
