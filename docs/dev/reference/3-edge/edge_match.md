---
title: "edge-match"
sidebar:
  order: 6
  badge:
    text: Draft
    variant: caution
---

See [edge-extend](edge_extend.md) for the rules `edge-match` shares with it.

## Inputs

- `edge-match` MUST coverage-clean the input layer, the same way
  `edge-extend`'s own inputs stage does (see `docs/dev/reference/3-edge/edge_extend.md`),
  and MUST load the overlay layer raw, uncleaned, the same way
  `edge-mosaic`'s overlay load does (see `docs/adr/0086`).
- The input role MAY span multiple files (e.g. one raw admin boundary file
  per country), combined internally. The overlay layer MUST remain a
  single file.
- The main output MUST NOT carry a `source_file` column; it is an
  internal working column only, used by `assign-one`'s per-file grouping
  (see `docs/dev/explanation/3-edge/assign.md`), not exported. The issues report MAY
  carry `source_file`, shortened to its parent directory plus filename
  (e.g. `sen/adm2.parquet`), never the full input path (see
  `docs/adr/0087`).

## Assigning input polygons to overlay polygons

- Under `assign-one`, `edge-match` MUST assign every input polygon from the input file to a
  single overlay polygon shared by the whole file, chosen by majority vote
  of that file's input polygons (`assign-one`, see `docs/dev/explanation/3-edge/assign.md`);
  a tie between two candidate overlay polygons MUST be broken by the lower overlay polygon
  id. Once the file has a winner, every input polygon in it MUST be assigned to
  that overlay polygon unconditionally, including an input polygon with zero individual
  overlap with it; such an input polygon is not dropped here, but MAY still drop
  later at clip time if its extended geometry never reaches the overlay polygon
  (see Clipping), reported as a `kind='clip-empty'` issue row.
- `edge-match` MUST accept `assign` (CLI: `--assign`) of `auto`, `one` or
  `many`, defaulting to `auto`. `one` is `assign-one` above. `many`
  (`assign-many`) MUST instead assign each input polygon independently to the
  overlay polygon it shares the largest overlapping area with, so one input
  file's input polygons MAY scatter across many overlay polygons. `auto` MUST
  run `assign-one`, then switch to `assign-many` when fewer than half the
  assigned input polygons overlap their winner. `edge-match` MUST log which
  mode it ran.
- Under `assign-one`, a whole input file with no input polygon
  overlapping any overlay polygon at all MUST be dropped, not treated as fatal, and
  `edge-match` MUST log a warning naming its input polygons. Under `assign-many`
  (`many`), an individual input polygon with no overlap with any overlay polygon
  MUST be dropped the same way. Either case, unless `merge` is set (see
  Configuration), in which case the dropped input polygon(s) are instead
  grouped into one orphan group of their own and extended together (see
  "Extending each group"), kept unclipped in the output. Either case MUST
  also be recorded in the issues report described under Outputs.
- An overlay polygon matched by zero input polygons MUST be dropped, unless `merge` is
  set (see Configuration), in which case that overlay polygon's own geometry and
  attributes are kept unclipped in the output instead. This case MUST
  also be recorded in the issues report described under Outputs.

## Extending each group

- `edge-match` MUST group input polygons by their assigned overlay polygon, including a group
  of exactly one input polygon.
- For each group, `edge-match` MUST extend that group's input polygons alone
  (boundary extraction, point/Voronoi generation, merging; see
  `docs/dev/reference/3-edge/edge_extend.md`). Clipping to the group's overlay polygon happens later,
  batched across all groups (see Clipping below), not inside this step.
- Each group's extension MUST run in an isolated process, separate from
  every other group and from `edge-match`'s own process.
- A group whose extension fails MUST be dropped from the output, not
  treated as fatal to the whole run, and `edge-match` MUST log an error naming
  it, since this may signal a real data problem even though it isn't fatal.
  `edge-match` MUST raise only if every group fails to produce output. Every
  input polygon belonging to a failed group MUST be recorded in the issues report
  described under Outputs.

## Clipping

- Before clipping, `edge-match` MUST collect shared border points from every
  extended group and the overlay tiles, then snap both the tiles and the extended
  polygons to them (see `docs/adr/0144`).
- `edge-match` MUST clip every real group's reassembled, extended output to its
  own `overlay_fid`'s geometry, per `docs/dev/reference/3-edge/edge_clip.md`, one distinct
  `overlay_fid` at a time, each in its own spawned OS subprocess. The orphan
  group (`merge` set only) MUST NOT be clipped; it has no overlay polygon to clip
  against. An overlay polygon matched by zero input polygons (`merge` set only) MUST be
  appended to the clipped result afterward, via its own unclipped
  geometry, before the invalid-edge check.
- Unlike a failed group's extension, `edge-match` MUST raise immediately if any
  real `overlay_fid`'s clip subprocess fails, aborting the whole run rather than
  dropping just that group.
- `edge-match` MUST merge or keep every clip-detached piece in the clipped result, recording each one with an edge
  neighbour as a `kind='detached-part'` row.

## Invalid-edge check

- `edge-match` MUST NOT run a coverage-clean pass over the clipped output.
- `edge-match` MUST check every pair of touching parts from different output rows,
  rows of the orphan group (`PASSTHROUGH_OVERLAY_FID`) excluded, for invalid edges.
  It MUST report each row pair with invalid edges as a `kind='invalid-edge'` row,
  and MUST NOT raise on or fix them.
- A check query MUST stop after `INVALID_EDGE_BATCH_SECONDS`, and its pairs MUST be
  reported as unchecked.

## Outputs

