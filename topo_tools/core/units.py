"""Degrees -> meters conversion for issues-file reporting columns.

Ported from cod-site/src/lib/tools/topo-clean/pipeline/units.ts.
"""

from math import cos, radians

from duckdb import DuckDBPyConnection

METERS_PER_DEGREE = 111_320


def cos_lat_factor(centroid_lat: float) -> float:
    """Latitude-scale factor, guarded near the poles so it never collapses to ~0."""
    return max(cos(radians(centroid_lat)), 0.05)


def centroid_lat_of(conn: DuckDBPyConnection, table: str) -> float:
    """Latitude of the table's overall extent centroid, 0 if the table is empty."""
    lat = conn.execute(f"""--sql
        SELECT ST_Y(ST_Centroid(ST_Extent_Agg(geom))) FROM "{table}"
    """).fetchall()[0][0]
    return lat if lat is not None else 0.0


def m2_per_deg2_factor(conn: DuckDBPyConnection, table: str) -> float:
    """Multiplier from a raw degree^2 area to square meters, latitude-scaled."""
    return METERS_PER_DEGREE**2 * cos_lat_factor(centroid_lat_of(conn, table))
