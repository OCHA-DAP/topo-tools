# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- cod-ab metadata export accepts null source-layer URLs for data handed
  over without a download endpoint, retaining any available URLs and
  allowing an empty `source_url`.

## [0.17.0] - 2026-10-06

### Fixed

- topo-clean, edge-extend and edge-match: closing a notch keeps a vertex
  the unit shares with a third unit in place, so it never cuts a hole
  out of a unit, and fills a gap it encloses against a neighbouring unit.
- topo-clean marks a gap fixed only when no part of it stays open, reports
  any gap the fix opens, and measures `thin` mode's width after closing
  notches.
- schema-join places a column it adds within its own level (names before
  the level's code), and a level the input lacks before the coarser ones.

## [0.16.0] - 2026-10-05

### Added

- code-create and code-update write the root code into level 0's code
  column (e.g. `adm0_code`) when a code template is given.

### Changed

- package-polygons, package-points, package-lines and package keep the
  input's column order on coarser levels.
- schema-fill keeps the input's column order, with the depth column last.
- code-create and code-update place a code column seeded from names right
  after its level's name columns.
- code-create's `--source-codes copy` keeps level 0's code in a sibling
  column only when level 1 gets one too.

## [0.15.1] - 2026-10-05

### Changed

- code-create: with `--source-codes embed` and no delimiter, whole-number
  source codes are zero-padded so each level has one width, and are
  numbered within their parent unless every code repeats the parent's
  code at one width.

### Fixed

- topo-detect: a notch on a reported gap or overlap is reported only as
  that gap or overlap, not also as a `notch` row.

## [0.15.0] - 2026-10-05

### Added

- cod-ab skill: records COD-AB metadata while processing (source, layer
  URLs, licence, level names, caveats) and writes `{iso3}_metadata.csv`
  with `scripts/metadata.py`; the columns are documented on the
  Packaging section's Metadata page.

### Changed

- Issues reports from `schema-detect`, `code-detect`, `name-detect`,
  `name-clean` and `code-create`, and every `validate` report, default to
  Parquet; pass a `.csv` name for a spreadsheet. `_validate_summary.csv`
  stays CSV.
- `code-detect`'s Parquet report carries each flagged unit's geometry.

## [0.14.0] - 2026-10-05

### Added

- `schema-detect`: checks one layer's level columns and hierarchy nesting
  and writes a report. Errors: `levels-undetected`, `level-skipped`,
  `multiple-parents`, `orphan-child`. Warnings: `supplemental-column`,
  `column-naming`, `column-set-mismatch`.
- `code-detect`: checks unit codes and writes a report. Errors:
  `blank-code`, `name-conflict` (both moved from `name-detect`),
  `duplicate-code`, `prefix-mismatch`. Warnings: `split-unit`,
  `format-outlier`, `format-undetected`.
- `validate`: runs `schema-detect`, `topo-detect`, `code-detect` and
  `name-detect` on one layer, writes each report plus
  `_validate_summary.csv`, and exits 1 on any error.
- Notches, where two neighbouring units' unshared edges run close together,
  are reported by `topo-detect` as `notch` rows and closed by `topo-clean`,
  `edge-extend` and `edge-match` by moving the flagged endpoints onto the
  other unit.
- `edge-match --assign auto|one|many`, defaulting to `auto`: assign-one,
  switching to assign-many when fewer than half the input polygons overlap
  the winner. Every run logs the mode used.
- A check that raises inside `schema-detect`, `code-detect` or
  `name-detect` writes a `check-failed` error row.
- cod-ab skill: runs without the plugin installed, from the skill's raw
  GitHub URL.

### Changed

- `--assign` replaces `edge-match --per-feature`; use `--assign many`
  (`assign="many"` in the API).
- `edge-match` and `edge-mosaic` report a leftover gap only when it lies
  inside an overlay polygon the output was clipped to, so a hole in the
  overlay layer itself isn't reported.
- Issue reasons, error messages, help text and docs say "polygon" instead of
  "feature" wherever the layer is always polygons, e.g. `merged into
  neighbouring polygon`.
- `code-detect` and `name-detect` run on layers with a supplemental grouping
  column, and `code-create`/`code-update` refuse only a grouping whose units
  cluster on the map.

### Fixed

- `coverage_clean()`, and every tool using it, gives the same output with
  any thread count; issue keys and row order are stable across runs.
- Names containing digits or a short root constant stay names in level
  detection, and a middle level is kept when constant columns join the root
  group.
- The Homebrew tap workflow bumps only to a newer release.

## [0.13.0] - 2026-10-02

### Added

- `name-detect`: checks a coded layer's unit names and writes a CSV report
  (or Parquet with geometry), always, even when clean. Errors: missing codes,
  blank or placeholder names, a code with more than one name, encoding
  errors. Warnings: duplicates under one parent, names differing only in
  case, accents or punctuation, invisible characters, NFC, spacing, all-caps
  outliers, mixed Latin/Cyrillic/Greek words, codes inside names. Safe fixes
  are given in `suggested`. Rule-based, no new dependency.
- `name-clean`: runs the `name-detect` checks and fixes only what can't
  change a name's meaning (spacing, invisible characters, NFC, encoding
  repairs that are certain), writing the cleaned layer and the report with
  a `fixed` column. Case, spelling and duplicates are left for review.