- `edge-match` MUST NOT run the coverage check or a gap check on its output.
- `edge-match` MUST export the final merged layer.
- `edge-match` MUST also export an issues report alongside it, using the same columns as every other tool's issues report, listing every dropped input polygon, every
  input polygon belonging to a dropped group, every input polygon dropped for an empty
  clip intersection, every clip-detached piece with an edge neighbour, and every passthrough input polygon and gap-filled overlay polygon
  (`merge` set only), so a human can audit what didn't make it into the output or what may
  need review.
- For an `unassigned`/`dropped_group`/`clip-empty`/`passthrough` row,
  `source_file` MUST record the input polygon's own origin file as a
  parent-directory-plus-filename (not the full path). For a `gap-fill` row,
  `source_file` MUST be null, since it has no single originating input file.
- For an `unassigned`/`dropped_group`/`clip-empty`/`passthrough` row,
  `unit_a` MUST hold the input polygon's own fid; for a `dropped_group` row,
  `overlay_fid` and `reason` MUST record the group's assigned overlay polygon and
  drop reason. For a `clip-empty` row, `overlay_fid` MUST hold the input polygon's
  assigned overlay polygon's fid and `reason` MUST explain that the clip
  intersection came back empty. For a `passthrough` row, `reason` MUST
  explain that the input polygon had no overlapping overlay polygon and was extended alone
  and kept unclipped in the output; a passthrough input polygon MUST NOT also
  appear as an `unassigned` row. For a `gap-fill` row, `overlay_fid` MUST
  hold the gap-filled overlay polygon's fid and `reason` MUST explain that the
  overlay polygon had no matched input polygons and was kept unclipped in the output;
  `unit_a` MUST be null, since the row is the overlay polygon itself, not an input polygon.
  A field that doesn't apply to a row's kind MUST be null.
- `edge-match` MUST produce the issues report only when it has at least one
  row; when it would be empty, no file MUST be written (and a stale file
  from a previous run at that path MUST be removed).

## Configuration (`api.edge_match.match()` / CLI)

- `edge-match` MUST accept one or more input files and exactly one overlay
  file per call. The CLI additionally accepts `--input` (repeatable and
  comma-separable) alongside the glob-capable `INPUT_FILE` positional, both
  usable together, matching `edge-mosaic`'s own `--input` idiom.
- With a single input file, the output path MUST default to that input
  path with a `_matched` suffix. With multiple input files, `output_path`
  MUST be given explicitly. The issues-report path MUST default to the
  output path with an `_issues` suffix.
- `edge-match` MUST raise `FileExistsError` if either output path already
  exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `assign`, `groups`, `clip`,
  `outputs`; any other value MUST raise `ValueError`. `step`
  MUST be `None` whenever more than one input file is given; any other
  value MUST raise `ValueError` (see `docs/adr/0084`).
- `edge-match` MAY accept `match_column`/`overlay_match_column`/`input_match_column`
  to override spatial assignment with an exact code join (see `docs/dev/explanation/3-edge/assign.md`).
- With more than one input file, `auto` MUST run `one`, and `many` MUST
  raise `ValueError` (see `docs/adr/0084`).
- `edge-match` MAY accept `merge: bool = False` (CLI: `--merge`, a plain
  boolean flag): `False` (default) copies no overlay columns and drops
  both an unmatched input polygon and an unmatched overlay polygon; `True` copies every
  overlay column (excluding `fid`/`geom`) onto every matched input polygon, keeps
  an unmatched input polygon's own extended geometry in the output unclipped
  (`kind='passthrough'`), and keeps an unmatched overlay polygon's own geometry
  in the output unclipped (`kind='gap-fill'`). There is no way to enable
  one behavior without the other.
- With `merge` set, `edge-match` MAY additionally accept
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
- With `merge` set, `edge-match` MAY additionally accept `prefer:
  "overlay" | "input" | None = None` (CLI: `--prefer`) to auto-resolve a
  real overlay/input column-name collision: `"overlay"` keeps the overlay layer's
  column and drops the input layer's, `"input"` does the reverse. Omitting
  `prefer` (the default) preserves raising `ValueError` on a real
  collision. `prefer` MUST raise `ValueError` if given without `merge`,
  or combined with any of `overlay_include`/`overlay_exclude`/
  `input_include`/`input_exclude` (see `docs/adr/0077`, `docs/adr/0081`, `docs/adr/0088`).
- `edge-match` MAY opt into cascading admin-hierarchy columns via
  `fill_schema`/`--fill-schema`, right after clipping and before export
  (both the single-file step loop and the multi-file combine path).
  `name_field`/`code_field`/`--name-field`/`--code-field` (given together
  or both omitted; omitted falls back to structural auto-detection) and
  `depth_column`/`--depth-column` (default `adm_lvl`) narrow it; all MUST
  raise `ValueError` if given without `fill_schema=True`.
  `edge-match` MUST raise `ValueError` if `depth_column` already names an
  existing column when `fill_schema` is set. `fill_schema` is independent
  of `merge`: it fills a per-row schema-depth gap left by the input data
  itself, while `merge`'s own gap-fill (`fill_unmatched_overlays()`, see
  `docs/adr/0083`) fills a per-overlay geometry-coverage gap left by the
  match; the two compose freely (see `docs/adr/0095`).

## Examples

### Example 1: fit an admin4 layer into a single country boundary, output name chosen automatically

    topo-tools edge-match adm4.geojson adm0.geojson

### Example 2: fit admin3 into admin2 groups, explicit output

    topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg

### Example 3: custom issues report path

    topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg \
      --issues-file match_report.gpkg

### Example 4: cascade admin-hierarchy columns and stamp each row's depth before export

    topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg --fill-schema
