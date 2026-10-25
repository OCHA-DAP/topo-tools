"""Assigns each input file to the overlay polygon its features overlap most."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import (
    CRS,
    EQUAL_AREA_CRS,
    GEOMETRY_TYPE,
    SNAP_TOLERANCE,
    SUBDIVIDE_MIN_VERTICES,
)
from topo_tools.core.duckdb_utils import bbox_columns_sql, subdivide_sql

logger = getLogger(__name__)


def input_bbox_extent(
    conn: DuckDBPyConnection, name: str
) -> tuple[float, float, float, float] | None:
    """Return the combined bbox of every row in `{name}_input_01`, or None if empty."""
    row = conn.execute(f"""--sql
        SELECT MIN(xmin), MIN(ymin), MAX(xmax), MAX(ymax)
        FROM (SELECT {bbox_columns_sql("geom")} FROM "{name}_input_01")
    """).fetchone()
    return None if row[0] is None else (row[0], row[1], row[2], row[3])


def union_bbox(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """Combine two (xmin, ymin, xmax, ymax) bboxes into their enclosing bbox."""
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def bboxes_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    margin: float,
) -> bool:
    """Whether two (xmin, ymin, xmax, ymax) bboxes overlap within margin."""
    return (
        a[0] <= b[2] + margin
        and a[2] >= b[0] - margin
        and a[1] <= b[3] + margin
        and a[3] >= b[1] - margin
    )


def overlay_fid_bboxes(
    conn: DuckDBPyConnection, table: str
) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Return each non-empty overlay row's fid and (xmin, ymin, xmax, ymax) extent."""
    rows = conn.execute(f"""--sql
        SELECT fid, xmin, ymin, xmax, ymax
        FROM (SELECT fid, {bbox_columns_sql("geom")} FROM "{table}")
        WHERE xmin IS NOT NULL
    """).fetchall()
    return [(r[0], (r[1], r[2], r[3], r[4])) for r in rows]


def load_nearby_overlay(
    conn: DuckDBPyConnection,
    name: str,
    fid_bboxes: list[tuple[int, tuple[float, float, float, float]]],
) -> None:
    """Copy into `{name}_overlay_01` only the overlay rows near `{name}_input_01`."""
    bbox = input_bbox_extent(conn, name)
    nearby = [
        str(fid)
        for fid, obox in fid_bboxes
        if bbox is not None and bboxes_overlap(bbox, obox, 2 * SNAP_TOLERANCE)
    ]
    where = f"WHERE fid IN ({','.join(nearby)})" if nearby else "WHERE FALSE"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_overlay_01" AS
        SELECT * FROM "{name}_overlay_full" {where}
    """)


def prepare_overlay_tiles(
    conn: DuckDBPyConnection,
    name: str,
    *,
    input_bbox: tuple[float, float, float, float] | None = None,
) -> None:
    """Precompute the overlay tile decomposition, skipping parts outside input_bbox."""
    bbox_where = ""
    if input_bbox is not None:
        cxmin, cymin, cxmax, cymax = input_bbox
        bbox_where = f"""
            WHERE xmax >= {cxmin} AND xmin <= {cxmax}
              AND ymax >= {cymin} AND ymin <= {cymax}
        """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_overlay_parts" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_overlay_01"
        ),
        bboxed AS (
            SELECT fid, part_geom,
                   ST_NPoints(part_geom) AS n_points, {bbox_columns_sql("part_geom")}
            FROM parts
        )
        SELECT row_number() OVER () AS part_id, *
        FROM bboxed
        {bbox_where}
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_overlay_tiles" (
            overlay_fid BIGINT, geom {GEOMETRY_TYPE},
            xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE
        )
    """)
    heavy = conn.execute(f"""--sql
        SELECT count(*) FROM "{name}_02_overlay_parts"
        WHERE n_points >= {SUBDIVIDE_MIN_VERTICES}
    """).fetchone()[0]
    if heavy:
        logger.info(
            "assign-one: splitting %d heavy overlay part(s) (>= %d vertices)",
            heavy,
            SUBDIVIDE_MIN_VERTICES,
        )
    conn.execute(f"""--sql
        INSERT INTO "{name}_02_overlay_tiles" BY NAME
        WITH t AS (
            SELECT fid AS overlay_fid, UNNEST({subdivide_sql("part_geom")}) AS geom
            FROM "{name}_02_overlay_parts"
            WHERE n_points >= {SUBDIVIDE_MIN_VERTICES}
        )
        SELECT overlay_fid, geom, {bbox_columns_sql("geom")} FROM t
    """)


