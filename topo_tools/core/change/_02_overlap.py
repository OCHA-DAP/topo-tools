"""Computes pairwise overlap ratios between the old and new layers."""

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import EQUAL_AREA_CRS
from topo_tools.core.duckdb_utils import bbox_columns_sql

from ._constants import INTERSECTION_SLIVER_DEG2


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Compute shared_area/coverage_a/coverage_b/iou for every touching (a, b) pair."""
    # Bbox columns precomputed here, not called inline in the join below:
    # DuckDB re-evaluates an inline envelope call per comparison, not once per row.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_a_01"
        )
        SELECT fid, part_geom, {bbox_columns_sql("part_geom")}
        FROM parts
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_b_01"
        )
        SELECT fid, part_geom, {bbox_columns_sql("part_geom")}
        FROM parts
    """)

    # Sliver crumbs dropped by raw degree^2 area, a cheap pre-filter before
    # the equal-area transform, applied only to surviving intersection geometry.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
        WITH crumbs AS (
            SELECT a.fid AS a_fid, b.fid AS b_fid,
                   ST_CollectionExtract(
                       ST_Intersection(a.part_geom, b.part_geom), 3
                   ) AS geom
            FROM "{name}_02_tmp1" a
            JOIN "{name}_02_tmp2" b
              ON b.xmax >= a.xmin
             AND b.xmin <= a.xmax
             AND b.ymax >= a.ymin
             AND b.ymin <= a.ymax
             AND ST_Intersects(a.part_geom, b.part_geom)
        ),
        filtered AS (
            SELECT a_fid, b_fid, geom FROM crumbs
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
              AND ST_Area(geom) >= {INTERSECTION_SLIVER_DEG2}
        )
        SELECT a_fid, b_fid,
               SUM(
                   ST_Area(ST_Transform(geom, 'EPSG:4326', '{EQUAL_AREA_CRS}', true))
               ) AS shared_area
        FROM filtered
        GROUP BY a_fid, b_fid
    """)

    # Whole-geometry area per fid, computed once (not once per pair).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
        SELECT fid, ST_Area(ST_Transform(
                   geom, 'EPSG:4326', '{EQUAL_AREA_CRS}', true
               )) AS area
        FROM "{name}_a_01"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp5" AS
        SELECT fid, ST_Area(ST_Transform(
                   geom, 'EPSG:4326', '{EQUAL_AREA_CRS}', true
               )) AS area
        FROM "{name}_b_01"
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT p.a_fid, p.b_fid, p.shared_area,
               p.shared_area / NULLIF(aa.area, 0) AS coverage_a,
               p.shared_area / NULLIF(ba.area, 0) AS coverage_b,
               p.shared_area / NULLIF(aa.area + ba.area - p.shared_area, 0) AS iou
        FROM "{name}_02_tmp3" p
        JOIN "{name}_02_tmp4" aa ON aa.fid = p.a_fid
        JOIN "{name}_02_tmp5" ba ON ba.fid = p.b_fid
    """)

    for n in range(1, 6):
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp{n}"')
