"""Extracts polygon boundary lines and retains per-polygon attributes."""

import logging

from duckdb import DuckDBPyConnection

from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.edge_extend._constants import LINES_VERTEX_BUDGET

logger = logging.getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str) -> None:
    """Create boundary lines from polygons."""
    # Per-polygon boundary lines, with bbox columns precomputed: DuckDB
    # re-evaluates an inline envelope call per comparison, not once per row.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        WITH boundary AS (
            SELECT fid, ST_Boundary(geom) AS geom FROM "{name}_01"
        )
        SELECT fid, geom, {bbox_columns_sql("geom")}
        FROM boundary
    """)

    # Per-polygon neighbor union: scalar bbox predicates plus an exact
    # ST_Intersects filter, no LATERAL, plans as PIECEWISE_MERGE_JOIN.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        SELECT a.fid AS afid, ST_Union_Agg(b.geom) AS neighbor_union
        FROM "{name}_02_tmp1" AS a
        JOIN "{name}_02_tmp1" AS b
          ON a.fid != b.fid
         AND b.xmax >= a.xmin
         AND b.xmin <= a.xmax
         AND b.ymax >= a.ymin
         AND b.ymin <= a.ymax
         AND ST_Intersects(a.geom, b.geom)
        GROUP BY a.fid
    """)

    # Row-order batches, each joining only its own neighbour unions: a 2048-row
    # vector holds every row's difference, and a hash join builds all of tmp2.
    sizes = conn.execute(f"""--sql
        SELECT a.rowid, a.n + coalesce(n.n, 0)
        FROM (SELECT rowid, fid, ST_NPoints(geom) AS n FROM "{name}_02_tmp1") AS a
        LEFT JOIN (
            SELECT afid, ST_NPoints(neighbor_union) AS n FROM "{name}_02_tmp2"
        ) AS n ON a.fid = n.afid
        ORDER BY a.rowid
    """).fetchall()
    batches: list[list[int]] = []
    total = LINES_VERTEX_BUDGET
    for rid, n in sizes:
        if total + n > LINES_VERTEX_BUDGET:
            batches.append([rid, rid])
            total = 0
        batches[-1][1] = rid
        total += n
    batches = batches or [[0, -1]]

    # Exterior edges = each polygon's boundary minus its neighbour union.
    for i, (lo, hi) in enumerate(batches, 1):
        if len(batches) > 1:
            logger.info("lines: batch %d of %d", i, len(batches))
        into = (
            f'CREATE OR REPLACE TABLE "{name}_02" AS'
            if i == 1
            else f'INSERT INTO "{name}_02"'
        )
        conn.execute(f"""--sql
            {into}
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_LineMerge(ST_CollectionExtract(
                    CASE WHEN n.neighbor_union IS NOT NULL
                        THEN ST_Difference(a.geom, n.neighbor_union)
                        ELSE a.geom
                    END, 2
                )))).geom AS geom
            FROM (
                SELECT * FROM "{name}_02_tmp1" WHERE rowid BETWEEN {lo} AND {hi}
            ) AS a
            LEFT JOIN (
                SELECT * FROM "{name}_02_tmp2" WHERE afid IN (
                    SELECT fid FROM "{name}_02_tmp1" WHERE rowid BETWEEN {lo} AND {hi}
                )
            ) AS n ON a.fid = n.afid
        """)

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp2"')
