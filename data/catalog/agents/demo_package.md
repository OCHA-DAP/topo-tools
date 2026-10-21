# AGENTS.md: {title}

Input for `topo-tools package`: the {rows} gemeenten from the [edge](../edge/AGENTS.md) demo after `edge-match --per-feature`, so together they fill the provincies, water included. Each has adm0 `NL`, its provincie as adm1 from Kadaster Bestuurlijke Gebieden, and its CBS code and name as adm2. The CRS is EPSG:28992, in metres.

## Run the demo

```bash
topo-tools package {url} --output 'nld_{{x}}.parquet' --name-field 'adm{{n}}_name' --code-field 'adm{{n}}_code'
```

`package` writes `nld_admin0`, `nld_admin1` and `nld_admin2` (1, {provincies} and {rows} polygons), `nld_points` ({points} points, one per unit) and `nld_lines` ({lines} boundary lines). Each level's issues file lists the provincies' own {gaps} holes, such as the Baarle-Nassau enclaves. In the browser: [package]({web}/package?url={url}&name=adm{{n}}_name&code=adm{{n}}_code).

## Quirks

- Without `--name-field` and `--code-field`, auto-detection finds only adm1 and adm2, so no country polygon is written.
- Boundaries are simplified to 100 m. Use the full-precision layer for areas.