### Changed

- Documentation lives at <https://topo-tools.org/docs/>, and the Claude Code
  plugin marketplace at <https://topo-tools.org/marketplace.json>.
  `py.topo-tools.org` is retired.
- CLI `--help` text is rewritten in plain language, with more examples.
  Reference pages are generated from it.

## [0.12.0] - 2026-10-02

### Added

- `code-create --source-codes replace|embed|copy`: `embed` puts each level's
  source code after its parent's code, removing a parent prefix every code
  repeats, so `11`/`22`/`33` and `11`/`1122`/`112233` both give `XY112233`
  and existing p-codes come out unchanged. `copy` keeps the source code in
  a `*_code1` column next to the code column.
- `--delimiter ''` in both code tools, for ISO2-style codes such as `XY0101`.
  `code-update` detects the root and each level's width from the previous
  release's codes.
- `--min-width` accepts one width, one per level (`2,2,4`, coarsest first)
  or `auto`.
- With explicit `--name-field`/`--code-field`, a level with names but no
  code column gets codes numbered by name, in both code tools.
- cod-ab skill: stage 4 asks whether the codes are source codes to keep as
  given and picks `code-create` or `code-update` from the answer. Stage 3
  stops and reports units dropped by `edge-match`, and takes the reference
  admin0 from a shared `00_shared/` folder. A preview script renders issues
  and features over OpenStreetMap and EOxCloudless.

### Changed

- `code-refactor` is renamed `code-create` (CLI command, `api.code_create`,
  docs pages). There is no alias.
- `code-update` never reissues a code: new numbers start above every code
  a parent had in the previous release, retired codes included. A unit
  moved under a new parent whose rewritten code is already taken gets a new
  code. Without a delimiter, a `modified` 1:1 match keeps its code; with
  one, it gets a new code.
- Without a delimiter, new codes are numbered below the top 10% of a
  level's width (`90` to `99` at width 2), which is kept for placeholder
  codes such as a disputed area's `99`.
- The docs site uses the HDX theme and shows the OCHA logo.

### Fixed

- Both code tools raise on a missing code, a code with more than one name,
  same-named siblings when codes come from names, a level whose codes
  outgrow a fixed width without a delimiter, and a structurally detected
  merged or skipped level, instead of producing merged or wrong codes.
- `code-create` reads an integer code column as text.

## [0.11.0] - 2026-10-01

### Added

