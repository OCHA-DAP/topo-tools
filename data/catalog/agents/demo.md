# AGENTS.md: Netherlands demo inputs

Input layers for trying topo-tools on Dutch boundaries. Each folder is named after the tool or tool family it's for and holds the files that tool takes, simplified to 100 m. Tool outputs aren't stored here: run the tool to produce them.

| Input | Tool | Contents |
|---|---|---|
| [schema-map](schema-map/AGENTS.md) | `schema-map` | 342 gemeenten with CBS column names |
| [schema-join](schema-join/AGENTS.md) | `schema-join` | 342 gemeenten and 12 provincies, each with its own code and name |
| [edge](edge/AGENTS.md) | `edge-match` | 342 gemeenten, land only, and 12 provincies with water |
| [topo](topo/AGENTS.md) | `topo-detect`, `topo-clean` | 343 gemeenten with 2 overlaps and 3 gaps added |
| [code](code/AGENTS.md) | `code-update` | 345 gemeenten of 2022, coded, and 342 of 2023 |
| [names](names/AGENTS.md) | `name-detect`, `name-clean` | 342 gemeenten with 6 name errors added |
| [package](package/AGENTS.md) | `package` | 342 gemeenten under adm0 and adm1, filling their provincies |

The gemeenten come from [nld/2025/nld_admin2](../2025/nld_admin2/AGENTS.md) (2022 and 2023 for `code`) and the provincies from Kadaster Bestuurlijke Gebieden, built by `data/catalog/build_demo.py` in [topo-tools](https://github.com/OCHA-DAP/topo-tools). The conventions are in [../AGENTS.md](../AGENTS.md).
