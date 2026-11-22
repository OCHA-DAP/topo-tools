---
title: "code-update"
sidebar:
  order: 11
  badge:
    text: Draft
    variant: caution
---

`code-update` reconciles an already-coded OLD layer against an uncoded
NEW candidate: classify what changed via `change`'s own engine, then
apply the standard's changelog-driven retention policy so a unit's code
survives, gets replaced, or retires. Seven stages, each a numbered module
in `topo_tools/core/code_update/`.

## `_01_inputs`/`_02_levels`: two independent resolutions, one shared format

OLD and NEW are loaded, reprojected, and coverage-cleaned independently
(`core.io.read_reproject_and_clean()`), NEW's uncleaned geometry kept as
`{name}_b_00` for `_07_outputs` to write. Their per-level code/name columns
are then resolved independently too, each via the identical explicit-
pair-or-structural-fallback contract `code-create` uses
(`docs/dev/explanation/4-codes/code_create.md`): `--code-field-a`/`--name-field-a`
for OLD, `--code-field-b`/`--name-field-b` for NEW. A resolved level with
no code column at all (only a name, `has_code=False`, see
`docs/adr/0106`) raises `ValueError`: neither side's resolution ever
creates a column, so a codeless level needs an explicit `--code-field-a`/
`-b` pointed at a real one. A level-count mismatch between the two
resolutions raises `ValueError` immediately, before any dissolve or
classify work starts, since a real level-count change (a new admin tier
added or dropped) needs a human decision, not an automatic pass that
would otherwise silently misalign levels by depth.

`core.code.detect_code_format()` runs against OLD's own resolved
**finest** level, never level 0: a root-only value like `AFG` has no
delimiter occurrence to infer anything from, while the finest level gives
the richest sample for both delimiter/root detection and each level's width
mode.
Each of `root_code`/`delimiter`/`min_width` falls back to the detected
value independently, only when that field itself wasn't explicitly given;
an explicit value is never overridden by detection, even if detection
would have inferred something different. When OLD's finest codes have no
non-alphanumeric character (`has_delimiter()`), or `delimiter=''` is given,
`detect_undelimited_format()` runs against every OLD level column instead:
a code without a delimiter can only be split by width, and each level's
width is the length it adds to its parent's code.

## `_03_dissolve`: independent per side, per level

OLD and NEW are dissolved independently at every level, reusing
`core.dissolve`'s stage function directly, exactly as `package-polygons`
does: OLD grouped by its own already-real code column, NEW grouped by its
own resolved (structural or explicit) raw column. There is no cross-side
dependency at this stage; classification, next, is what actually compares
the two.

## `_04_classify`: reusing `change` as a library, not a subprocess

Each level's two dissolved tables are copied into the exact table names
`core.change`'s own stage functions expect
(`{name}_chg_{n}_a_01`/`_b_01`), then `core.change._02_overlap.main()`
and `_03_classify.main()` run directly, in-process, against them.
`code-update` reads `_03_classify`'s own internal pair-level (`_03a`) and
per-fid membership (`_03b`) tables directly, never the public changelog
`_03c` output, since `_03c` drops the `a_fid`/`b_fid`/`cluster_id`
linkage this tool needs to drive per-unit code outcomes.

`code_col_a`/`code_col_b`/`name_col_a`/`name_col_b` default to that
level's own resolved code/name columns, but are independently
overridable. The default matters less than it first appears: OLD's
resolved code column already holds a *formatted* code (`AFG.001`) while
NEW's resolved code column is still a *raw, uncoded* source value
(`AFG.1_1`), so `link_by_code` rarely finds a match against the defaults
alone, since the two sides are in genuinely different formats. A
`relocated` unit (spatially disjoint from its OLD polygon, so overlap-
based linking alone can't catch it) depends on identity-linking to be
found at all, and without a real shared identifier it falls back to
`link_by_name`, more fragile since a name can change independently of a
relocation. `--code-column-a`/`-b` exist for the case where OLD/NEW do
share a genuine persistent source identifier distinct from either side's
code/name columns.

## `_05_reparent`: re-derived spatially, never trusted from an embedded column

For every level finer than the coarsest, each NEW unit's true current
parent is re-derived spatially against the immediately-coarser level's
NEW, already-dissolved units, via `core.assign.assign_many()` (per-child
best overlap), never read off a raw embedded parent column. A raw
parent-reference column goes stale exactly when the coarser unit was
itself split, merged, or relocated this same version, so trusting it
would silently misparent every child under a unit that no longer exists
under that identity.

## `_06_assign`: one retention policy, six shapes of change funneled through two code paths

Every relationship class reduces to one of two things happening to a
unit's code: it's **retained** (rewritten under a possibly-new parent
prefix, never re-ranked) or it's **replaced** (assigned fresh through the
same batched `assign_new_codes()` call `code-create` itself uses).

