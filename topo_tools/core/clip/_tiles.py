"""Overlay tiles, and the row/tile pairs the clip batches over them."""

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import CLIP_PAIR_VERTEX_BUDGET, GEOMETRY_TYPE
from topo_tools.core.duckdb_utils import bbox_columns_sql, subdivide_sql


def build_tiles(
    conn: DuckDBPyConnection, overlay_source: str, fids_sql: str, table: str
) -> None:
    """Add `table` (tid, overlay_fid, geom, bbox) rows for fids not tiled yet."""
    conn.execute(f"""--sql
        CREATE TABLE IF NOT EXISTS "{table}" (
            tid BIGINT, overlay_fid BIGINT, geom {GEOMETRY_TYPE},
            xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE
        )
    """)
    fids = conn.execute(f"""--sql
        SELECT DISTINCT fid FROM ({fids_sql}) f(fid)
        WHERE fid NOT IN (SELECT DISTINCT overlay_fid FROM "{table}")
        ORDER BY 1
    """).fetchall()
    for (g,) in fids:
        conn.execute(f"""--sql
            INSERT INTO "{table}" BY NAME
            SELECT (SELECT coalesce(max(tid), 0) FROM "{table}")
                   + row_number() OVER () AS tid,
                   {g} AS overlay_fid, geom, {bbox_columns_sql("geom")}
            FROM (
                SELECT UNNEST({subdivide_sql("geom")}) AS geom
                FROM {overlay_source} WHERE fid = {g}
            )
        """)


def pair_batches(
    conn: DuckDBPyConnection,
    rows: str,
    tiles: str,
    out: str,
    overlay_fid: int | None = None,
) -> None:
    """Create `out` (fid, tid, b): row/tile bbox pairs, batched by row vertex count."""
    where = "TRUE" if overlay_fid is None else f"overlay_fid = {overlay_fid}"
    where_r = "TRUE" if overlay_fid is None else f"r.overlay_fid = {overlay_fid}"
    # Joining counts, never geometries, keeps one big row over many tiles in memory.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{out}" AS
        SELECT p.fid, p.tid,
               floor(sum(n.np) OVER (ORDER BY p.fid, p.tid)
                     / {CLIP_PAIR_VERTEX_BUDGET})::BIGINT AS b
        FROM (
            SELECT r.fid, t.tid FROM "{rows}" r JOIN "{tiles}" t
              ON t.overlay_fid = r.overlay_fid
             AND t.xmax >= r.xmin AND t.xmin <= r.xmax
             AND t.ymax >= r.ymin AND t.ymin <= r.ymax
            WHERE {where_r}
        ) p
        JOIN (SELECT fid, ST_NPoints(geom) AS np FROM "{rows}" WHERE {where}) n
          USING (fid)
    """)