def _build_pairs(
    conn: DuckDBPyConnection, name: str, *, use_cached_tiles: bool = False
) -> None:
    """Bbox-prefiltered overlap-area join, tiling any oversized overlay part first.

    use_cached_tiles=True reuses a prior prepare_overlay_tiles() call's tiles
    instead of rebuilding them.
    """
    if not use_cached_tiles:
        prepare_overlay_tiles(conn, name, input_bbox=input_bbox_extent(conn, name))

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_input_01"
        )
        SELECT fid, part_geom, {bbox_columns_sql("part_geom")}
        FROM parts
    """)

    # Overlay parts below the tiling threshold: exact intersection directly.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_pairs_raw" AS
        SELECT c.fid AS input_fid, p.fid AS overlay_fid,
               SUM(ST_Area(ST_Transform(
                   ST_Intersection(c.part_geom, p.part_geom),
                   '{CRS}', '{EQUAL_AREA_CRS}', true
               ))) AS shared_area
        FROM "{name}_02_tmp1" c
        JOIN "{name}_02_overlay_parts" p
          ON p.xmax >= c.xmin AND p.xmin <= c.xmax
         AND p.ymax >= c.ymin AND p.ymin <= c.ymax
         AND ST_Intersects(c.part_geom, p.part_geom)
        WHERE p.n_points < {SUBDIVIDE_MIN_VERTICES}
        GROUP BY c.fid, p.fid
    """)

    # Oversized overlay parts: input polygons joined to the precomputed tile set at
    # once, tiles already tagged with their own overlay_fid.
    conn.execute(f"""--sql
        INSERT INTO "{name}_02_pairs_raw"
        SELECT c.fid AS input_fid, t.overlay_fid AS overlay_fid,
               SUM(ST_Area(ST_Transform(
                   ST_Intersection(c.part_geom, t.geom),
                   '{CRS}', '{EQUAL_AREA_CRS}', true
               ))) AS shared_area
        FROM "{name}_02_tmp1" c
        JOIN "{name}_02_overlay_tiles" t
          ON t.xmax >= c.xmin AND t.xmin <= c.xmax
         AND t.ymax >= c.ymin AND t.ymin <= c.ymax
         AND ST_Intersects(c.part_geom, t.geom)
        GROUP BY c.fid, t.overlay_fid
    """)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_pairs" AS
        SELECT input_fid, overlay_fid, SUM(shared_area) AS shared_area
        FROM "{name}_02_pairs_raw"
        GROUP BY input_fid, overlay_fid
    """)

    drop_tables = ["_02_tmp1", "_02_pairs_raw"]
    if not use_cached_tiles:
        drop_tables += ["_02_overlay_parts", "_02_overlay_tiles"]
    for tbl in drop_tables:
        conn.execute(f'DROP TABLE IF EXISTS "{name}{tbl}"')


_RESERVED_ASSIGN_COLUMNS = {
    "input_fid",
    "overlay_fid",
    "assignment_method",
    "spatial_agrees",
}


def _carry_forward_columns(
    conn: DuckDBPyConnection,
    name: str,
    carry_columns: list[str] | None,
    input_columns: list[str] | None = None,
) -> None:
    """Append caller-named overlay columns onto `_02_assign`; does nothing if unset."""
    if not carry_columns:
        return
    reserved_collisions = set(carry_columns) & _RESERVED_ASSIGN_COLUMNS
    if reserved_collisions:
        msg = (
            f"carry_columns collides with reserved assign column(s): "
            f"{sorted(reserved_collisions)}"
        )
        raise ValueError(msg)
    input_column_set = (
        set(input_columns)
        if input_columns is not None
        else {row[0] for row in conn.execute(f'DESCRIBE "{name}_input_01"').fetchall()}
    )
    input_collisions = set(carry_columns) & input_column_set
    if input_collisions:
        msg = (
            f"carry_columns collides with the input layer's own column(s): "
            f"{sorted(input_collisions)}"
        )
        raise ValueError(msg)
    carry_sql = "".join(f', p."{c}" AS "{c}"' for c in carry_columns)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_assign" AS
        SELECT a.*{carry_sql}
        FROM "{name}_02_assign" a
        JOIN "{name}_overlay_01" p ON p.fid = a.overlay_fid
    """)


