# AGENTS.md: Netherlands

CBS Wijk- en Buurtkaart 2021 to 2025, downloaded from the PDOK GeoPackages. The layers are gemeenten (`nld_admin2`), wijken (`nld_admin3`) and buurten (`nld_admin4`). Inputs for demonstrating each topo-tools tool are in [demo/AGENTS.md](demo/AGENTS.md). Credit: © Kadaster / Centraal Bureau voor de Statistiek.

## Conventions shared by every year

- **CRS** is EPSG:28992 (RD New), in metres, so `ST_Area` returns m². Reproject with `ST_Transform(geometry, 'EPSG:28992', 'EPSG:4326', always_xy := true)`.
- **`water`** is `NEE` for land, `JA` for water and `B` for the single Belgian row, `GM0998` Buitenland. Filter on `water = 'NEE'` for land-only analysis.
- **Gemeente codes aren't unique.** 81 gemeenten in 2025 have both a land row and a water row under the same `gemeentecode`. Join on `(gemeentecode, water)`.
- **Water buurten** have codes ending in `9997` or `9998`.
- **Parent codes** are in each child row: buurten carry `wijkcode` and `gemeentecode`, and wijken carry `gemeentecode`.

## Comparing years

Codes are not stable identities. From 2024 to 2025, 235 buurt codes appeared and 80 disappeared. Also, 14,476 of the 14,494 land buurten that kept their code have geometry that isn't byte-identical. Compare years by overlap, not by code or `ST_Equals`:

```sql
-- Which 2023 gemeente took over Weesp's 2022 territory
WITH w AS (
  SELECT ST_Union_Agg(geometry) AS g
  FROM read_parquet('{data}/nld/2022/nld_admin2/nld_admin2.parquet')
  WHERE gemeentenaam = 'Weesp'
)
SELECT n.gemeentecode, n.gemeentenaam,
       round(ST_Area(ST_Intersection(n.geometry, w.g)) / 1e6, 2) AS km2
FROM read_parquet('{data}/nld/2023/nld_admin2/nld_admin2.parquet') n, w
WHERE ST_Intersects(n.geometry, w.g)
  AND ST_Area(ST_Intersection(n.geometry, w.g)) > 1e4
ORDER BY km2 DESC;
-- GM0363 Amsterdam, 24.16
```

`topo-tools change` classifies a year pair. Its `--link-by-code` only links codes that are unique, so first filter both inputs to `water = 'NEE'`.

## Revisions

These are the PDOK revisions served on 2026-09-27: versie 3 for 2021 to 2023, versie 2 for 2024 and versie 1 for 2025. PDOK marks only 2022 as final.
