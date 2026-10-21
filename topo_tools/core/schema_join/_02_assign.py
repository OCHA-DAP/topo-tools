"""Pairs each input polygon with its plurality-overlap join polygon and area share."""

from duckdb import DuckDBPyConnection

from topo_tools.core.assign import assign_many
from topo_tools.core.constants import EQUAL_AREA_CRS


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Build `{name}_02_assign` plus `{name}_02_share` (best join area share)."""
    assign_many(conn, name)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_share" AS
        WITH areas AS (
            SELECT fid AS input_fid,
                   ST_Area(ST_Transform(geom, 'EPSG:4326', '{EQUAL_AREA_CRS}', true))
                       AS input_area
            FROM "{name}_input_01"
        )
        SELECT a.input_fid, a.overlay_fid, areas.input_area, p.shared_area,
               p.shared_area / NULLIF(areas.input_area, 0) AS overlap_share
        FROM "{name}_02_assign" a
        JOIN areas USING (input_fid)
        JOIN "{name}_02_pairs" p
          ON p.input_fid = a.input_fid AND p.overlay_fid = a.overlay_fid
    """)
