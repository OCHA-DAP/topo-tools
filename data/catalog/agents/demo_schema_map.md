# AGENTS.md: {title}

Input for `topo-tools schema-map`: the {rows} land gemeenten from the CBS Wijk- en Buurtkaart 2025 ([full precision](../../2025/nld_admin2/AGENTS.md)), with the provincie code and name from CBS StatLine table 86059NED. The CRS is EPSG:28992, in metres.

## Run the demo

```bash
topo-tools schema-map {url} nld_admin2_mapped.parquet --csv-output nld_admin2_crosswalk.csv
topo-tools schema-map {url} --map-only --csv-output nld_admin2_crosswalk.csv
```

`--map-only` writes only the crosswalk. Without it, `schema-map` writes the same crosswalk and applies it:

{crosswalk}

`nld_admin2_mapped.parquet` has the columns `adm2_name, adm2_code, adm1_name, adm1_code` and is in EPSG:4326.

## Quirks

- Boundaries are simplified to 100 m. Use the full-precision layer for areas.
- Water rows are left out, so `topo-detect` reports the water bodies, such as the IJsselmeer, as {gaps} gaps.
- `landcode` and `landnaam` each hold one value. They are there to show `schema-map` dropping single-value columns.

## Queries

```sql
-- Gemeenten per provincie
SELECT provinciecode, provincienaam, count(*) AS gemeenten
FROM read_parquet('{url}')
GROUP BY ALL ORDER BY provinciecode;
-- {counts}
```
