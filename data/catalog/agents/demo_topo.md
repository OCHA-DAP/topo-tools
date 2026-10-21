# AGENTS.md: {title}

Input for `topo-tools topo-detect` and `topo-tools topo-clean`: the {rows} gemeenten from the CBS Wijk- en Buurtkaart 2025 ([full precision](../../2025/nld_admin2/AGENTS.md)), each with its water, so together they cover the country with no gaps or overlaps. Then one vertex on each of five shared borders is moved, the way a misplaced click would. The CRS is EPSG:28992, in metres.

| Error | Gemeente | Neighbour | Vertex moved |
|---|---|---|---|
{errors}

## Run the demo

```bash
topo-tools topo-detect {url} nld_admin2_issues.parquet
topo-tools topo-clean {url} nld_admin2_cleaned.parquet --maximum-gap-width thin
```

`topo-detect` reports {overlap_issues} overlaps and {gap_issues} gaps, {min_w} to {max_w} m wide. `topo-clean` always fixes the overlaps. `--maximum-gap-width thin` also fills the gaps, because each is a thin wedge along a border. In the browser: [topo-clean]({web}/topo-clean?url={url}&gap=thin), [topo-detect]({web}/topo-detect?url={url}).

## Quirks

- `GM0998` Buitenland is the Belgian enclaves in Baarle-Nassau. It stays in so the coverage has no holes.
- Boundaries are simplified to 100 m. Use the full-precision layer for areas.