`unchanged` and `renamed` are retained, and so is `modified` when the
format has no delimiter: such codes carry no promise that the area is
identical, and keeping a 1:1 match's code avoids churn from re-digitising.
Retained codes go through
`rewrite_child_code(old_code, new_parent_code, fmt)`, which reattaches the OLD
code's own tail onto the (possibly new) parent prefix, a no-op
reconstruction when the parent didn't change and a genuine prefix
cascade when it did. This one function is what lets a `modified` parent's
untouched descendants inherit its new prefix without re-ranking or
touching their own tail integers or sibling order at all.

Every other class (`modified` with a delimiter, `relocated`, `created`,
`split`, `merge`, `complex`) funnels into one shared per-level batch: every new-code
request for that level, regardless of which relationship class produced
it, is collected into a single staging table and assigned in one
`assign_new_codes()` call, seeded with this level's retained codes plus
every OLD code at that level, retained or retired, so a code is never
reissued within or across consecutive releases; a code retired two or
more releases back isn't tracked (see `docs/adr/0126`).

`match_method` is collapsed per cluster via `_reduce_match_methods()`:
a cluster spanning exactly one old/new pair keeps that pair's own value
(`"spatial"` or `"identity"`) untouched; a cluster spanning multiple
linked pairs (`merge`, `complex`, or a `split`'s several children) unions
every linked pair's own method and joins with `"+"` only when the set is
genuinely mixed, so a uniformly-spatial merge still reports plain
`"spatial"`, not `"spatial+spatial"`.

`predecessor_code` stays a scalar column: `NULL` for `created` and for
every retained row, the one linked OLD code for `modified`/`relocated`,
the one shared OLD code for every `split` child, and `NULL` again on a
`merge`/`complex` survivor's own geometry row, since a scalar can't
losslessly hold more than one predecessor. Full N:M lineage for a
`merge`/`complex` cluster lives in the changelog instead (every retired
`old_code` row shares that cluster's own `cluster_id`), avoiding a
`LIST`-typed output column, which degrades badly on `.shp` export (no
array types, 10-character field limit).

## `_07_outputs`: writing under OLD's own column names

Each level's new code is written into the column name OLD's own
resolution used at that level, in place on the NEW-side finest table; if
NEW's own raw column at that level is differently named, it's left
completely untouched as an ordinary passthrough attribute rather than
being overwritten or dropped. This is what gives a caller naming
continuity across versions for free: nothing needs to be respecified to
keep `adm1_pcode` (or `state_code`, or any other established name)
looking the same after an update as it did before.

The `predecessor_field` column is populated only for the finest level's
own rows, computed from the finest level's own changelog entries and
joined back onto the finest table by raw NEW value before that raw
value's own column gets overwritten in the per-level loop below it (a
real ordering dependency when OLD's output column name and NEW's raw
column name happen to coincide).

The changelog is always written, even when it would be empty: a
`removed` code gets one row with `new_code=NULL`, a `created` code gets
one row with `old_code=NULL`, a `merge` gives N retired rows plus one new
row, and a `complex` cluster gives one row per actually-linked old/new
pair straight from `change`'s own pairwise table, never a full N×M
cross-product.

## Memory profile: level-major, not stage-major

`_03_dissolve` through `_06_assign` run as one per-level loop, ascending,
rather than four stages each looping over every level (`docs/adr/0105`).
A level's own OLD dissolve table drops once that level's `assign` step
reads it; its NEW dissolve table stays resident one extra iteration (the
next level's `reparent` step needs it as the parent geometry), then
drops. `_07_outputs` needs no dissolve table at all: each level's `fid ->
raw column value` mapping is captured into a small Python dict right
after that level's own dissolve runs, before its NEW dissolve table would
otherwise be dropped.

This bounds peak resident geometry to roughly two levels' dissolved
tables at a time, not every level simultaneously. It does not bound
`_01_inputs`'s own memory cost: coverage-cleaning the two full
finest-level input files (`core.io.read_reproject_and_clean()`, shared by
every tool that reads a layer) can itself peak several GB on a large,
topologically messy input, before any per-level work starts, a cost
orthogonal to `code-update`'s own per-level architecture.
