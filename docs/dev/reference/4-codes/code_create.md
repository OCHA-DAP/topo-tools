---
title: "code-create"
sidebar:
  order: 4
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `code-create` MUST read the one input and reproject it to EPSG:4326 via
  `core.io.read_and_reproject()`. It takes exactly one flat input at the
  finest level, hierarchy embedded as columns (the same shape
  `schema-fill`/`package-polygons` expect).

## Level resolution

- `code-create` MUST resolve each level's own code column either via an
  explicit `name_field`/`code_field` pair (each an `{n}`-template, given
  together or not at all, raising `ValueError` if only one is given, the
  same contract `schema-map`/`package-polygons` use), or, when both are
  omitted, via structural auto-detection
  (`core.schema_map.detect_level_columns_or_single()`, cardinality/
  containment only, no naming convention assumed).
- `code-create` MUST raise `ValueError` ("no admin hierarchy level
  detected") if structural auto-detection finds zero levels.
- In structural mode, `code-create` MUST raise `ValueError` ("no existing
  code column to overwrite") if any resolved level has no code column at all
  (e.g. a trailing finest level with only a name column, see
  `docs/adr/0106`), rather than silently skipping that level or overwriting
  its name column.
- With an explicit `name_field`/`code_field` pair, a level with a name
  column but no code column, or a code column with no non-blank value,
  MUST get a code column seeded from its names; a level with neither MUST
  raise `ValueError`.
- When the finest level's codes are seeded from names, `code-create` MUST
  raise `ValueError` if a name repeats under one parent, since seeding
  would merge those units.
- `code-create` MUST raise `ValueError` in structural mode ("group units like a
  level") if detection sets a column aside as a supplemental coarser
  grouping whose groups cluster on the map (`_spatially_coherent()`, taken
  as true without geometry or under 10 rows), and ("a coarser level merged
  into this one") if any member of a level's group-by has over 30% fewer
  values than its code under each parent, rather than coding a merged or
  skipped level (see `docs/adr/0121`, `docs/adr/0131`).
- Every resolved level MUST be renumbered to a clean, relative `1..N`
  sequence, coarsest first; a genuinely constant coarsest column (e.g. a
  single-country file's own admin0 code) is dropped before reaching this
  step and never becomes a level.
- A source column that never resolves into a level MUST be left
  untouched. With `code_field`, level 0's code column MUST hold
  `root_code` (or the row's `root_code_field` value) on every row, added after level 0's name column (else after
  level 1's columns) when missing. Structural detection MUST NOT add a
  level 0 column.

## Assignment

- `source_codes` MUST be one of `replace` (default), `embed`, `copy`.
- Every level's code column MUST be read as text (an integer column cast
  to VARCHAR, a blank value treated as missing). A row of a source-coded
  level with no source code MUST raise `ValueError` under every mode,
  since ranking would merge every code-less unit under a parent into one.
- Under `replace` and `copy`, for each resolved level `1..N`, ascending,
  `code-create` MUST rank that level's own distinct code-column values
  under their immediately-coarser level's already-assigned code (or
  `root_code`, for level 1), sorted by their own raw, pre-assignment value,
  and overwrite the column in place with a freshly assigned, sequential,
  zero-padded code (`core.code.assign_new_codes()`).
- Under `replace` and `copy`, a level's raw source value MUST NOT be reused
  as-is (zero-padded or passed through unchanged): it may be non-numeric,
  gappy, or duplicated across siblings, so every value is always re-ranked
  into a fresh sequential integer before formatting.
- Under `copy`, each level's source code column MUST be copied to its next
  free numbered sibling (`adm1_code` to `adm1_code1`), placed right after
  it, before assignment; a seeded level gets none, and level 0 gets one
  only when level 1 does. A seeded level's code column MUST be placed right
  after its level's name columns.
- Under `embed`, a level with a source code column MUST be coded as its
  parent's code (or `root_code`), then `delimiter`, then its own source
  value, and a seeded level is ranked as under `replace`. If every source
  code at a level starts with its parent's source code (or `root_code`, at
  level 1) and is longer than it, that prefix MUST be removed before
  embedding; if only some do, `embed` MUST raise `ValueError`. An integer
  source code column is the exception: its prefix MUST be removed only
  when every code starts with it and, with an empty `delimiter`, the
  remainders share one width, else its codes MUST be kept whole. With an
  empty `delimiter`, an integer level MUST then be left-padded with zeros
  to the larger of its `min_width` and its widest value, never truncated,
  and `embed` MUST raise `ValueError` if a text level's source codes differ
  in length (see `docs/adr/0122`, `docs/adr/0125`, `docs/adr/0135`).
- The sort key MUST be the resolved code column's own raw value; there is
  no COD-AB-specific multi-column tie-break (e.g. `srcid` then `name` then
  `name1`-`name3`).
- Each level's codes MUST be zero-padded to that level's own width:
  `min_width` itself, its entry in a per-level list, or under `auto` the
  widest tail the level needs (see `docs/adr/0123`).
- A parent whose child count exceeds `10 ** width - 1` (999 at width 3)
  MUST NOT have its already-assigned, lower-numbered children's codes
  repadded; the overflowing child's own tail simply grows past the width
  instead. With an empty delimiter and a fixed width, an overflowing parent
  MUST raise `ValueError` instead.

## Outputs

- `code-create` MUST export the finest-level table, every resolved
  level's code column overwritten in place, as the main output, same
  format as the input. It performs no topology hard gate: geometry is
  never modified, only attribute columns are rewritten.
- `code-create` MAY write an issues report when `issues_path` is given
  and a numbered level (any level with a fixed width, except a
  source-coded one under `embed`) has a parent over overflow capacity; it MUST delete any
  stale file already at that path when the run produces zero overflow
  rows. Schema: `kind` (`'digit-overflow'`), `level`, `parent_code`,
  `assigned_code` (the overflowing parent's highest code), `child_count`,
  `min_width` (that level's width), `reason`.

## Configuration (`api.code_create.code_create()` / CLI)

- `code-create` MUST process exactly one input file per call.
- Exactly one of `root_code` or `root_code_field` MUST be given, raising
  `ValueError` otherwise; `delimiter` and `min_width` MUST be given
  explicitly (no default), validated via `core.code.resolve_code_format()`:
  `root_code` non-empty, `delimiter` exactly one character, or empty,
  `min_width` one positive width, a comma list of
  positive widths with exactly one per numbered level (coarsest first),
  or `auto`. `root_code` is opaque, never shape-checked (a disputed-
  territory or otherwise non-ISO3 string works identically to an ISO3
  one).
- `root_code_field` (CLI: `--root-code-field`) MUST name an existing
  column, blank on no row, raising `ValueError` otherwise. Each row's own
  value is its root code: level 1 is ranked and numbered under it, so each
  root's units number from 1 exactly as a separate call per root would.
  The column MUST NOT become a level itself.
- `output_path`, if omitted, MUST default to `input_path` with a `_coded`
  stem suffix.
- `issues_path`, if omitted, MUST default to `output_path` with an
  `_issues` stem suffix and a `.parquet` extension. It MUST be one of
  `core.code.TABLE_COPY_OPTS`'s extensions (a tabular format; the issues
  report has no geometry column), raising `ValueError` otherwise.
- `code-create` MUST raise `FileExistsError` for `output_path` or
  `issues_path` if either already exists and overwriting wasn't requested.
- `step`, if given, MUST be one of `inputs`, `levels`, `assign`, `outputs`;
  any other value MUST raise `ValueError`.

## Examples

### Example 1: structural auto-detection, no code column exists yet

    topo-tools code-create admin2.geojson --root-code AFG --delimiter . --min-width 3

### Example 2: explicit level columns, ambiguous auto-detection

    topo-tools code-create admin2.geojson --root-code AFG --delimiter . --min-width 3 \
      --code-field adm{n}_code --name-field adm{n}_name

### Example 3: overflow issues report

Writes `admin2_coded.geojson` and, only if any parent exceeds `10 **
min_width - 1` children, `admin2_coded_issues.parquet`:

    topo-tools code-create admin2.geojson admin2_coded.geojson --root-code AFG --delimiter . --min-width 3

### Example 4: source codes embedded without a delimiter

    topo-tools code-create admin3.geojson --root-code XY --delimiter '' --min-width auto \
      --source-codes embed --code-field adm{n}_code --name-field adm{n}_name

### Example 5: source codes kept in sibling columns

    topo-tools code-create admin3.geojson --root-code XYZ --delimiter . --min-width 3 \
      --source-codes copy --code-field adm{n}_code --name-field adm{n}_name

### Example 6: one width per level

    topo-tools code-create admin3.geojson --root-code XYZ --delimiter . --min-width 2,3,4

### Example 7: one root code per country, from a column

    topo-tools code-create world_admin2.parquet --root-code-field iso3 --delimiter . --min-width 3
