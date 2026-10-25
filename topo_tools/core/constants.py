"""Shared, tool-neutral constants used by more than one pipeline."""

import re

SNAP_TOLERANCE = 0.00000001
# core.coverage.coverage_clean_escalating adds this to snapping_distance
# per retry when ST_CoverageInvalidEdges_Agg still flags the output.
SNAP_ESCALATION_STEP = SNAP_TOLERANCE
# Caps the retry loop at SNAP_TOLERANCE + this many extra steps (9 ->
# 1e-7 deg, ~1.1cm).
SNAP_ESCALATION_MAX_STEPS = 9
# A clip-detached piece merges into a neighbour only below this fraction of its
# own source part's kept piece; else it is reported.
DETACHED_MERGE_MAX_RATIO = 0.01
# A detached piece mostly outside its unit's original footprint is extension fill.
DETACHED_MAX_ORIGINAL_SHARE = 0.5
# Original land clipped away next to a piece, over its area; below this the
# original drew the piece as a lobe joined only at a pinch point.
DETACHED_MIN_NECK_RATIO = 0.1
# Notches: unshared edges of two units within NOTCH_SPACING / 8 of each other
# for at least NOTCH_MIN_SCORE spacings, edge-extend's default point spacing.
NOTCH_SPACING = 0.0002
NOTCH_MIN_SCORE = 2
# A flagged segment's endpoint moves onto the other unit only within this
# share of the segment's own length.
NOTCH_MAX_GAP_RATIO = 1 / 10
# Degrees around a notch inside which its two units are cut and rebuilt.
NOTCH_WINDOW_MARGIN = 0.002
# Equal Earth, used by match/change to rank/compute areas for cross-polygon
# area comparison (never stored).
EQUAL_AREA_CRS = "EPSG:8857"
# Every geometry is read, stored, and written tagged with this CRS (WGS84
# lon/lat, the GeoParquet default, so its `geo` metadata omits the crs key).
CRS = "OGC:CRS84"
# Shapefile/GPKG/GeoJSON exports; GDAL writes the standard WGS84 definition for it.
GDAL_CRS = "EPSG:4326"
GEOMETRY_TYPE = f"GEOMETRY('{CRS}')"
# Intermediate (subprocess hand-off) Parquet; V1 GeoParquet tags CRS-less as CRS84.
PARQUET_OPTS = "(FORMAT PARQUET, GEOPARQUET_VERSION 'V2')"
# Column names DuckDB's GDAL COPY writer treats specially as the feature's
# implicit FID; COPY (FORMAT GDAL) fails outright if either is present.
RESERVED_COLUMN_NAMES = ("fid", "OGC_FID")

# GIS bookkeeping columns map/refactor exclude as noise, not "real" source
# data: case-insensitive exact match, not a heuristic.
NOISE_COLUMNS = frozenset(
    {
        "objectid",
        "globalid",
        "fid",
        "shape_leng",
        "shape_length",
        "shape__length",
        "shape_area",
        "shape__area",
        "bbox",
        # GRASS GIS's per-feature category ID.
        "cat",
        # GDAL's own synthesized feature index, see RESERVED_COLUMN_NAMES.
        "ogc_fid",
        "ogc_fid_orig",
        "fid_orig",
    }
)

NUMERIC_DUCKDB_TYPE_PREFIXES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
    "REAL",
)


def is_numeric_duckdb_type(duckdb_type: str) -> bool:
    """Check a `DESCRIBE`-reported DuckDB column type is a numeric one."""
    return duckdb_type.upper().startswith(NUMERIC_DUCKDB_TYPE_PREFIXES)


FLOATING_DUCKDB_TYPE_PREFIXES = ("FLOAT", "DOUBLE", "DECIMAL", "REAL")


def is_floating_duckdb_type(duckdb_type: str) -> bool:
    """Check a `DESCRIBE`-reported DuckDB column type is a continuous/fractional one."""
    return duckdb_type.upper().startswith(FLOATING_DUCKDB_TYPE_PREFIXES)


TEMPORAL_DUCKDB_TYPE_PREFIXES = ("DATE", "TIME", "TIMESTAMP", "INTERVAL")


def is_temporal_duckdb_type(duckdb_type: str) -> bool:
    """Check a `DESCRIBE`-reported DuckDB column type is a date/time one."""
    return duckdb_type.upper().startswith(TEMPORAL_DUCKDB_TYPE_PREFIXES)


_NOISE_SUFFIX_RE = re.compile(r"_(\d+)$")
# ESRI Shapefile's DBF driver caps field names at this many characters total,
# truncating the base name to make room for a disambiguating "_N" suffix.
_DBF_FIELD_NAME_LIMIT = 10


def is_noise_column(name: str) -> bool:
    """Check name, or its GDAL collision-suffixed/DBF-truncated form, is noise."""
    lowered = name.lower()
    if lowered in NOISE_COLUMNS:
        return True
    match = _NOISE_SUFFIX_RE.search(lowered)
    if not match:
        return False
    base = lowered[: match.start()]
    if base in NOISE_COLUMNS:
        return True
    return len(lowered) == _DBF_FIELD_NAME_LIMIT and any(
        noise.startswith(base) for noise in NOISE_COLUMNS
    )


# core.clip skips grid-tiling below this vertex count and clips directly.
CLIP_TILE_MIN_VERTICES = 5000
# Target vertices per tile once tiling triggers; cell size is solved from
# this against each overlay polygon's vertex density (see _adaptive_cell_size).
CLIP_TILE_TARGET_VERTICES = 1350
CLIP_TILE_MIN_CELL = 0.05
CLIP_TILE_MAX_CELL = 5.0

_PARQUET_EXPORT = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
COPY_OPTS = {
    ".parquet": _PARQUET_EXPORT,
    ".gpkg": "WITH (FORMAT GDAL, DRIVER 'GPKG')",
    ".geojson": "WITH (FORMAT GDAL, DRIVER 'GeoJSON')",
    ".shp": "WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile')",
}