def assign_one(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    *,
    use_cached_tiles: bool = False,
    overlay_match_column: str | None = None,
    input_match_column: str | None = None,
    carry_columns: list[str] | None = None,
    input_columns: list[str] | None = None,
) -> None:
    """Force every input polygon in a source_file onto its file's majority overlay."""
    _build_pairs(conn, name, use_cached_tiles=use_cached_tiles)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_file_votes" AS
        SELECT c.source_file, pr.overlay_fid, COUNT(DISTINCT pr.input_fid) AS n_inputs
        FROM "{name}_02_pairs" pr
        JOIN "{name}_input_01" c ON c.fid = pr.input_fid
        WHERE pr.shared_area > 0
        GROUP BY c.source_file, pr.overlay_fid
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_file_winner" AS
        SELECT source_file, overlay_fid FROM (
            SELECT source_file, overlay_fid,
                   ROW_NUMBER() OVER (
                       PARTITION BY source_file ORDER BY n_inputs DESC, overlay_fid ASC
                   ) AS rn
            FROM "{name}_02_file_votes"
        ) WHERE rn = 1
    """)
    if overlay_match_column and input_match_column:
        # Per-feature code candidate, restricted to an overlay it overlaps at all,
        # rolled up to a per-file majority, preserving one-overlay-per-file.
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
            SELECT c.source_file, p.fid AS overlay_fid,
                   COUNT(DISTINCT c.fid) AS n_inputs
            FROM "{name}_input_01" c
            JOIN "{name}_overlay_01" p
              ON c."{input_match_column}" = p."{overlay_match_column}"
            JOIN "{name}_02_pairs" pr
              ON pr.input_fid = c.fid AND pr.overlay_fid = p.fid AND pr.shared_area > 0
            GROUP BY c.source_file, p.fid
        """)
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
            SELECT source_file, overlay_fid FROM (
                SELECT source_file, overlay_fid,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_file
                           ORDER BY n_inputs DESC, overlay_fid ASC
                       ) AS rn
                FROM "{name}_02_tmp3"
            ) WHERE rn = 1
        """)
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_file_final" AS
            SELECT
                source_file,
                COALESCE(code.overlay_fid, spatial.overlay_fid) AS overlay_fid,
                CASE WHEN code.overlay_fid IS NOT NULL THEN 'code'
                     ELSE 'spatial_fallback' END AS assignment_method,
                CASE WHEN code.overlay_fid IS NOT NULL
                     THEN code.overlay_fid = spatial.overlay_fid END AS spatial_agrees
            FROM "{name}_02_tmp4" code
            FULL OUTER JOIN "{name}_02_file_winner" spatial USING (source_file)
        """)
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp3"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp4"')
        winner_table = f"{name}_02_file_final"
        extra_cols = ", w.assignment_method, w.spatial_agrees"
    else:
        winner_table = f"{name}_02_file_winner"
        extra_cols = ""

    # Every input polygon rides its file's winner unconditionally; a truly
    # non-overlapping one still drops later, at clip time.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_assign" AS
        SELECT c.fid AS input_fid, w.overlay_fid{extra_cols}
        FROM "{name}_input_01" c
        JOIN "{winner_table}" w ON w.source_file = c.source_file
    """)
    if overlay_match_column and input_match_column:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_file_final"')

    _carry_forward_columns(conn, name, carry_columns, input_columns)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_unassigned" AS
        SELECT fid AS input_fid, source_file, geom
        FROM "{name}_input_01"
        WHERE fid NOT IN (SELECT input_fid FROM "{name}_02_assign")
    """)

    unassigned = conn.execute(f"""--sql
        SELECT input_fid FROM "{name}_02_unassigned" ORDER BY input_fid
    """).fetchall()
    if unassigned:
        fids = [row[0] for row in unassigned]
        logger.warning(
            "assign-one: dropping %d input fid(s) whose file had no overlay polygon "
            "overlap at all: %s",
            len(fids),
            fids,
        )

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_file_votes"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_file_winner"')

    used_fids = [
        row[0]
        for row in conn.execute(
            f'SELECT DISTINCT overlay_fid FROM "{name}_02_assign"'
        ).fetchall()
    ]
    where = (
        f"WHERE fid IN ({','.join(str(f) for f in used_fids)})"
        if used_fids
        else "WHERE FALSE"
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_overlay_01" AS
        SELECT * FROM "{name}_overlay_01" {where}
    """)
