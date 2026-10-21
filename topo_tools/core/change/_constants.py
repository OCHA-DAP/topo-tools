"""Non-user-configurable constants for the change pipeline."""

from topo_tools.core.constants import SNAP_TOLERANCE

# Minimum max(coverage_a, coverage_b) for two units to be spatially linked
# (union-find edge).
TAU_MATCH_DEFAULT = 0.8

# Minimum IoU for a 1:1 spatially-linked pair to be unchanged/renamed rather
# than modified. Ported from cod-site's change/App.svelte default.
TAU_SAME_DEFAULT = 0.98

# Intersection crumbs below this area are dropped as noise before shared-area
# aggregation: the area-equivalent of the shared SNAP_TOLERANCE noise floor.
INTERSECTION_SLIVER_DEG2 = SNAP_TOLERANCE**2

# Tabular changelog export has no geometry column, so extend's GDAL-vector
# COPY_OPTS doesn't apply: FORMAT CSV/PARQUET need no spatial extension.
TABLE_COPY_OPTS = {
    ".csv": "(FORMAT CSV, HEADER)",
    ".parquet": "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)",
}
