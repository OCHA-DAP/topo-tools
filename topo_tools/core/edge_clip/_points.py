"""Shared border points: overlay tiles, per-input candidates, one point per cluster."""

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import (
    CLIP_PAIR_VERTEX_BUDGET,
    OVERLAY_VERTEX_SNAP,
    SHARED_POINT_TOLERANCE,
)
from topo_tools.core.coverage import grid_pairs
from topo_tools.core.duckdb_utils import bbox_columns_sql, subdivide_sql

T = SHARED_POINT_TOLERANCE
D = OVERLAY_VERTEX_SNAP
_TMP = (
    "_pt_i",
    "_pt_bat",
    "_pt_rb",
    "_pt_tb",
    "_pt_xr",
    "_pt_xp",
    "_pt_lv",
    "_pt_lseg",
    "_pt_iv",
    "_pt_ivp",
    "_pt_bc",
    "_pt_av",
    "_pt_cut",
    "_pt_bcc",
    "_pt_a0",
    "_pt_bap",
    "_pt_d",
    "_pt_dvp",
    "_pt_ccp",
    "_pt_cand",
)


def _xy_table(select: str) -> str:
    return f"""SELECT row_number() OVER () AS pid, x, y,
        x AS xmin, x AS xmax, y AS ymin, y AS ymax FROM ({select})"""


def build_tiles(
    conn: DuckDBPyConnection, overlay_source: str, fids_sql: str, prefix: str
) -> None:
    """Create `{prefix}_tiles` (tid, overlay_fid, geom, bbox) and `{prefix}_cut`.

    `{prefix}_cut` holds the tile vertices the subdivide adds per overlay fid;
    the empty candidate tables collect_candidates appends to are created too.
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{prefix}_tiles" AS
        SELECT row_number() OVER () AS tid, overlay_fid, geom,
               {bbox_columns_sql("geom")}
        FROM (
            SELECT fid AS overlay_fid, UNNEST({subdivide_sql("geom")}) AS geom
            FROM {overlay_source} WHERE fid IN ({fids_sql})
        )
    """)
    conn.execute(
        f'CREATE OR REPLACE TABLE "{prefix}_cut" '
        "(overlay_fid BIGINT, x DOUBLE, y DOUBLE)"
    )
    fids = conn.execute(
        f'SELECT DISTINCT overlay_fid FROM "{prefix}_tiles" ORDER BY 1'
    ).fetchall()
    for (g,) in fids:
        conn.execute(f"""--sql
            INSERT INTO "{prefix}_cut"
            SELECT {g}, x, y FROM (
                SELECT ST_X(d.geom) AS x, ST_Y(d.geom) AS y
                FROM (SELECT UNNEST(ST_Dump(ST_Points(geom))) AS d
                      FROM "{prefix}_tiles" WHERE overlay_fid = {g})
                EXCEPT SELECT ST_X(d.geom), ST_Y(d.geom)
                FROM (SELECT UNNEST(ST_Dump(ST_Points(geom))) AS d
                      FROM {overlay_source} WHERE fid = {g})
            )
        """)
    for table, cols in (
        ("cand", "pri INTEGER, x DOUBLE, y DOUBLE"),
        ("cutnear", "x DOUBLE, y DOUBLE"),
        ("dcand", "x DOUBLE, y DOUBLE"),
    ):
        conn.execute(f'CREATE OR REPLACE TABLE "{prefix}_{table}" ({cols})')


