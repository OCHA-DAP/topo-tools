---
title: "code-update"
sidebar:
  order: 5
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `code-update` MUST read, reproject to EPSG:4326, and coverage-clean both
  OLD (already coded) and NEW (uncoded candidate) inputs via
  `core.io.read_reproject_and_clean()`, and MUST match every OLD and NEW
  row, including a row the clean removes, writing every NEW row with its
  uncleaned input geometry.

## Level resolution and format detection

- `code-update` MUST resolve OLD's and NEW's own per-level code/name
  columns independently, each via the same explicit-pair-or-structural-
  fallback contract `code-create` uses (`docs/dev/reference/4-codes/code_create.md`):
  `name_field_a`/`code_field_a` (or structural auto-detection) for OLD,
  `name_field_b`/`code_field_b` (or structural auto-detection) for NEW.
- `code-update` MUST raise `ValueError` ("no existing code column to
  overwrite") if either side resolves a level with no code column at all
  (e.g. a trailing finest level with only a name column, see
  `docs/adr/0106`), rather than silently skipping that level. With
  explicit templates, a level-0 code column MUST be left untouched, and a
  NEW level with a name column but no code column MUST get one seeded from
  its names, prefixed by its parent's code so same-named units under
  different parents stay apart. At the finest level, a name repeating
  under one parent MUST raise `ValueError`.
- `code-update` MUST raise `ValueError` if any row on either side has no
  code (or, for a seeded level, no name) at any level, or if a code has
  more than one name.
- `code-update` MUST raise `ValueError` in structural mode on either side
  ("group units like a level") if detection sets a column aside as a
  supplemental coarser grouping whose groups cluster on the map
  (`_spatially_coherent()`, taken as true without geometry or under 10
  rows), and ("a coarser level merged into this one") if any member of a
  level's group-by has over 30% fewer values than its code under each
  parent, rather than coding a merged or skipped level (see
  `docs/adr/0121`, `docs/adr/0131`).
- `code-update` MUST raise `ValueError` ("level mismatch") if OLD's and
  NEW's resolved level counts differ, before any dissolve/classify stage
  runs: a real level-count change needs a human decision, not an automatic
  pass.
- `code-update` MUST detect `root_code`/`delimiter`/`min_width` via
  `core.code.detect_code_format()`, run against OLD's own resolved
  finest-level code column, whenever any of the three is omitted; each
  field independently falls back to the detected value only when that
  field itself is `None`, an explicitly given field is never overridden by
  detection. An explicit `min_width` MAY be one width, a comma list with
  exactly one width per level (coarsest first), or `auto`, as in
  `code-create`; a detected one is per level when OLD's levels differ.
- When OLD's finest codes contain no non-alphanumeric character, or
  `delimiter=''` is given, `code-update` MUST detect the format from OLD's
  per-level code columns instead (`detect_undelimited_format()`):
  `root_code` as level 1's shared leading non-digit run, and one width per
  level from the length each level adds to its parent's code, raising
  `ValueError` on a missing root or mixed widths. `min_width='auto'` with
  an empty delimiter MUST raise `ValueError`.

## Dissolve

- `code-update` MUST dissolve OLD and NEW independently at every resolved
  level, reusing `core.dissolve`'s stage function directly: OLD grouped by
  its own already-real code column, NEW grouped by its own resolved
  (structural or explicit) code column.

## Classify

- `code-update` MUST classify every level by reusing `core.change`'s own
  overlap and classify stage functions directly against that level's two
  dissolved tables, producing every `relationship_class` `change` defines:
  `unchanged`, `renamed`, `modified`, `relocated`, `split`, `merge`,
  `complex`, `created`, `removed`.
- `tau_match`, `tau_same`, `link_by_code`, `link_by_name`, and `link_mode`
  MUST be plain passthrough to `change`'s own classification engine, same
  names and defaults (`tau_match=0.8`, `tau_same=0.98`,
  `link_by_code=False`, `link_by_name=False`, `link_mode="either"`).
- `code_column_a`/`code_column_b`/`name_column_a`/`name_column_b` (reusing
  `change`'s own flag names) MUST default to that level's own resolved
  code/name columns, and MUST be independently overridable, for identity-
  linking a case overlap-based linking can't catch on its own (e.g. a
  `relocated` unit).

## Reparent

- For every level finer than the coarsest, `code-update` MUST re-derive
  each NEW unit's true current parent spatially against the immediately-
  coarser level's NEW, already-dissolved units, via
  `core.assign.assign_many()` (per-child best overlap, not a shared
  majority vote); it MUST NOT trust a stale embedded parent column, since
  that column goes stale exactly when the coarser unit was itself split,
  merged, or relocated this version.

## Assign (retention policy)

Applied per level, ascending, using the level's own already-re-derived
parent codes:

| `relationship_class` | outcome |
|---|---|
| `unchanged`, `renamed` | code retained: `rewrite_child_code()` reattaches the OLD code's own tail onto the unit's (possibly new) parent prefix |
| `modified` | without a delimiter, code retained as for `unchanged`; with one, as for `relocated` |
| `relocated` | new code assigned under the re-derived parent; `predecessor_code` is the one linked OLD code |
| `created` | new code assigned; `predecessor_code` is `NULL` |
| `split` (1 OLD to N NEW) | each NEW unit gets its own new code; all share one `predecessor_code`, the one OLD code |
| `merge` (N OLD to 1 NEW) | the one NEW unit gets one new code; `predecessor_code` is `NULL` on that geometry row (a scalar can't hold N predecessors; full lineage lives in the changelog, see Outputs) |
| `complex` (N OLD to M NEW) | each NEW unit gets its own new code, one shared next-available scope per level; `predecessor_code` is `NULL` on every geometry row, same reasoning as `merge` |
| `removed` | no NEW-side output row; the OLD code is excluded from output, and no new code reuses it |

- A new code MUST be numbered above every code its parent had at that
  level in OLD, retired codes included, so no OLD code is ever issued to a
  different unit (see `docs/adr/0126`).
- Without a delimiter, when the next number above the parent's highest
  code doesn't fit the level's width, new codes MUST continue above its
  highest code below the top 10% of the range (`90` at width 2, `900` at
  width 3), where placeholders like `99` sit, raising `ValueError` if
  they'd reach it (see `docs/adr/0127`).
- A retained code rewritten under a new parent MUST NOT repeat any OLD
  code at that level or another retained code; that unit gets a new code
  instead (outcome `new`, `predecessor_code` its OLD code).
- A `modified` unit MUST keep its code (outcome `retained`, reason
  `geometry modified, code retained (lenient: codes without a delimiter)`)
  when the format has no delimiter, and MUST get a new code when it has
  one.
- `match_method` MUST be that one pair's own `change`-assigned value
  (`"spatial"` or `"identity"`) for a cluster spanning exactly one old/new
  pair; for a cluster spanning multiple linked pairs (`merge`, `complex`,
  or a `split`'s per-child links), every linked pair's own `match_method`
  MUST be collapsed into one value, joined with `"+"` when genuinely
  mixed.
- A `'new'`-outcome row's `code_outcome` MUST be overwritten to
  `'overflow'` (reusing `code-create`'s own `10 ** width - 1` capacity
  rule, never under `auto`) when its parent's total
  retained-plus-new child count at that level exceeds capacity; `reason`
  MUST be overwritten to state the overflow.

## Outputs

- `code-update` MUST write each level's new code into the column OLD's own
  resolution named at that level, in place on the NEW-side finest table.
  NEW's own raw column at that level, if differently named, MUST be left
  untouched as an ordinary passthrough attribute.
- With `code_field_a`, the output's level 0 code column (`code_field_a` at
  `n=0`) MUST hold `root_code` on every row, added after level 0's name
  column (else after level 1's columns) when missing.
- `code-update` MUST add a `predecessor_field` (default `predecessor_code`)
  column, populated only for the finest level's own rows.
- `code-update` MUST always write a changelog, even when it would be
  empty, no geometry column: `level`, `old_code`, `old_name`, `new_code`,
  `new_name`, `relationship_class`, `cluster_id`, `match_method`,
  `code_outcome` (`retained`/`new`/`retired`/`overflow`), `reason`. A
  `merge` gives N retired rows plus 1 new row; a `complex` cluster gives
  one row per actually-linked old/new pair from `change`'s own pairwise
  table, never a full N×M cross-product; a `removed` code gives one row
  with `new_code=NULL`; a `created` code gives one row with
  `old_code=NULL`.
- `code-update` performs no topology hard gate of its own beyond what
  `core.dissolve`'s stage function already enforces per level internally.

## Configuration (`api.code_update.code_update()` / CLI)

- `code-update` MUST process exactly one OLD/NEW file pair per call.
- `output_path`, if omitted, MUST default to `new_path` with a `_coded`
  stem suffix.
- `changelog_path`, if omitted, MUST default to `output_path` with a
  `_changelog` stem suffix and a `.csv` extension. It MUST be one of
  `core.code.TABLE_COPY_OPTS`'s extensions (a tabular format; the
  changelog has no geometry column), raising `ValueError` otherwise.
- `code-update` MUST raise `FileExistsError` for `output_path` or
  `changelog_path` if either already exists and overwriting wasn't
  requested.
- `link_mode` MUST be `"either"` or `"both"`; any other value MUST raise
  `ValueError`.
- `step`, if given, MUST be one of `inputs`, `levels`, `process`,
  `outputs`; any other value MUST raise `ValueError`. `process` runs
  dissolve, classify, reparent, and assign for every level, one level
  fully before the next (see `docs/adr/0105`).

## Examples

### Example 1: basic run, format auto-detected off OLD's own codes

    topo-tools code-update admin1_old.geojson admin1_new.geojson

### Example 2: identity-linking a relocated unit by source code

    topo-tools code-update old.gpkg new.gpkg --link-by-code \
      --code-column-a srcid --code-column-b srcid

### Example 3: explicit output paths, custom format override

Writes `admin1_new_coded.geojson` and `admin1_new_coded_changelog.csv`
unless given:

    topo-tools code-update old.parquet new.parquet coded.parquet changelog.csv \
      --root-code AFG --delimiter . --min-width 3