- `edge-clip`, `edge-match` and `edge-mosaic` merge a small clip-detached
  piece (under 1% of its source part's main piece) into the same-overlay
  feature it shares the longest edge with, when the pre-extension original
  doesn't draw it as part of the unit. `edge-match` checks against its own
  input; `edge-clip` and `edge-mosaic` take the original via a new
  `--original` option and, without it, only report such pieces. Every
  piece, merged or kept, is a `detached-part` row in the issues report.

### Changed

- COD-AB how-to and skill: stage 3 fits to the previous release's outline,
  dissolved from its admin1 in source.coop's matched catalog
  (`hdx/cod-ab/matched`), and stops to ask when there is no previous
  release. The how-to notes that the cleaning step can shift interior
  edges.
- The docs site is named Topology Tools, uses the web app's icon and
  links to the web app.
- Docs are grouped by the six COD-AB release phases (schema, topology,
  edge matching, codes, names, packaging), each holding its how-to step,
  tutorial, reference and explanation pages. The shared rules, topology
  and performance notes move to the unpublished `docs/dev/`. Page URLs
  change; update an installed `topo-tools` plugin so
  its COD-AB step links resolve.

### Removed

- `pyyaml` is not a dependency: no topo-tools code reads YAML.

## [0.10.2] - 2026-09-29

### Fixed

- `schema-map`: when a level has more than one code column, the one that
  contains its parent's code (`AF01` under `AF`) gets the bare
  `adm{n}_code`, and source-column order breaks ties. A surrogate ID listed
  before the p-code gets the numbered sibling.
- `cat` (GRASS GIS's category ID) is a noise column: `schema-map` leaves it
  out of the crosswalk, and `package-polygons`/dissolve drop it.

### Changed

- `schema-join` keeps the input layer's column order, so the order set by
  moving crosswalk rows in `schema-map` holds. A sibling it adds goes right
  after its own column, and new-level columns go last in template order.
- COD-AB how-to and skill: stage 1 leaves the admin0 file out, a per-level
  extra such as `adm2_type` is allowed, and every numbered sibling
  `schema-map` produces (another source's code or name, an older version,
  a code given a name target) is the reviewer's call.

## [0.10.1] - 2026-09-29

### Fixed

- Micro-polygons: a polygon part at most `SNAP_TOLERANCE` wide is a
  defect, like a micro gap. Every tool that modifies geometry merges each
  one into the neighbouring feature it overlaps most once buffered (often
  its own large part), or drops it when it touches nothing, and reports it
  as a `micro-polygon` issue row. `topo-detect` reports them, and the
  topology hard gate raises on any left in an output.
- A feature that was entirely micro-polygon came back from
  `ST_CoverageClean` as an empty-geometry row; it's now merged into its
  neighbour before cleaning. `edge-stitch` on a layer with such features
  writes fewer rows than before.
- Requires DuckDB 1.5.6, which fixes UNNEST pushdown, Top-N window
  elimination (`QUALIFY row_number()`) and UNION ALL subplan bugs in
  query shapes these tools use.

## [0.10.0] - 2026-09-28

### Changed

- Layer roles use QGIS terms: `edge-clip`, `edge-mosaic` and `edge-match`
  take an input and an overlay layer, `schema-join` an input and a join
  layer. There are no aliases for the old names.
  - CLI: the edge tools' `CLIP_FILE` positional is now `OVERLAY_FILE`,
    and `schema-join`'s `CHILD_FILE PARENT_FILE` is now
    `INPUT_FILE JOIN_FILE`. The env vars match.
  - API: `overlay_path` (was `clip_path`/`parent_path`), `input_path` (was
    `children_path`/`child_path`), and `schema_join.join()`'s `join_path`.
  - Flags (and matching kwargs/env vars): `--overlay-include`/`--overlay-exclude`,
    `--input-include`/`--input-exclude`, `--overlay-match-column`/
    `--input-match-column`, `--prefer overlay|input`, and `--per-feature`
    (was `--multi-parent`, kwarg `per_feature`).
  - Issues files: the edge tools' and `topo-clean`'s `parent_fid` column is
    now `overlay_fid`, and `schema-join`'s is `join_fid`. `schema-join`'s
    `no-parent` kind is now `no-overlap`, and its reason strings and the
    edge tools' `clip-empty`, gap-fill and code-mismatch reasons use the
    new terms.

## [0.9.0] - 2026-09-28

### Changed

- `schema-map` maps a crosswalk and applies it in one run by default,
  writing both the `_crosswalk.csv` and the `_mapped` layer. `--csv`
  applies an edited crosswalk (columns in its row order), and `--map-only`
  writes only the crosswalk. The crosswalk path moves from the second
  positional argument to `--csv-output`, and a `.csv` output path is
  rejected.

### Removed

- `schema-refactor` and `schema-crosswalk` (CLI and `topo_tools.api`). Use
  `schema-map --csv` and plain `schema-map`.

### Fixed

- `schema-map`, `change`, `code-refactor` and `code-update` write their CSV
  outputs as UTF-8 with a byte-order mark, so Excel opens non-ASCII names
  correctly. `schema-map` reads a crosswalk with or without one.
- Every command's `--help` prints its examples one per line, without
  literal `\b` markers.
- `cod-ab` plugin: `convert.py to-parquet` reads every source through
  pyogrio and accepts `--encoding` for a shapefile without a `.cpg` file,
  so CP1252 characters like `’` and `€` decode correctly on every OS. The
  source CRS, projected or from a GDB, is kept in the GeoParquet.

## [0.8.2] - 2026-09-25

### Fixed

- `schema-map`: a float or decimal column holding any fractional value
  (an area, a coordinate) is never mapped as a level or a sibling column,
  and falls through to `unmatched`.
- Structural level detection: a column that is NULL on some rows clusters
  with a level by 1:1 correspondence on the rows where both are populated,
  never by a shared digit in the column names. Sparse levels such as
  woredas, adminpoints hierarchies and capitals-only columns map to their
  own level instead of merging into a neighbor or splitting off.

## [0.8.1] - 2026-09-25

### Fixed

- Structural level detection: when a coarser grouping nests a level
  without the level's codes embedding it, the parent whose codes the level
  embeds takes the chain slot, so the real level keeps its own number
  instead of becoming the finer level's name siblings.
- Structural level detection: a grouping that nests a level without the
  level's codes embedding it (e.g. senatorial districts over LGAs), and
  alone breaks the column naming the rest of the chain shares, is reported
  as `supplemental` rather than taking a level of its own.
- `schema-join`: a shared column whose type differs between child and
  parent (e.g. INTEGER vs VARCHAR) is compared as text, rather than
  failing the whole join on a cast error.
- `schema-map`: a single-feature file's constant columns form one root
  level, rather than chaining one level per column (e.g. `area_sqkm` as
  `adm1_code` in an admin0 file).
- Input loading: geometry is repaired again after reprojecting to
  EPSG:4326, since reprojection can itself make a valid polygon invalid
  (e.g. a sliver with near-duplicate vertices), which crashed later GEOS
  operations with a `TopologyException`.
- `schema-map`: a blank value is never evidence that one column embeds
  another, so a column that's blank on every row (e.g. an empty
  `salb_id`) can't make every name look like a code.

## [0.8.0] - 2026-09-24

### Added

- `package-polygons`/`package`: `--output-name-field`/`--output-code-field`
  rename the name/code template columns (numbered siblings included) in
  every written level, e.g. `adm{n}_code` to `adm{n}_pcode`.

### Changed

- Every tool writes `geometry` as the first output column.
- `schema-refactor`/`schema-crosswalk`/`schema-join`: columns are ordered
  deepest level first, names before codes, and rows are sorted by the
  deepest level's code (#75). `schema-refactor` takes
  `--name-field`/`--code-field` to set the templates. `schema-join` issues'
  `unit_a` is the output row number, matching the code-sorted output.

### Fixed

- Structural level detection anchors each level on the columns that share
  the most naming text, so a level whose parent has only a name column
  (e.g. an admin2 with `adm1_name` but no `adm1_code`) keeps its own name
  columns. `schema-join` compares those columns against the child, and
  schema-fill/package/code tools see them as part of the level (#80).
- A numbered sibling of a column ending in a digit is `_`-separated
  (`GID_2_1`, not `GID_21`), so it can't read as level 21 under a template
  ending in `{n}` (e.g. GADM's `GID_{n}`). `schema-join` with
  `--name-field`/`--code-field` copies and compares the name template's
  columns even when its prefix differs from the code template's (#79).

## [0.7.2] - 2026-09-24

### Fixed

- `schema-map`/`schema-crosswalk`: with `--level` omitted, a file with no
  single-value country column numbers its coarsest level adm1, not adm0, and
  logs a warning. Pass `--level` for a multi-country file (#65).

## [0.7.1] - 2026-09-24

### Added

- `schema-map`/`schema-crosswalk` `--level`: numbers the finest detected
  level as the file's own admin level, for files with no country column
  (#65).

### Changed

- `topo-tools` plugin install switched from a git-clone-based marketplace to a
  generated URL-based one (`https://ocha-dap.github.io/topo-tools-py/marketplace.json`,
  zip archive source), so it works without git installed. See
  `docs/pages/agents/index.md` for a paste-in Claude Code prompt that installs
  it with marketplace auto-update enabled.

## [0.7.0] - 2026-09-17

### Added

- `code-refactor` (API and CLI): cold-starts a hierarchical code on a flat,
  finest-level input, levels resolved structurally by default (or via an
  explicit `--name-field`/`--code-field` pair), each level's units ranked
  under their parent and assigned a fresh sequential code in a
  configurable `--root-code`/`--delimiter`/`--min-width` format.
- `code-update` (API and CLI): reconciles an already-coded OLD layer
  against an uncoded NEW candidate, classifying every unit via `change`'s
  own engine and applying a changelog-driven retain/replace/retire policy
  per unit, cascading a changed parent's new code prefix down to every
  unchanged/renamed descendant. Format auto-detects off OLD's own existing
  codes unless overridden.
- `schema-map`'s structural level detection now finds a trailing, name-only
  finest level (no code column of its own); `code-refactor`/`code-update`
  raise `ValueError` for such a level instead of silently producing no
  code for it.

### Fixed

- `code-update`'s name-column resolution now uses a real `name_column`
  (from already-computed role info) instead of a positional guess, which
  could silently link on a numeric attribute column (e.g. `area_sqkm`)
  instead of the real name column for a single-level file.

## [0.6.0] - 2026-09-14

### Added

- `package-polygons`, `package-points`, `package-lines`, and `package`
  (API and CLI): a family of cartographic-derivative tools for web maps.
  `package-polygons` dissolves a polygon layer into every detected coarser
  admin level in one call; `package-points` produces one pole-of-
  inaccessibility label point per admin unit per level, with every level's
  own identity column (including a whole-table-constant root, e.g. a
  single-country file's `adm0_*`, dissolved to its own single row) landing
  under one name shared across every level (the source file's own naming
  convention, e.g. `pcode`, or an explicit schema's fixed `code`/`name`)
  rather than a level-numbered column like `adm1_pcode` ever surviving, and
  any attribute that can't generalize to a coarser level excluded rather
  than leaked as an impossible NULL; `package-lines` produces one
  deduplicated boundary-line network tagged by adjacency and admin depth,
  with each side's own detected identity families (e.g. `pcode`, `name`)
  exposed under single-letter-prefixed generic columns (`a_pcode`/
  `b_pcode`, `a_name`/`b_name`) rather than an untrustworthy
  auto-generated fid; a row is exterior exactly when its `b_*` columns are
  `NULL`, one level coarser than the coarsest detected level, with no
  separate `boundary_type` column. Levels are auto-detected structurally
  by default
  (`core.schema_map`'s cardinality/containment matcher, no naming
  convention assumed), or via an explicit `--name-field`/`--code-field`
  pair or target-schema YAML.
- `schema-fill` also gains structural auto-detection of every admin level
  and its own code column, replacing the requirement for an explicit
  target-schema YAML; `--name-field`/`--code-field`/`--target-schema`
  remain available to force a specific naming convention.
- `core.dissolve` (and every tool built on it) sums a numeric column that
  varies within a group instead of dropping it, unless overridden per
  column via `--aggregation column=function` (`sum`, `min`, `max`, `avg`,
  `first`).

### Fixed

- `schema-map`'s structural level detection no longer lets an audit/
  workflow column (e.g. `update_by`, `created_date`) outrank a file's real
  admin hierarchy: date/time columns are excluded from chain candidacy
  outright, a constant-root chain exemption only applies to an unbroken
  prefix from the file's own root, and both that exemption and a
  tolerance-based embedding check now require corroborating spatial
  coherence when geometry is loaded, closing gaps found in UNHCR's
  Moldova/Colombia/Ecuador/Tunisia/Greece/Sudan files.

### Removed

- **Breaking:** the standalone `dissolve` tool (API `dissolve()` and CLI
  `dissolve`) is removed, replaced by `package-polygons`. `--group-by`
  (arbitrary-column grouping) is dropped along with it: `package-polygons`
  always auto-detects every admin level in one call instead, structurally
  by default or via an explicit `--name-field`/`--code-field` pair or
  target-schema YAML. A caller using `dissolve --group-by
  adm2_pcode,adm1_pcode` should build a `schema-map` target-schema YAML
  for their column naming convention and run `package-polygons` instead,
  which produces every coarser level in one call rather than one
  `--group-by` at a time.

## [0.5.6] - 2026-09-01

### Fixed

- `edge-extend` no longer produces coordinates outside valid WGS84 range
  for a file with a peripheral boundary point far from the rest of its
  point cloud (e.g. a remote exclave or polar-adjacent territory): its
  Voronoi step now clips such a cell to `-180/-90/180/90` when its own
  bbox exceeds it.

## [0.5.5] - 2026-08-31

### Added

- `edge-stitch`, `edge-match`, and `edge-mosaic` (API and CLI) gain an
  opt-in `fill_schema`/`--fill-schema` flag, cascading admin-hierarchy
  columns and stamping each row's real depth right after their own merge,
  before export, reusing `schema-fill`'s own logic from the `api` layer.
  Narrow it with `target_schema_path`/`--target-schema` and
  `depth_column`/`--depth-column`.

### Fixed

- `schema-fill` (API and CLI) now pins the fill to each row's own real
  depth by default: a genuinely NULL value at a row's own real terminal
  level is left NULL rather than backfilled from a shallower ancestor.
  `cascade_from_level`/`--cascade-from-level` (never released) is removed;
  the pin is now automatic and per-row instead of a caller-supplied,
  file-wide reference level.
- `edge-stitch`, `edge-match`, and `edge-mosaic`'s multi-file combine step
  now sorts inputs by column count descending (ties by filename) before
  unioning/folding, so output column order no longer depends on
  caller-supplied file order.

## [0.5.4] - 2026-08-28

### Added

- `dissolve` (API and CLI) gains `exclude` and `target_schema` options
  that drop columns unconditionally, before the constancy auto-keep check
  runs. `target_schema` auto-derives every column at a level finer than
  `group_by`'s own detected level from a target-schema YAML.

## [0.5.3] - 2026-08-27

### Changed

- Build backend is now `hatchling` (pure Python), not `uv_build`, so building
  the package from source (Homebrew, and any other from-source packaging
  channel) no longer needs a Rust toolchain. `uv`-based workflows
  (`uv sync`/`uv build`/`uv run`/`uv tool install`) are unaffected.

### Fixed

- `pyproject.toml` now declares `license-files = ["LICENSE"]`, so `uv
  build`'s sdist actually includes the `LICENSE` file (it didn't before,
  breaking any packaging that reads a `license_file` out of the downloaded
  source, e.g. conda-forge's recipe format).

## [0.5.2] - 2026-08-26

### Fixed

- `fill_unmatched_parents()` (the `--merge` gap-fill step) now inserts
  one unmatched parent's geometry at a time instead of building a
  scratch table of every unmatched parent and rebuilding the whole
  result table in one `UNION ALL BY NAME`, fixing a memory blowup
  gap-filling against a true global-scale parent (verified against the
  real ~730MB fieldmaps global adm0 parent).
- `edge-match`'s and `edge-mosaic`'s multi-file paths no longer keep a
  redundant full copy of the parent table alive alongside an
  already-identical one, just to feed gap-fill.

## [0.5.1] - 2026-08-26

### Added

- `schema-fill` now folds level 0 into its detected/filled level range
  whenever the table has its own level-0 code column (e.g. `adm0_id`),
  falling back finer levels to it and stamping `adm_lvl` `0` for a
  country-only row; a table with no level-0 code column is unaffected
  (see `docs/adr/0091`).

### Fixed

- `coverage_clean()` now maps `ST_CoverageClean`'s output back onto rows
  via a synthetic per-row id instead of `fid`. Real multi-source portolan
  tables have colliding `fid` values across files, so the previous
  `fid`-keyed join fanned out on collisions and paired rows with the
  wrong cleaned geometry, causing `edge-match`'s `--merge` to raise
  `INVALID_EDGES` on real multi-file data even after the escalating
  `snapping_distance` retry exhausted.
- `edge-match`'s per-group `edge-extend` subprocess now runs the same
  no-erosion self-check standalone `edge-extend` already runs on its own
  output.
- `edge-extend`'s shared-boundary-zone difference (`_03_points.py`) is now
  bbox-prefiltered against nearby originals instead of a whole-file global
  union, fixing an OOM/stall on `idn_admin4`/`phl_admin4`-scale inputs
  (see `docs/adr/0090`).
- Issue rows in `edge-match`/`edge-mosaic` now shorten a Windows-written
  `source_file` path (backslash separators) the same way a POSIX path is
  shortened, instead of leaking the full absolute path.

## [0.5.0] - 2026-08-26

### Fixed

- `edge-stitch` now escalates `snapping_distance` past `SNAP_TOLERANCE`
  when invalid edges remain after the first coverage-clean pass, fixing
  a spurious `INVALID_EDGES` failure where two adjacent tiles sharing a
  border coincident with the clip boundary drifted apart under
  independent clipping (see `docs/adr/0089`).
- `edge-mosaic`'s `--merge` gap-fill now keeps an unmatched **parent**
  (zero matched children) in the output using the parent's own geometry
  and attributes, instead of keeping an unmatched whole **children file**
  unclipped, which was the wrong entity (see `docs/adr/0083`, superseding
  `docs/adr/0078`).
- `assign_one` no longer drops an individual child with zero overlap with
  its file's majority-vote winner; every child in a file with a
  determined winner is now forced onto it unconditionally. This is now
  the default assignment strategy for both `edge-mosaic` and `edge-match`
  (see `docs/adr/0082`).
- `edge-match` no longer coverage-cleans the parent/clip layer before
  assignment; it loads it raw, the same way `edge-mosaic` already does,
  fixing a pathological stall on large real-world parent files (see
  `docs/adr/0086`).
- `edge-mosaic`, `edge-match`, and standalone `edge-stitch` no longer
  export a `source_file` column on their main output; it was an internal
  `assign-one` working column that leaked into exported files by
  accident. `edge-mosaic`/`edge-match`'s issues report still carries
  `source_file`, but shortened to its parent directory plus filename
  (e.g. `sen/adm2.parquet`), never the full input path (see
  `docs/adr/0087`).

### Added

- `edge-match` now accepts multiple children files per call, the same
  glob/`--input` combining `edge-mosaic` already supports. Children are
  loaded and assigned one file at a time (a memory-bounded per-file loop
  mirroring `edge-mosaic`'s own, `docs/adr/0079`), so peak memory stays
  bounded regardless of how many files are combined; the shared per-group
  Voronoi extension, clip, stitch, and output steps then run once over the
  full combined result, so children from different files landing on the
  same parent extend together instead of independently (see
  `docs/adr/0084`).
- `edge-match`: new opt-in `--multi-parent`/`multi_parent` flag, reverting
  to the old per-child `assign-many` behavior for files whose children
  genuinely scatter across multiple parents (e.g. a poorly-digitized
  admin4 layer fitting into many admin3 units) (see `docs/adr/0082`);
  rejected outright when more than one children file is given, since its
  per-child plurality logic is not designed for cross-file semantics.
- A child whose clip intersection with its assigned parent comes back
  empty is now tracked and reported as a `kind='clip-empty'` issue row
  instead of silently dropped, across `edge-mosaic`, `edge-match`, and
  standalone `edge-clip`; never raises (see `docs/adr/0082`).
- Standalone `edge-clip` gains a general issues file for the first time:
  previously it only existed when a code-join was configured; it now
  always resolves an issues path and always writes it whenever there's
  at least one `clip-empty` or `code-mismatch` row.
- Issue rows now populate a real `source_file` value for `edge-match`
  (previously always NULL), including for `assign-many`'s unassigned rows.
- `prepare_parent_tiles()` now skips tiling any parent part outside the
  children's combined bbox, so matching a handful of children against one
  much larger shared parent (e.g. a global admin0 file) no longer tiles
  parts nothing will ever be assigned to (see `docs/adr/0085`).

### Changed

- **Breaking:** `edge-mosaic`/`edge-match`'s `--merge` is now a plain
  boolean flag; the old `--merge iso_3,adm0_name` value form becomes
  `--merge --parent-include iso_3,adm0_name`. New `--parent-include`/
  `--parent-exclude`/`--child-include`/`--child-exclude` narrow which
  parent/child columns survive in the merged output, and new `--prefer
  [parent|child]` auto-resolves a real parent/child column-name collision
  instead of raising. Both tools now perform identical parent gap-fill
  (`kind='gap-fill'`) and child-file passthrough (`kind='passthrough'`)
  under `--merge`, where previously each tool only had one of the two
  mechanisms (see `docs/adr/0088`).

## [0.4.0] - 2026-08-25

### Added

- `schema-fill`: new tool that cascades each admin-hierarchy column down
  from the nearest non-NULL shallower level and stamps a new `adm_lvl`
  column (overridable via `--depth-column`) with each row's real depth;
  levels are derived from a `schema-map` target-schema YAML, not a
  hardcoded naming convention. Run against an already-clipped/stitched
  layer, then `dissolve` each level normally, which carries the depth
  column through automatically.
- Standalone `edge-clip`: opt-in `carry_columns` (CLI: repeatable/
  comma-splittable `--carry-column`) copies named parent-layer columns
  onto every matched child.
- `edge-mosaic`/`edge-match`: opt-in `merge_columns: list[str] | bool =
  False` (CLI: `--merge`, a boolean-or-value flag) copies parent-layer
  columns onto matched children/files, every column when bare, a named
  subset when given a list, and keeps an otherwise-dropped unmatched
  child/file's own extended geometry in the output instead, unclipped.
  For `edge-mosaic` this passthrough is per whole children file; for
  `edge-match` it's per child, grouped into an orphan group of its own
  and extended alone, a materially weaker safety profile than
  `edge-mosaic`'s own passthrough (see `docs/adr/0077`, `docs/adr/0078`,
  `docs/adr/0079`, `docs/adr/0081`).

### Changed

- **Breaking:** `schema-map`/`schema-fill`'s bundled default target schema
  is now generic (`data/default.yaml`, `code_field: "adm{n}_code"`), not
  COD-AB-specific (`data/cod-ab.yaml`, `adm{n}_pcode`); a fuller
  COD-AB-specific schema is planned separately.
- `schema-map`: a function-passing, non-bijective same-level bracket
  candidate now numbers as a sibling (`name1`, `name2`, ...) instead of
  `supplemental` when its own collapse ratio against the level's unit
  count is `<= 0.30` (see `docs/adr/0076`).
- `edge-mosaic`: its multi-file children path now clips one file at a
  time against a cached parent-tile decomposition instead of loading
  every file into one combined table, bounding peak memory to roughly
  one file's own size regardless of batch count (see `docs/adr/0079`).
  `step` MUST now be omitted when more than one children file is given;
  `--debug` exports only the final accumulated tables, not one export
  per input file.

### Removed

- **Breaking:** standalone `edge-clip`'s multi-file batching
  (repeatable/comma-splittable `--input`/`--output`/`--issues`,
  `--name`-required mode): reverted to a strict
  one-children-file/one-parent-file/one-output primitive now that
  `edge-mosaic` owns batching many children files against one shared
  parent load (see `docs/adr/0080`, superseding `docs/adr/0022`/`0023`/
  `0024`).

## [0.3.0] - 2026-08-24

### Added

- `dissolve`: new tool that aggregates a polygon layer into a coarser one
  by grouping on attribute columns, auto-keeping every other column that's
  constant per group and dropping the rest.
- `schema-map`/`schema-refactor`/`schema-crosswalk`: new tools that map a
  source-column crosswalk to a target schema by inferring the admin
  hierarchy structurally (never renaming anything itself), apply that
  crosswalk to rename/drop columns, and chain the two in one call.
- `edge-match`/`edge-mosaic`/`edge-clip`: assignment now prefers an exact
  code match (e.g. a shared pcode) over the spatial plurality/majority
  pick when they disagree, falling back to the spatial pick when no code
  match exists; both outcomes are reported as issue rows for review.
- `schema-map`: chain-building now falls back to containment alone when
  no column pair in the file has embedding evidence, and tolerates a
  single missing-value sentinel (e.g. a literal "No_Pcode" string) the
  same way NULL already carries no evidence.
- `schema-map`: the crosswalk CSV gains a `unique_count` column
  (parent-combined distinct count) so a reviewer can spot a value reused
  across parents.

### Changed

- **Breaking:** every tool is renamed to a `{group}-{verb}` CLI
  convention: `extend`→`edge-extend`, `clip`→`edge-clip`,
  `stitch`→`edge-stitch`, `match`→`edge-match`, `mosaic`→`edge-mosaic`,
  `detect`→`topo-detect`, `clean`→`topo-clean`, `map`→`schema-map`,
  `refactor`→`schema-refactor`. `dissolve` and `change` are unchanged.
  Applies to CLI command names and Python API module names alike.
- Every tool's `overwrite` kwarg now defaults to `true`, logging the
  overwrite instead of requiring the flag on every rerun.

### Fixed

- `schema-map`: role assignment no longer lets one column's embedding
  evidence override a sibling's own shape evidence, and a coincidentally
  bijective non-name column no longer displaces the real name column.
- `schema-map`: GDAL collision-suffixed and DBF-truncated duplicate
  columns (e.g. `fid_1`, `Shape_Le_1`) are now excluded from the
  crosswalk the same as their originals; all-null columns are excluded
  from chain candidacy the same way they're already treated as no
  evidence for embedding.
- `schema-map`: dropped a country/admin0 embedding assumption that broke
  on files with an independently-numbered admin1 or no country column at
  all; group formation now considers every column regardless of
  embedding, and level exclusion is based on the level's own cardinality
  rather than its position in the chain.

## [0.2.0] - 2026-08-11

### Added

- `clean`: the fix-stage escalation loop now also rejects a rung if any fid
  with no detected defect of its own (not touching a filled gap, not party
  to an overlap) came out with a materially different area, or if any
  fid's fixed geometry isn't a Polygon/MultiPolygon. The existing total-area
  sanity floor only checks the summed total, so a single small feature
  collapsing (or degenerating into a stray line via a mixed
  `GEOMETRYCOLLECTION`) could pass it if the rest of the dataset is much
  larger. Defect-involved fids are exempt rather than held to a percentage
  floor, since `ST_CoverageClean` can legitimately redraw a whole neighborhood
  around a fix (confirmed: filling a gap fully reassigned two small,
  overlap-uninvolved connector strips into a third fid), and full
  containment is a legitimate 100%-loss outcome for the absorbed fid.
- `clean`: the fix stage now logs the accepted result's total area change
  (gained/lost, as a percentage) on every successful run, not just on
  escalation or failure.
- `clean`: the issues report now includes the actual measured outcome of
  the fix for every row, not just the defect as detected:
  `unit_a_area_change_m2`/`unit_b_area_change_m2` (each named unit's own
  real area change) for overlap rows, `filled_area_m2` (how much of the
  gap's own area ended up covered) for gap rows.

### Fixed

- `clean`: the fix stage no longer skips `ST_CoverageClean` entirely on a
  gap-only input (no overlaps at all). The gate previously relied on
  `has_coverage_violations()` alone, which never detects gaps, so a
  correctly-detected, fillable gap could sit in the issues file forever
  without ever actually being fixed. The gate now also checks whether any
  detected gap qualifies to fill under the resolved `gap_maximum_width`.

### Changed

- `clean`: `--maximum-gap-width` now defaults to `auto` (fill only
  thin/sliver-shaped gaps) instead of `all` (fill every detected gap).
  `all` remains available as an explicit opt-in.
- **Breaking:** `clean`: `--maximum-gap-width`/`--snapping-distance` (and
  the matching `api.clean.clean()` kwargs) now take decimal degrees instead
  of meters, the units `ST_CoverageClean` itself takes on our
  always-EPSG:4326 data, and the same convention GDAL/OGR uses for distance
  parameters on an unprojected layer. Removes a dataset-wide
  `cos(centroid latitude)` conversion that was a real approximation over
  large north-south extents. See `docs/clean.md`.
- `clean`: `_03_clean.py` now retries the resolved `gap_maximum_width`
  (from `auto`/`all`/an explicit value) through a validated escalation
  ladder (widening only, never below the original target) if
  `ST_CoverageClean` leaves residual invalid edges, raises, or silently
  erodes real polygon area (two confirmed real `ST_CoverageClean` failure
  modes), unrelated to `snapping_distance`. Validation now checks both
  `has_coverage_violations()` and a total-area sanity floor, since the
  former alone passes a totally empty result as "no violations." If every
  rung fails, `clean` now raises a clear, actionable error instead of the
  previous bare `OVERLAPS: {table}`. `--maximum-gap-width all`'s width is
  still computed from the widest actually-detected gap (unchanged
  behavior). A fixed large constant was tried during development and
  rejected after it was shown to make `ST_CoverageClean` erase real
  polygon area on real data. See `docs/clean.md`.

### Removed

- `clean`: the `MIN_ISSUE_AREA_M2` (~1cm²) floating-point noise floor on
  detected gaps/overlaps. Ported from topo-tools-js's own WASM-tuned
  constant; empirical testing against this native pipeline (a real
  9,658-fid COD admin4 layer, plus Chile/Philippines/Indonesia admin3's
  full-pipeline `extended.parquet` output) found zero floating-point
  artifacts on either detection path with the floor removed. `clean` now
  reports every detected gap/overlap regardless of size. See
  `docs/clean.md`.
- `clean`: sliver detection/reporting (`--sliver-tolerance`, issues-file
  `kind='sliver'` rows). Detection was unreliable even at tiny real-data
  scale (OOM confirmed on a 21-fid Angola admin1 file) and slivers were
  never auto-fixable in the first place (see `docs/clean.md`). `clean` now
  only detects/fixes gaps and overlaps.
- `extend`/`match`: `--memory-gb`. It only ever sized `attempt.py`'s
  Voronoi resampling distance. Other stages (`_01_inputs`, `_02_lines`,
  the whole-table `_05_merge`/`_04_merge` coverage-clean pass) have no
  resampling lever and routinely exceed whatever ceiling was declared
  anyway (documented cases up to ~5.9GB against a 4GB target), so the flag
  gave a false sense of a memory guarantee the pipeline never actually
  provided. The starting resampling distance is now always
  `min(DEFAULT_DISTANCE, natural_res)`; the existing doubling-retry loop
  still handles any resulting failure the same way it always did, see
  `docs/voronoi-memory.md`.

## [0.1.0] - 2026-07-10

Initial release: four tools, CLI + Python API for each.

- `extend`: Voronoi-based polygon boundary extension, producing a complete
  coverage layer that fills gaps.
- `match`: fits a child polygon layer into a coarser parent/clip layer by
  largest-overlap assignment, then runs `extend`'s pipeline per group.
- `clean`: detects and fixes coverage gaps/overlaps via `ST_CoverageClean`;
  detects (but never auto-fixes) slivers, reported separately for review.
- `change`: compares two versions of a polygon layer and classifies every
  unit as unchanged/renamed/modified/relocated/split/merge/complex/created/
  removed, via spatial overlap and optional code/name identity linking.

[Unreleased]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.17.0...HEAD
[0.17.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.15.1...v0.16.0
[0.15.1]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.6...v0.6.0
[0.5.6]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/OCHA-DAP/topo-tools-py/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/OCHA-DAP/topo-tools-py/releases/tag/v0.1.0
