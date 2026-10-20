---
title: "COD-AB metadata"
sidebar:
  order: 3
  label: "Metadata"
---

One row per country release, written as a `metadata` table inside each
candidate GDB. Columns MUST appear in this order. An empty value
means unknown. All COD-AB data is published under CC BY-IGO.

| Column | Type | Required | Description |
| --- | --- | --- | --- |
| `country_name` | text | yes | Country name in English |
| `country_iso2` | text | yes | ISO 3166-1 alpha-2, uppercase |
| `country_iso3` | text | yes | ISO 3166-1 alpha-3, uppercase |
| `version` | text | yes | Release version, `v01`, `v02`, ... |
| `admin_level_full` | integer | yes | Deepest level covering the whole country |
| `admin_level_max` | integer | yes | Deepest level in the data |
| `admin_1_name` … `admin_5_name` | text | no | Concept name of each level (Province, District), not a unit name |
| `admin_1_count` … `admin_5_count` | integer | up to `admin_level_max` | Number of units per level, empty beyond `admin_level_max` |
| `admin_notes` | text | no | Notes on levels, codes and names |
| `date_source` | date | no | Latest edit date published by any source layer |
| `date_updated` | date | no | Date the contributor last updated the dataset |
| `date_reviewed` | date | no | Date OCHA last reviewed the dataset |
| `date_metadata` | date | yes | Date this record was written |
| `date_valid_on` | date | no | Date from which this version is valid |
| `date_valid_to` | date | no | Date this version was superseded, empty while current |
| `update_frequency` | integer | yes | Years between source updates, 1 or 2 |
| `update_type` | text | yes | `major`, or `minor` when no boundary changed |
| `source` | text | yes | Organisation(s) that produced the data |
| `source_url` | text | no | Available source-layer URLs, `; `-separated, coarsest level first; empty when the data has no download URL |
| `contributor` | text | yes | OCHA office or team that processed the data |
| `methodology_dataset` | text | yes | How the data was obtained and processed, including licence attribution conditions |
| `methodology_pcodes` | text | no | How codes were assigned |
| `caveats` | text | no | Limits a data user should know |

Dates are `YYYY-MM-DD`. `admin_level_full` MUST NOT exceed
`admin_level_max`.

Use `null` for a source layer's `url` in `metadata.json` when the data
was handed over directly without a download endpoint. Those null URLs
are omitted from `source_url`. The source organisation and methodology
remain required so the record still explains where the data came from.
