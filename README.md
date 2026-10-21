# topo-tools

[![CI](https://github.com/OCHA-DAP/topo-tools-py/actions/workflows/ci.yml/badge.svg)](https://github.com/OCHA-DAP/topo-tools-py/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/topo-tools)](https://pypi.org/project/topo-tools/)
[![Python versions](https://img.shields.io/pypi/pyversions/topo-tools)](https://pypi.org/project/topo-tools/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

![World ADM0 boundaries extended with Voronoi-filled coastline](https://raw.githubusercontent.com/OCHA-DAP/topo-tools-py/main/img/wld_01.png)

`topo-tools` is a collection of DuckDB-powered geospatial topology utilities
for cleaning and reconciling administrative boundary polygons, usable from
the CLI or as a Python package.

Full documentation: <https://ocha-dap.github.io/topo-tools-js/tools/docs/>

| Tool | Description |
| --- | --- |
| **schema-map** | map and apply a source to target schema crosswalk |
| **schema-fill** | fill down admin-hierarchy columns |
| **schema-join** | copy a join layer's admin-hierarchy columns onto each input feature |
| **topo-clean** | detect and fix gap/overlap defects |
| **topo-detect** | detect gap/overlap defects |
| **edge-match** | fit an input layer into an overlay layer |
| **edge-extend** | fill gaps with a Voronoi extension |
| **edge-mosaic** | re-clip an extended input layer into a new overlay layer |
| **edge-clip** | clip an input layer to an overlay layer |
| **edge-stitch** | close seams in a tiled layer |
| **change** | classify changes between two polygon layer versions |
| **code-create** | assign a fresh hierarchical code, ranked per parent |
| **code-update** | carry codes over from an old layer to a new one |
| **name-detect** | check unit names for blanks, duplicates and encoding errors |
| **name-clean** | fix spacing, invisible characters and encoding errors in unit names |
| **package** | dissolve, label, and line-ify a layer in one call |
| **package-polygons** | dissolve a layer into coarser admin levels |
| **package-points** | one label point per admin unit |
| **package-lines** | deduplicated admin boundary lines |

## Installation

Install with `uv`, `pip`/`pipx`, conda or Homebrew:

```sh
uv tool install topo-tools                   # CLI
uv add topo-tools                            # Python library
pip install topo-tools                       # CLI or library
pipx install topo-tools                      # CLI
conda install -c conda-forge topo-tools      # CLI or library, recommended on Windows
brew install OCHA-DAP/topo-tools/topo-tools  # macOS/Linux, no Python tooling
```

## Supported Formats

Polygon inputs/outputs: GeoParquet (`.parquet`), GeoPackage (`.gpkg`),
Shapefile (`.shp`), GeoJSON (`.geojson`). Output format matches input format.
`change`'s tabular changelog is CSV or GeoParquet only; its spatial overlay
layer supports the same four formats as the other tools.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup.
