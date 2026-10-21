# AGENTS.md: {title}

Input for `topo-tools schema-join`: the {rows} land gemeenten from the CBS Wijk- en Buurtkaart 2025 ([full precision](../../2025/nld_admin2/AGENTS.md)) and the 12 provincies from Kadaster Bestuurlijke Gebieden. The CRS is EPSG:28992, in metres.

## Run the demo

```bash
topo-tools schema-join {input} {join} nld_admin2_join.parquet
```

`schema-join` assigns each gemeente to the provincie it overlaps most and copies that provincie's `adm1_code` and `adm1_name` onto it. `nld_admin2_join.parquet` has the columns `adm2_name, adm2_code, adm1_name, adm1_code` and is in EPSG:4326. Every gemeente lies inside one provincie, so no issues file is written.

## Quirks

- The provincies include water (IJsselmeer, Waddenzee, the Zeeland estuaries), while the gemeenten are land only.
- Boundaries are simplified to 100 m. Use the full-precision layer for areas.

## Queries

```sql
-- Provincie area in km², water included
SELECT adm1_code, adm1_name, round(ST_Area(geometry) / 1e6) AS km2
FROM read_parquet('{join}') ORDER BY adm1_code;
-- {areas}
```