def pair_batches(
    conn: DuckDBPyConnection, rows: str, tiles: str, out: str, rows_where: str
) -> None:
    """Create `out` (fid, tid, b): row/tile bbox pairs, batched by row vertex count."""
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
            WHERE r.{rows_where}
        ) p
        JOIN (SELECT fid, ST_NPoints(geom) AS np FROM "{rows}" WHERE {rows_where}) n
          USING (fid)
    """)


def collect_candidates(conn: DuckDBPyConnection, input_table: str, prefix: str) -> None:
    """Append candidate points of `input_table` (fid, overlay_fid, geom), per fid."""
    x = conn.execute
    x(f"""--sql
        CREATE OR REPLACE TABLE _pt_i AS
        SELECT fid, overlay_fid, geom, {bbox_columns_sql("geom")} FROM "{input_table}"
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)
    fids = x("SELECT DISTINCT overlay_fid FROM _pt_i ORDER BY 1").fetchall()
    for (g,) in fids:
        pair_batches(conn, "_pt_i", f"{prefix}_tiles", "_pt_bat", f"overlay_fid = {g}")
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_rb AS
            SELECT fid, ST_Boundary(geom) AS bnd FROM _pt_i WHERE overlay_fid = {g}
        """)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_tb AS
            SELECT tid, ST_Boundary(geom) AS bnd FROM "{prefix}_tiles"
            WHERE overlay_fid = {g}
        """)
        x("CREATE OR REPLACE TABLE _pt_xr (fid BIGINT, x GEOMETRY)")
        for (b,) in x("SELECT DISTINCT b FROM _pt_bat ORDER BY 1").fetchall():
            x(f"""--sql
                INSERT INTO _pt_xr
                SELECT p.fid, ST_Intersection(r.bnd, t.bnd)
                FROM _pt_bat p JOIN _pt_rb r USING (fid) JOIN _pt_tb t USING (tid)
                WHERE p.b = {b}
            """)
        # Crossings: each point piece, and both ends of each line piece.
        x("""--sql
            CREATE OR REPLACE TABLE _pt_xp AS
            WITH pt AS (
                SELECT UNNEST(ST_Dump(ST_CollectionExtract(x, 1))) AS d FROM _pt_xr
            ),
            ln AS (
                SELECT UNNEST(ST_Dump(ST_CollectionExtract(x, 2))) AS d FROM _pt_xr
            )
            SELECT ST_X(p) AS x, ST_Y(p) AS y FROM (
                SELECT d.geom AS p FROM pt
                UNION ALL SELECT ST_StartPoint(d.geom) FROM ln
                UNION ALL SELECT ST_EndPoint(d.geom) FROM ln
            )
        """)
        x("""--sql
            CREATE OR REPLACE TABLE _pt_lv AS
            SELECT li, d.path[1] AS i, d.geom AS p
            FROM (
                SELECT li, UNNEST(ST_Dump(ST_Points(g))) AS d
                FROM (SELECT row_number() OVER () AS li,
                             UNNEST(ST_Dump(bnd)).geom AS g FROM _pt_tb)
            )
        """)
        x("""--sql
            CREATE OR REPLACE TABLE _pt_lseg AS
            SELECT row_number() OVER () AS pid, ST_MakeLine(p, np) AS g,
                   least(ST_X(p), ST_X(np)) AS xmin,
                   greatest(ST_X(p), ST_X(np)) AS xmax,
                   least(ST_Y(p), ST_Y(np)) AS ymin,
                   greatest(ST_Y(p), ST_Y(np)) AS ymax
            FROM (SELECT p, lead(p) OVER (PARTITION BY li ORDER BY i) AS np FROM _pt_lv)
            WHERE np IS NOT NULL
        """)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_iv AS {
            _xy_table(f'''
                SELECT DISTINCT ST_X(d.geom) AS x, ST_Y(d.geom) AS y
                FROM (SELECT UNNEST(ST_Dump(ST_Points(geom))) AS d FROM _pt_i
                      WHERE overlay_fid = {g}
                        AND fid IN (SELECT fid FROM _pt_xr WHERE NOT ST_IsEmpty(x)))
            ''')
        }
        """)
        grid_pairs(conn, "_pt_iv", "_pt_lseg", "_pt_ivp", on="TRUE", tol=T)
        # 2 = a crossing row's vertex within T of a tile segment, 3 = a crossing.
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_bc AS
            SELECT row_number() OVER () AS pid, pri, x, y,
                   x AS xmin, x AS xmax, y AS ymin, y AS ymax
            FROM (
                SELECT DISTINCT 2 AS pri, v.x, v.y
                FROM _pt_ivp p JOIN _pt_iv v ON v.pid = p.pid
                JOIN _pt_lseg s ON s.pid = p.near_pid
                WHERE ST_DWithin(ST_Point(v.x, v.y), s.g, {T})
                UNION SELECT DISTINCT 3, x, y FROM _pt_xp
            )
        """)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_av AS {
            _xy_table("SELECT DISTINCT ST_X(p) AS x, ST_Y(p) AS y FROM _pt_lv")
        }
        """)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_cut AS
            SELECT x, y FROM "{prefix}_cut" WHERE overlay_fid = {g}
        """)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_bcc AS {
            _xy_table("SELECT x, y FROM _pt_bc UNION SELECT x, y FROM _pt_cut")
        }
        """)
        x(
            "CREATE OR REPLACE TABLE _pt_a0 AS "
            "SELECT a.* FROM _pt_av a ANTI JOIN _pt_cut USING (x, y)"
        )
        grid_pairs(conn, "_pt_bcc", "_pt_a0", "_pt_bap", on="TRUE", tol=T)
        # Overlay vertices within D of a crossing row's vertex: rank 0, rows snap at D.
        grid_pairs(conn, "_pt_iv", "_pt_a0", "_pt_dvp", on="TRUE", tol=D)
        x(f"""--sql
            CREATE OR REPLACE TABLE _pt_d AS
            SELECT DISTINCT a.x, a.y
            FROM _pt_dvp p JOIN _pt_iv v ON v.pid = p.pid
            JOIN _pt_a0 a ON a.pid = p.near_pid
            WHERE (a.x - v.x) ^ 2 + (a.y - v.y) ^ 2 <= {D} ^ 2
        """)
        x(f'INSERT INTO "{prefix}_dcand" SELECT x, y FROM _pt_d')
        # 0 = an overlay vertex within T of any other candidate, 1 = a cut point.
        x(f"""--sql
            INSERT INTO "{prefix}_cand"
            SELECT DISTINCT 0, a.x, a.y
            FROM _pt_bap p JOIN _pt_bcc b ON b.pid = p.pid
            JOIN _pt_a0 a ON a.pid = p.near_pid
            WHERE (a.x - b.x) ^ 2 + (a.y - b.y) ^ 2 <= {T} ^ 2
            UNION SELECT pri, x, y FROM _pt_bc
            UNION SELECT 1, x, y FROM _pt_cut
            UNION SELECT 0, x, y FROM _pt_d
        """)
        # Cut points within T of an overlay vertex: tiles drop one before the snap.
        x(f"""--sql
            INSERT INTO "{prefix}_cutnear"
            SELECT DISTINCT b.x, b.y
            FROM _pt_bap p JOIN _pt_bcc b ON b.pid = p.pid
            JOIN _pt_a0 a ON a.pid = p.near_pid
            JOIN _pt_cut k ON k.x = b.x AND k.y = b.y
            WHERE (a.x - b.x) ^ 2 + (a.y - b.y) ^ 2 <= {T} ^ 2
        """)
    for t in _TMP:
        x(f"DROP TABLE IF EXISTS {t}")


def pick_points(conn: DuckDBPyConnection, prefix: str) -> None:
    """Create `{prefix}_pts` (x, y, p, pri): one candidate per cluster within T.

    A candidate is kept unless another within T ranks before it by (pri, x, y).
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _pt_cand AS
        SELECT row_number() OVER () AS pid, pri, x, y,
               x AS xmin, x AS xmax, y AS ymin, y AS ymax
        FROM (SELECT min(pri) AS pri, x, y FROM "{prefix}_cand" GROUP BY x, y)
    """)
    grid_pairs(conn, "_pt_cand", "_pt_cand", "_pt_ccp", on="l.pid <> r.pid", tol=T)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{prefix}_pts" AS
        SELECT x, y, ST_Point(x, y) AS p, pri FROM _pt_cand k
        WHERE NOT EXISTS (
            SELECT 1 FROM _pt_ccp c JOIN _pt_cand o ON o.pid = c.near_pid
            WHERE c.pid = k.pid AND (o.x - k.x) ^ 2 + (o.y - k.y) ^ 2 <= {T} ^ 2
              AND (o.pri, o.x, o.y) < (k.pri, k.x, k.y)
        )
    """)
    for t in ("_pt_cand", "_pt_ccp"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
