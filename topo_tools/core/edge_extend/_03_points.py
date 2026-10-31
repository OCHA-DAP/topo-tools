"""Creates interpolated points along boundary lines at configurable intervals."""

from decimal import Decimal

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.duckdb_utils import bbox_columns_sql

from ._constants import MAX_POINTS_PER_SEGMENT


def build_segments(conn: DuckDBPyConnection, name: str) -> None:
    """Build vertex-to-vertex segments (_03_tmp1) and the per-fid boundary zone (_03a).

    Both are DISTANCE-independent, built once and reused across every retry.
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp1" AS
        WITH lines AS (
            SELECT row_number() OVER () AS lid, fid, geom
            FROM "{name}_02"
        ), verts AS (
            SELECT
                lid, fid,
                UNNEST(ST_Dump(ST_Points(geom))).geom AS geom,
                UNNEST(ST_Dump(ST_Points(geom))).path[1] AS idx
            FROM lines
        )
        SELECT
            fid,
            ST_MakeLine(prev_geom, geom) AS geom,
            ST_Distance(prev_geom, geom) AS seg_len
        FROM (
            SELECT
                fid, geom,
                LAG(geom) OVER (PARTITION BY lid ORDER BY idx) AS prev_geom
            FROM verts
        )
        WHERE prev_geom IS NOT NULL
    """)

    # Per-fid, bbox-tagged buffered boundary zone (not one whole-file blob),
    # so _03b can difference each fid against only nearby fids' zones.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03a" AS
        WITH zone AS (
            SELECT fid, ST_Union_Agg(ST_Buffer(ST_Boundary(geom), {SNAP_TOLERANCE}))
                AS geom
            FROM "{name}_02"
            GROUP BY fid
        )
        SELECT fid, geom, {bbox_columns_sql("geom")} FROM zone
    """)


def estimate_count(conn: DuckDBPyConnection, name: str, distance: Decimal) -> int:
    """Estimate main()'s point count from "{name}_03_tmp1", before differencing."""
    d = float(distance)
    return conn.execute(f"""--sql
        SELECT (
            count(*) FILTER (WHERE seg_len > {d * MAX_POINTS_PER_SEGMENT!r})
                * {MAX_POINTS_PER_SEGMENT}
            + coalesce(
                sum(seg_len) FILTER (WHERE seg_len <= {d * MAX_POINTS_PER_SEGMENT!r}),
                0
            ) / {d!r}
        )::BIGINT
        FROM "{name}_03_tmp1"
    """).fetchone()[0]


def main(
    conn: DuckDBPyConnection, name: str, distance: Decimal, *, debug: bool = False
) -> None:
    """Create points along boundary lines.

    Assumes build_segments has already created "{name}_03_tmp1".
    """
    d = float(distance)
    cap_threshold = d * MAX_POINTS_PER_SEGMENT

    # Long segments cap interpolation directly; normal segments re-merge into
    # per-fid lines first, avoiding a floor equal to the file's raw vertex count.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp2" AS
        SELECT
            fid,
            ST_LineInterpolatePoints(
                geom,
                GREATEST(
                    LEAST({d!r} / seg_len, 1.0),
                    1.0 / {MAX_POINTS_PER_SEGMENT}
                ),
                true
            ) AS geom
        FROM "{name}_03_tmp1"
        WHERE seg_len > {cap_threshold!r}
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp3" AS
        SELECT
            fid,
            ST_LineInterpolatePoints(
                geom,
                LEAST({d!r} / ST_Length(geom), 1.0),
                true
            ) AS geom
        FROM (
            SELECT fid, UNNEST(ST_Dump(ST_LineMerge(ST_Union_Agg(geom)))).geom
                AS geom
            FROM "{name}_03_tmp1"
            WHERE seg_len <= {cap_threshold!r}
            GROUP BY fid
        )
    """)

    # Aggregated to one multipoint per fid before differencing: per-segment
    # differencing scales call count with segment count, not fid count.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_tmp4" AS
        SELECT fid, ST_Union_Agg(geom) AS geom FROM (
            SELECT fid, geom FROM "{name}_03_tmp2"
            UNION ALL
            SELECT fid, geom FROM "{name}_03_tmp3"
        )
        GROUP BY fid
    """)

    # Differences each fid against a bbox-prefiltered local union of nearby
    # fids' zones only, never the whole file's zone as one operand.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03b" AS
        WITH
        tmp4_bbox AS (
            SELECT fid, geom, {bbox_columns_sql("geom")} FROM "{name}_03_tmp4"
        ),
        tmp4_local AS (
            SELECT a.fid AS afid, ST_Union_Agg(b.geom) AS geom
            FROM tmp4_bbox a
            JOIN "{name}_03a" b
              ON b.xmax >= a.xmin AND b.xmin <= a.xmax
             AND b.ymax >= a.ymin AND b.ymin <= a.ymax
             AND ST_Intersects(a.geom, b.geom)
            GROUP BY a.fid
        ),
        lines_bbox AS (
            SELECT row_number() OVER () AS rid, fid, geom, {bbox_columns_sql("geom")}
            FROM "{name}_02"
        ),
        lines_local AS (
            SELECT a.rid, a.fid AS afid, ST_Union_Agg(b.geom) AS geom
            FROM lines_bbox a
            JOIN "{name}_03a" b
              ON b.xmax >= a.xmin AND b.xmin <= a.xmax
             AND b.ymax >= a.ymin AND b.ymin <= a.ymax
             AND ST_Intersects(a.geom, b.geom)
            GROUP BY a.rid, a.fid
        )
        SELECT fid, geom FROM (
            SELECT
                a.fid,
                UNNEST(ST_Dump(
                    CASE WHEN n.geom IS NULL THEN a.geom
                        ELSE ST_Difference(a.geom, n.geom) END
                )).geom AS geom
            FROM tmp4_bbox a
            LEFT JOIN tmp4_local n ON n.afid = a.fid
            UNION ALL
            SELECT
                a.fid,
                UNNEST(ST_Dump(ST_Boundary(
                    CASE WHEN n.geom IS NULL THEN a.geom
                        ELSE ST_Difference(a.geom, n.geom) END
                ))).geom AS geom
            FROM lines_bbox a
            LEFT JOIN lines_local n ON n.rid = a.rid
        )
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)

    missing = conn.execute(f"""--sql
        SELECT count(*) FROM (
            SELECT DISTINCT fid FROM "{name}_03_tmp4"
            EXCEPT
            SELECT DISTINCT fid FROM "{name}_03b"
        )
    """).fetchall()[0][0]
    if missing > 0:
        msg = f"shared-boundary difference dropped {missing} fid(s) entirely"
        raise RuntimeError(msg)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp2"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp4"')
