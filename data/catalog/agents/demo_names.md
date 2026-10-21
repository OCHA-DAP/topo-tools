# AGENTS.md: {title}

Input for `topo-tools name-detect` and `topo-tools name-clean`: the {rows} gemeenten of the [package](../package/AGENTS.md) demo with six `adm2_name` values edited, one per kind of finding. The CRS is EPSG:28992, in metres.

| Code | Name | Stored as | Finding | Fixed by name-clean |
|---|---|---|---|---|
{edits}

## Run the demo

```bash
topo-tools name-detect {url} nld_admin2_name_issues.csv
topo-tools name-clean {url} nld_admin2_cleaned.parquet nld_admin2_name_issues.csv
```

`name-detect` reports the six findings and changes nothing. `name-clean` restores the four names whose fix can't change their meaning, and leaves the capitals and the duplicate for review. In the browser: [name-clean]({web}/name-clean?url={url}), [name-detect]({web}/name-detect?url={url}).
