---
title: "topo-clean"
sidebar:
  order: 4
  badge:
    text: Draft
    variant: caution
---

## Inputs

- `topo-clean` MUST read the input and reproject it to EPSG:4326 without
  correcting any topology defects first, so the issues stage sees the
  original, unmodified geometry.

## Detecting gaps and overlaps

`topo-clean` MUST detect gaps and overlaps exactly as `topo-detect` does (see
`docs/dev/reference/2-topology/topo_detect.md`, "Detecting gaps and overlaps"): the same
defect-reporting rules apply unchanged, `topo-clean` calls `topo-detect`'s own
detection stage directly rather than owning separate logic (see
`docs/adr/0028`).

## Fixing gaps and overlaps

- `topo-clean` MUST attempt to fix the input whenever it contains any overlap,
  or any detected gap that qualifies to be filled under the requested
  mode.
- `topo-clean` MUST close every detected notch before the fix, moving a
  flagged segment's endpoint onto the other unit only when the gap is at
  most `NOTCH_MAX_GAP_RATIO` of the segment's length and the endpoint
  touches no third unit, and merging any gap those moves enclose (bounded
  by the two units and their neighbours, with no other unit inside it)
  into whichever of the two shares more of its border (see
  `docs/adr/0139`). A notch left open MUST be reported with `fixed` FALSE.
- The default gap-fill behavior (reached by omitting `--maximum-gap-width`,
  not a named mode) MUST fill a gap only if its width is at or below
  `SNAP_TOLERANCE`, regardless of shape.
- Whatever the gap-fill mode, whenever it runs the coverage clean (a
  reported gap, invalid edges, or an explicit width), `topo-clean` MUST
  merge every enclosed gap
  whose mean width is at most `SLIVER_GAP_MAX_RATIO` of its edges' mean
  segment length into the neighbour sharing its longest border, and report
  it as a `sliver-gap` row with `fixed` TRUE.
- The `all` mode MUST fill every detected gap, regardless of shape.
- A user-supplied numeric width MUST be honored directly, in decimal
  degrees, with no unit conversion. Requesting any gap-fill mode other
  than `all` or a number MUST raise `ValueError`; requesting the
  literal string `auto` MUST also raise `ValueError`, the default is only
  reachable by omitting the flag.
- The default snapping behavior (reached by omitting `--snapping-distance`,
  not a named mode) MUST use `SNAP_TOLERANCE`, not `ST_CoverageClean`'s own
  extent-relative computed default; a user-supplied numeric distance, in
  decimal degrees, MUST be honored directly. Requesting any snapping mode
  other than a number MUST raise `ValueError`; requesting the literal
  string `auto` MUST also raise `ValueError`, the default is only
  reachable by omitting the flag.
- `topo-clean` MUST attempt the fix exactly once, at the width resolved from
  the requested mode. There is no retry and no escalation to a wider
  value.
- `topo-clean` MUST reject the fix, raising `RuntimeError` immediately, if any
  of the following hold: the output still contains an overlap; any
  polygon's fixed shape is not a valid polygon; the output's total area
  falls below a floor set by a small baseline tolerance plus headroom
  sized to the total area of the overlaps actually detected; or a polygon
  with no connection to any detected gap, overlap or notch collapses to nothing.
- A polygon that was itself party to a gap or overlap being resolved MAY
  change area substantially, including losing all of it, without
  triggering rejection. A polygon untouched by any detected defect MAY
  still drift in area (logged as a warning) without triggering rejection,
  but MUST NOT collapse to nothing.

## Outputs

- `topo-clean` MUST raise `RuntimeError` if the fixed output still contains
  any overlap.
- `topo-clean` MUST merge or drop every micro-polygon left in the fixed
  output, including when no gap or
  overlap needed fixing. Each part merged or dropped, before the fix or
  after it, MUST appear in the issues report as a `micro-polygon` row
  with its merge outcome, in place of its detection row.
- `topo-clean` MUST NOT raise an error over a gap left unfilled by design. It
  MUST only log a warning naming how many gaps are still unfilled.
- `topo-clean` MUST always produce the cleaned dataset. It MUST produce the
  issues report, using the same columns as every other tool's issues report,
  only when the input had at least one detected defect; when it would be
  empty, no file MUST be written (and a stale file from a previous run at
  that path MUST be removed).
- The issues report MUST also state each issue's actual measured outcome,
  not just the defect as originally detected: whether it was fixed (for a
  gap, no gap still open in the output overlaps it); for an overlap, how
  much each of its two named units' own area actually changed, with a unit
  that lost all its area removed from the output and its row marked
  unfixed with a `reason` naming the removed unit, plus the covering unit
  only when it covers the removed one in the input; for a gap,
  how much of the gap's own area ended up covered (zero if left unfilled).
  A gap open in the output that overlaps no detected gap MUST get its own
  unfixed `gap` row, so the unfixed gap rows match `topo-detect` on the
  output.
- `topo-clean` MUST report the fixed output's total area change (gained or
  lost) relative to the input.

## Configuration (`api.topo_clean.clean()` / CLI)

- `topo-clean` MUST process exactly one input file per call.
- The cleaned-dataset path MUST default to the input path with a
  `_cleaned` suffix. The issues-report path MUST default to the
  cleaned-dataset path with an `_issues` suffix.
- `topo-clean` MUST raise `FileExistsError` if either output path already
  exists and overwriting wasn't requested.
- If a single pipeline step is requested, it MUST be one of `inputs`,
  `issues`, `topo-clean`, `outputs`. Any other value MUST raise `ValueError`.

## Examples

### Example 1: explicit output (default is INPUT_FILE with a "_cleaned" suffix)

    topo-tools topo-clean example.geojson example_cleaned.geojson

### Example 2: fill every detected gap, not just slivers

    topo-tools topo-clean example.gpkg --maximum-gap-width all

### Example 3: custom issues report path and snapping distance

    topo-tools topo-clean example.parquet --issues-file example_report.parquet \
      --snapping-distance 0.0001
