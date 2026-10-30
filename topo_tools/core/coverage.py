"""Shared coverage-topology validation and repair helpers."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import (
    DETACHED_MAX_ORIGINAL_SHARE,
    DETACHED_MERGE_MAX_RATIO,
    DETACHED_MIN_NECK_RATIO,
    NOTCH_MAX_GAP_RATIO,
    NOTCH_MIN_SCORE,
    NOTCH_SPACING,
    NOTCH_WINDOW_MARGIN,
    SNAP_ESCALATION_MAX_STEPS,
    SNAP_ESCALATION_STEP,
    SNAP_TOLERANCE,
)
from topo_tools.core.duckdb_utils import bbox_columns_sql, subdivide_sql
from topo_tools.core.units import METERS_PER_DEGREE, m2_per_deg2_factor

logger = getLogger(__name__)


def check_no_erosion(
    conn: DuckDBPyConnection,
    table_before: str,
    table_after: str,
    *,
    buffer: float = SNAP_TOLERANCE,
) -> None:
    """Raise RuntimeError if any fid's table_after geometry eroded its table_before.

    SNAP_TOLERANCE-buffered, not exact ST_Covers: GEOS leaves float noise
    far below this scale on every polygon it touches, real erosion doesn't.
    """
    eroded = conn.execute(f"""--sql
        SELECT count(*) FROM "{table_before}" o
        JOIN "{table_after}" e USING (fid)
        WHERE NOT ST_Covers(ST_Buffer(e.geom, {buffer}), o.geom)
    """).fetchall()[0][0]
    if eroded > 0:
        msg = f"extension eroded the original footprint of {eroded} fid(s)"
        raise RuntimeError(msg)


def grid_pairs(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    left: str,
    right: str,
    out: str,
    *,
    on: str,
    tol: float,
    key: str = "pid",
) -> None:
    """Create `out` (pid, near_pid): left/right rows whose tol-padded bboxes overlap.

    Joins on grid cells, counting each pair in the cell of its overlap's min corner.
    """
    # A 99th-percentile-extent cell keeps both cells per row and rows per cell small.
    cell = conn.execute(f"""--sql
        SELECT greatest(quantile_cont(greatest(xmax - xmin, ymax - ymin), 0.99), {tol})
        FROM (
            SELECT xmin, xmax, ymin, ymax FROM "{left}"
            UNION ALL SELECT xmin, xmax, ymin, ymax FROM "{right}"
        )
    """).fetchone()[0]

    def cells(table: str, a: str) -> str:
        return f"""(
            SELECT {a}.*, cx, cy
            FROM (
                SELECT *, xmin - {tol} AS x0, ymin - {tol} AS y0,
                       xmax + {tol} AS x1, ymax + {tol} AS y1
                FROM "{table}"
            ) {a},
            LATERAL (SELECT UNNEST(range(
                floor({a}.x0 / {cell})::BIGINT, floor({a}.x1 / {cell})::BIGINT + 1
            )) AS cx),
            LATERAL (SELECT UNNEST(range(
                floor({a}.y0 / {cell})::BIGINT, floor({a}.y1 / {cell})::BIGINT + 1
            )) AS cy)
        )"""

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{out}" AS
        SELECT l.{key} AS pid, r.{key} AS near_pid
        FROM {cells(left, "l")} l JOIN {cells(right, "r")} r USING (cx, cy)
        WHERE r.x0 <= l.x1 AND r.x1 >= l.x0 AND r.y0 <= l.y1 AND r.y1 >= l.y0
          AND cx = floor(greatest(l.x0, r.x0) / {cell})::BIGINT
          AND cy = floor(greatest(l.y0, r.y0) / {cell})::BIGINT
          AND ({on})
    """)


def _near_part_pairs(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    left: str,
    right: str,
    out: str,
    *,
    on: str = "TRUE",
    tol: float = SNAP_TOLERANCE,
) -> None:
    """Create `out` (pid, near_pid): left/right parts within tol, `on` over l and r.

    Both tables need pid, geom and bbox columns; heavy parts are compared split.
    """
    grid_pairs(conn, left, right, f"{out}_cand", on=on, tol=tol)
    cand_l = f'SELECT pid FROM "{out}_cand"'
    cand_r = f'SELECT near_pid FROM "{out}_cand"'
    sides = {"l": (left, cand_l), "r": (right, cand_r)}
    if left == right:
        sides = {"l": (left, f"{cand_l} UNION {cand_r}")}
    for side, (table, pids) in sides.items():
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{out}_{side}" AS
            WITH s AS (
                SELECT pid, UNNEST({subdivide_sql("geom")}) AS geom FROM "{table}"
                WHERE pid IN ({pids})
            )
            SELECT row_number() OVER () AS qid, pid, geom, {bbox_columns_sql("geom")}
            FROM s
        """)
    rp = f"{out}_{'l' if left == right else 'r'}"
    grid_pairs(conn, f"{out}_l", rp, f"{out}_pp", on="TRUE", tol=tol, key="qid")
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{out}" AS
        WITH j AS (
            SELECT a.pid, b.pid AS near_pid, a.geom AS ag, b.geom AS bg
            FROM "{out}_pp" pp
            JOIN "{out}_l" a ON a.qid = pp.pid JOIN "{rp}" b ON b.qid = pp.near_pid
        )
        SELECT DISTINCT pid, near_pid FROM j
        SEMI JOIN "{out}_cand" USING (pid, near_pid) WHERE ST_DWithin(ag, bg, {tol})
    """)
    for side in ("cand", "l", "r", "pp"):
        conn.execute(f'DROP TABLE IF EXISTS "{out}_{side}"')


def has_invalid_edges(conn: DuckDBPyConnection, table: str) -> bool:
    """Return True if `table.geom` has any overlaps or unmatched shared edges."""
    # A part with no other part within the gap width can't hold an invalid edge, and
    # GEOS rebuilds a large neighbour's index per such part.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _inv_parts AS
        WITH d AS (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
        SELECT row_number() OVER () AS pid, geom, {bbox_columns_sql("geom")} FROM d
    """)
    _near_part_pairs(conn, "_inv_parts", "_inv_parts", "_inv_near", on="l.pid < r.pid")
    invalid = conn.execute(f"""--sql
        SELECT ST_CoverageInvalidEdges_Agg(geom, {SNAP_TOLERANCE}) IS NOT NULL
        FROM _inv_parts
        WHERE pid IN (SELECT pid FROM _inv_near UNION SELECT near_pid FROM _inv_near)
    """).fetchall()[0][0]
    for tmp in ("_inv_parts", "_inv_near"):
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    return invalid


def check_invalid_edges(conn: DuckDBPyConnection, table: str) -> None:
    """Raise RuntimeError if `table.geom` has any overlaps or unmatched shared edges."""
    if has_invalid_edges(conn, table):
        error = f"INVALID_EDGES: {table}"
        logger.error(error)
        raise RuntimeError(error)


def _union_sql(table: str, *, coverage: bool) -> str:
    """Build SQL for a one-row `g` union of `table.geom`'s polygon parts.

    coverage=True needs a table that has passed has_invalid_edges.
    """
    agg = "ST_CoverageUnion_Agg" if coverage else "ST_Union_Agg"
    return f"""
        SELECT {agg}(geom) AS g
        FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
        WHERE ST_Dimension(geom) = 2
    """


def gap_geometries_sql(table: str, *, coverage: bool = False) -> str:
    """Build SQL for a subquery of individual fully-enclosed interior-hole geometries.

    Dumps the union into parts first: ST_NumInteriorRings returns NULL on a
    MultiPolygon, and ST_Difference needs one polygon's own exterior ring.
    """
    return f"""(
        WITH u AS ({_union_sql(table, coverage=coverage)}),
        parts AS (
            SELECT (UNNEST(ST_Dump(g))).geom AS poly FROM u WHERE g IS NOT NULL
        ),
        holes AS (
            SELECT UNNEST(ST_Dump(
                ST_Difference(ST_MakePolygon(ST_ExteriorRing(poly)), poly)
            )).geom AS geom
            FROM parts WHERE ST_NumInteriorRings(poly) > 0
        )
        SELECT geom FROM holes WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    )"""


def gap_issues_sql(
    conn: DuckDBPyConnection,
    table: str,
    *,
    min_width: float = SNAP_TOLERANCE,
    within: str | None = None,
    coverage: bool = False,
) -> str:
    """Build SQL for `table.geom`'s gap-kind issue rows, in the shared issues schema.

    `within` (a query yielding `geom`) keeps only gaps whose interior point it covers.
    """
    m2_per_deg2 = m2_per_deg2_factor(conn, table)
    width_m = f"(ST_MaximumInscribedCircle(geom)).radius * 2 * {METERS_PER_DEGREE}"
    thinness_ratio = "4 * pi() * ST_Area(geom) / POWER(ST_Perimeter(geom), 2)"
    return f"""
        SELECT 'gap-' || row_number() OVER (ORDER BY hash(geom)) AS key, 'gap' AS kind,
               NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b,
               NULL::BIGINT AS overlay_fid, NULL::VARCHAR AS reason,
               ST_Area(geom) * {m2_per_deg2} AS area_m2, {width_m} AS max_width_m,
               {thinness_ratio} AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::DOUBLE AS unit_a_area_change_m2,
               NULL::DOUBLE AS unit_b_area_change_m2,
               NULL::DOUBLE AS filled_area_m2, FALSE AS fixed,
               NULL::VARCHAR AS source_file, geom
        FROM {gap_geometries_sql(table, coverage=coverage)} g
        WHERE (ST_MaximumInscribedCircle(geom)).radius * 2 > {min_width}
        {"" if within is None else _point_within_sql("g.geom", within)}
    """


def _point_within_sql(geom: str, within: str) -> str:
    """Build an AND clause: `geom`'s interior point lies in a `within` geometry."""
    return f"""AND EXISTS (
            SELECT 1 FROM (
                SELECT geom, ST_XMin(geom) AS xmin, ST_XMax(geom) AS xmax,
                       ST_YMin(geom) AS ymin, ST_YMax(geom) AS ymax
                FROM ({within})
            ) w, (SELECT ST_PointOnSurface({geom}) AS pt) q
            WHERE ST_X(q.pt) BETWEEN w.xmin AND w.xmax
              AND ST_Y(q.pt) BETWEEN w.ymin AND w.ymax
              AND ST_Intersects(w.geom, q.pt)
        )"""


def short_source_file_sql(column: str) -> str:
    """Build SQL shortening a full source path to its last two path parts."""
    backslash = chr(92)
    posix_column = f"replace({column}, '{backslash}', '/')"
    return f"array_to_string(list_slice(str_split({posix_column}, '/'), -2, -1), '/')"


def assign_issue_rows_sql(name: str, *, source_file_expr: str = "NULL::VARCHAR") -> str:
    """Build SQL for `{name}_02_assign`'s code-join issue rows, in the shared schema.

    Requires `assignment_method`/`spatial_agrees` (assign_one/assign_many
    called with match columns); `source_file_expr` needs a real column or NULL.
    """
    return f"""
        SELECT 'code-mismatch-' || a.input_fid AS key, 'code-mismatch' AS kind,
               a.input_fid AS unit_a, NULL::BIGINT AS unit_b, a.overlay_fid,
               'code join picked a different overlay polygon than spatial majority'
               AS reason,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::DOUBLE AS unit_a_area_change_m2,
               NULL::DOUBLE AS unit_b_area_change_m2,
               NULL::DOUBLE AS filled_area_m2, FALSE AS fixed,
               {source_file_expr} AS source_file, c.geom
        FROM "{name}_02_assign" a
        JOIN "{name}_input_01" c ON c.fid = a.input_fid
        WHERE a.assignment_method = 'code' AND a.spatial_agrees IS NOT TRUE
        UNION ALL BY NAME
        SELECT 'code-fallback-' || a.input_fid AS key, 'code-fallback' AS kind,
               a.input_fid AS unit_a, NULL::BIGINT AS unit_b, a.overlay_fid,
               'no matching code; fell back to spatial majority' AS reason,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::DOUBLE AS unit_a_area_change_m2,
               NULL::DOUBLE AS unit_b_area_change_m2,
               NULL::DOUBLE AS filled_area_m2, FALSE AS fixed,
               {source_file_expr} AS source_file, c.geom
        FROM "{name}_02_assign" a
        JOIN "{name}_input_01" c ON c.fid = a.input_fid
        WHERE a.assignment_method = 'spatial_fallback'
    """


def has_gaps(
    conn: DuckDBPyConnection,
    table: str,
    *,
    gap_maximum_width: float = SNAP_TOLERANCE,
    coverage: bool = False,
) -> bool:
    """Return True if `table.geom` has an interior hole at or below gap_maximum_width.

    A wider hole may be a real geographic absence, not a defect;
    gap_maximum_width=0 tolerates no hole of any size.
    """
    if gap_maximum_width == 0:
        max_interior_rings = conn.execute(f"""--sql
            WITH u AS ({_union_sql(table, coverage=coverage)})
            SELECT MAX(ST_NumInteriorRings(part))
            FROM (SELECT UNNEST(ST_Dump(g)).geom AS part FROM u)
        """).fetchall()[0][0]
        return (max_interior_rings or 0) > 0

    return conn.execute(f"""--sql
        SELECT EXISTS (
            SELECT 1 FROM {gap_geometries_sql(table, coverage=coverage)} h
            WHERE (ST_MaximumInscribedCircle(h.geom)).radius * 2 <= {gap_maximum_width}
        )
    """).fetchall()[0][0]


def check_gaps(
    conn: DuckDBPyConnection,
    table: str,
    *,
    gap_maximum_width: float = SNAP_TOLERANCE,
    coverage: bool = False,
) -> None:
    """Raise RuntimeError if `table.geom`'s union has a qualifying interior hole."""
    if has_gaps(conn, table, gap_maximum_width=gap_maximum_width, coverage=coverage):
        error = f"GAPS: {table}"
        logger.error(error)
        raise RuntimeError(error)


def count_gaps(conn: DuckDBPyConnection, table: str, *, min_width: float = 0) -> int:
    """Count interior holes in `table.geom`'s union that are wider than min_width."""
    return conn.execute(f"""--sql
        SELECT COUNT(*) FROM {gap_geometries_sql(table)} h
        WHERE (ST_MaximumInscribedCircle(h.geom)).radius * 2 > {min_width}
    """).fetchall()[0][0]


def is_micro_sql(geom: str, width: float = SNAP_TOLERANCE) -> str:
    """Build SQL testing whether polygon `geom` is at most `width` wide.

    2*area/perimeter bounds the inscribed-circle diameter from below, so CASE
    skips the costly ST_MaximumInscribedCircle for every clearly wide part.
    """
    return (
        f"CASE WHEN ST_IsEmpty({geom}) THEN FALSE "
        f"WHEN 2 * ST_Area({geom}) <= {width} * ST_Perimeter({geom}) "
        f"THEN (ST_MaximumInscribedCircle({geom})).radius * 2 <= {width} "
        "ELSE FALSE END"
    )


def has_micro_polygons(conn: DuckDBPyConnection, table: str) -> bool:
    """Return True if any polygon part of `table.geom` is SNAP_TOLERANCE-narrow."""
    return conn.execute(f"""--sql
        SELECT EXISTS (
            SELECT 1
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
            WHERE {is_micro_sql("geom")}
        )
    """).fetchall()[0][0]


def check_micro_polygons(conn: DuckDBPyConnection, table: str) -> None:
    """Raise RuntimeError if any polygon part of `table.geom` is a micro-polygon."""
    if has_micro_polygons(conn, table):
        error = f"MICRO_POLYGONS: {table}"
        logger.error(error)
        raise RuntimeError(error)


_EMPTY_ISSUES_SQL = (
    "SELECT NULL::VARCHAR AS key, NULL::VARCHAR AS kind, NULL::BIGINT AS unit_a, "
    "NULL::BIGINT AS unit_b, NULL::BIGINT AS overlay_fid, NULL::VARCHAR AS reason, "
    "NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m, "
    "NULL::DOUBLE AS thinness_ratio, NULL::DOUBLE AS near_length_m, "
    "NULL::DOUBLE AS unit_a_area_change_m2, "
    "NULL::DOUBLE AS unit_b_area_change_m2, NULL::DOUBLE AS filled_area_m2, "
    "NULL::BOOLEAN AS fixed, NULL::VARCHAR AS source_file, NULL::GEOMETRY AS geom "
    "WHERE FALSE"
)


def merge_micro_polygons(
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    *,
    issues_table: str,
) -> int:
    """Merge each micro-polygon part into its neighbour, or drop it; return the count.

    The neighbour is the non-micro part the SNAP_TOLERANCE-buffered micro part
    overlaps most (ties to lowest fid), in any row including its own.
    """
    if not has_micro_polygons(conn, table_in):
        conn.execute(f'CREATE OR REPLACE TABLE "{issues_table}" AS {_EMPTY_ISSUES_SQL}')
        if table_out != table_in:
            conn.execute(
                f'CREATE OR REPLACE TABLE "{table_out}" AS SELECT * FROM "{table_in}"'
            )
        return 0
    tol = SNAP_TOLERANCE
    columns = {r[0] for r in conn.execute(f'DESCRIBE "{table_in}"').fetchall()}
    src = "source_file" if "source_file" in columns else "NULL::VARCHAR"
    m2_per_deg2 = m2_per_deg2_factor(conn, table_in)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _micro_all AS
        SELECT row_number() OVER () AS rnid, * FROM "{table_in}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _micro_parts AS
        WITH parts AS (
            SELECT rnid, fid, {src} AS source_file,
                   UNNEST(ST_Dump(geom)).geom AS geom
            FROM _micro_all
        )
        SELECT row_number() OVER () AS pid, *, {is_micro_sql("geom")} AS micro,
               {bbox_columns_sql("geom")}
        FROM parts
    """)
    count = conn.execute("SELECT count(*) FROM _micro_parts WHERE micro").fetchone()[0]
    _near_part_pairs(
        conn,
        "_micro_parts",
        "_micro_parts",
        "_micro_near",
        on="l.micro AND NOT r.micro",
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _micro_dest AS
        WITH m AS (SELECT * FROM _micro_parts WHERE micro),
        n AS (SELECT * FROM _micro_parts WHERE NOT micro),
        pairs AS (
            SELECT m.pid, n.rnid AS dest_rnid, n.fid AS dest_fid,
                   ST_Area(ST_Intersection(ST_Buffer(m.geom, {tol}), n.geom)) AS w
            FROM _micro_near p JOIN m USING (pid) JOIN n ON n.pid = p.near_pid
        )
        SELECT m.pid, m.rnid, m.fid, m.source_file, m.geom, p.dest_rnid, p.dest_fid
        FROM m LEFT JOIN (
            SELECT * FROM pairs WHERE w > 0
            QUALIFY row_number() OVER (PARTITION BY pid ORDER BY w DESC, dest_fid)
                = 1
        ) p USING (pid)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{issues_table}" AS
        SELECT 'micro-polygon-' || row_number() OVER (ORDER BY fid, hash(geom)) AS key,
               'micro-polygon' AS kind,
               fid AS unit_a, dest_fid AS unit_b, NULL::BIGINT AS overlay_fid,
               CASE WHEN dest_rnid IS NULL THEN 'dropped: touches no polygon'
                    ELSE 'merged into neighbouring polygon' END AS reason,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               (ST_MaximumInscribedCircle(geom)).radius * 2 * {METERS_PER_DEGREE}
                   AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::DOUBLE AS unit_a_area_change_m2,
               NULL::DOUBLE AS unit_b_area_change_m2,
               NULL::DOUBLE AS filled_area_m2, TRUE AS fixed, source_file, geom
        FROM _micro_dest ORDER BY fid, hash(geom)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        WITH touched AS (
            SELECT rnid FROM _micro_dest
            UNION SELECT dest_rnid FROM _micro_dest WHERE dest_rnid IS NOT NULL
        ),
        pieces AS (
            SELECT p.rnid, p.geom FROM _micro_parts p SEMI JOIN touched USING (rnid)
            WHERE NOT p.micro
            UNION ALL
            SELECT dest_rnid, geom FROM _micro_dest WHERE dest_rnid IS NOT NULL
        ),
        rebuilt AS (
            SELECT rnid, ST_Union_Agg(geom) AS geom FROM pieces GROUP BY rnid
        )
        SELECT t.* EXCLUDE (geom, rnid), COALESCE(r.geom, t.geom) AS geom
        FROM _micro_all t
        LEFT JOIN rebuilt r USING (rnid)
        WHERE r.rnid IS NOT NULL OR t.rnid NOT IN (SELECT rnid FROM touched)
        ORDER BY t.rnid
    """)
    for tmp in ("_micro_all", "_micro_parts", "_micro_near", "_micro_dest"):
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    if count:
        logger.info("merged or dropped %d micro-polygon part(s) in %s", count, table_in)
    return count


def merge_detached_parts(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    *,
    pre_clip_table: str,
    overlay_source: str,
    original_table: str | None,
    issues_table: str,
) -> int:
    """Merge small clip-detached pieces the original layer shows as clip artefacts.

    Without original_table every detached piece is reported, never merged.
    """
    multi = conn.execute(
        f'SELECT count(*) FROM "{table_in}" WHERE ST_NumGeometries(geom) > 1'
    ).fetchone()[0]
    if not multi:
        conn.execute(f'CREATE OR REPLACE TABLE "{issues_table}" AS {_EMPTY_ISSUES_SQL}')
        if table_out != table_in:
            conn.execute(
                f'CREATE OR REPLACE TABLE "{table_out}" AS SELECT * FROM "{table_in}"'
            )
        return 0
    tol = SNAP_TOLERANCE
    columns = {r[0] for r in conn.execute(f'DESCRIBE "{table_in}"').fetchall()}
    src = "a.source_file" if "source_file" in columns else "NULL::VARCHAR"
    m2_per_deg2 = m2_per_deg2_factor(conn, table_in)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_all AS
        SELECT row_number() OVER () AS rnid, * FROM "{table_in}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_parts AS
        WITH parts AS (
            SELECT a.rnid, a.fid, {src} AS source_file,
                   ST_NumGeometries(a.geom) > 1 AS multi,
                   UNNEST(ST_Dump(a.geom)).geom AS geom
            FROM _det_all a
        )
        SELECT row_number() OVER () AS pid, parts.*, c.overlay_fid,
               ST_Area(parts.geom) AS area, {bbox_columns_sql("parts.geom")}
        FROM parts LEFT JOIN "{pre_clip_table}" c USING (fid)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_pre AS
        WITH q AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS geom FROM "{pre_clip_table}"
            WHERE fid IN (SELECT fid FROM _det_parts WHERE multi)
        )
        SELECT row_number() OVER () AS qid, fid, geom, ST_Area(geom) AS qarea,
               {bbox_columns_sql("geom")}
        FROM q
    """)
    # Each output piece lies inside exactly one pre-clip part, so a point test
    # finds it without building any intersection geometry.
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_pts AS
        SELECT pid, fid, pt, ST_X(pt) AS px, ST_Y(pt) AS py
        FROM (SELECT pid, fid, ST_PointOnSurface(geom) AS pt
              FROM _det_parts WHERE multi)
    """)
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_q AS
        SELECT p.pid, p.fid, COALESCE(q.qid, -p.pid) AS qid
        FROM _det_pts p LEFT JOIN _det_pre q
          ON q.fid = p.fid AND q.xmin <= p.px AND q.xmax >= p.px
         AND q.ymin <= p.py AND q.ymax >= p.py AND ST_Intersects(q.geom, p.pt)
        QUALIFY row_number() OVER (
            PARTITION BY p.pid ORDER BY q.qarea DESC NULLS LAST, q.qid
        ) = 1
    """)
    if original_table is None:
        conn.execute("""--sql
            CREATE OR REPLACE TABLE _det_group AS SELECT *, TRUE AS on_src FROM _det_q
        """)
    else:
        conn.execute("""--sql
            CREATE OR REPLACE TABLE _det_split AS
            SELECT pid, p.fid, p.geom, p.xmin, p.xmax, p.ymin, p.ymax
            FROM _det_parts p JOIN _det_q q USING (pid)
            WHERE q.qid IN (SELECT qid FROM _det_q GROUP BY qid HAVING count(*) > 1)
        """)
        _load_owned_original(conn, original_table, "_det_split")
        conn.execute("""--sql
            CREATE OR REPLACE TABLE _det_probe AS
            SELECT pid, s.fid, t.pt AS geom, t.px AS xmin, t.px AS xmax,
                   t.py AS ymin, t.py AS ymax
            FROM _det_split s JOIN _det_pts t USING (pid)
        """)
        _owned_original_parts(conn, "_det_probe", "_det_onsrc", pids_only=True)
        # An interior point can land in a hole of a piece mostly on the source.
        conn.execute("""--sql
            CREATE OR REPLACE TABLE _det_probe AS
            SELECT * FROM _det_split WHERE pid NOT IN (SELECT pid FROM _det_onsrc)
        """)
        _owned_original_parts(conn, "_det_probe", "_det_offsrc")
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE _det_group AS
            WITH shared AS (
                SELECT o.pid FROM _det_offsrc o JOIN _det_parts p USING (pid)
                GROUP BY o.pid, p.area
                HAVING sum(ST_Area(ST_CollectionExtract(
                    ST_Intersection(p.geom, o.geom), 3
                ))) >= {DETACHED_MAX_ORIGINAL_SHARE} * p.area
            )
            SELECT q.*, q.pid IN (SELECT pid FROM _det_onsrc)
                        OR q.pid IN (SELECT pid FROM shared) AS on_src
            FROM _det_q q
        """)
    # Per pre-clip part, the largest piece on the source footprint is kept, so an
    # extension-only piece never outranks a unit's real footprint.
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_rank AS
        WITH flagged AS (
            SELECT g.*, p.area,
                   row_number() OVER (
                       PARTITION BY g.fid, g.qid
                       ORDER BY g.on_src DESC, p.area DESC, g.pid
                   ) = 1 AS kept
            FROM _det_group g JOIN _det_parts p USING (pid)
        )
        SELECT pid, area, kept,
               max(area) FILTER (WHERE kept) OVER (PARTITION BY fid, qid) AS kept_area
        FROM flagged
    """)
    # A point contact measures about 2*tol of neighbour boundary; an edge far more.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_d AS
        SELECT p.*, r.area / NULLIF(r.kept_area, 0) < {DETACHED_MERGE_MAX_RATIO}
                   AS small
        FROM _det_rank r JOIN _det_parts p USING (pid)
        WHERE NOT r.kept
    """)
    # Only parts near a detached piece can be its destination; scanning every
    # part of a whole-country overlay_fid here exhausts memory at scale.
    _near_part_pairs(
        conn,
        "_det_d",
        "_det_parts",
        "_det_npair",
        on="l.overlay_fid IS NOT DISTINCT FROM r.overlay_fid",
    )
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_near AS
        SELECT n.pid, n.rnid, n.fid, n.overlay_fid, n.geom,
               n.xmin, n.xmax, n.ymin, n.ymax
        FROM _det_parts n
        WHERE n.pid NOT IN (SELECT pid FROM _det_d WHERE small IS NOT FALSE)
          AND n.pid IN (SELECT near_pid FROM _det_npair)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_sbuf AS
        SELECT pid, rnid, overlay_fid, xmin, xmax, ymin, ymax,
               ST_Buffer(geom, {tol}) AS buf
        FROM _det_d
    """)
    # Joined pairs copy the neighbour side's geometry per row, so a whole
    # high-vertex neighbour part per pair exhausts memory; split boundaries don't.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_nseg AS
        WITH b AS (
            SELECT pid, rnid, fid, overlay_fid, ST_Boundary(geom) AS bnd
            FROM _det_near
        ),
        segs AS (
            SELECT pid, rnid, fid, overlay_fid, UNNEST({subdivide_sql("bnd")}) AS seg
            FROM b
        )
        SELECT *, {bbox_columns_sql("seg")} FROM segs
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_best AS
        WITH pairs AS (
            SELECT s.pid, n.pid AS dest_pid, n.rnid AS dest_rnid, n.fid AS dest_fid,
                   sum(ST_Length(ST_Intersection(n.seg, s.buf))) AS contact
            FROM _det_sbuf s JOIN _det_nseg n
              ON n.rnid <> s.rnid
             AND n.overlay_fid IS NOT DISTINCT FROM s.overlay_fid
             AND n.xmin <= s.xmax + {tol} AND n.xmax >= s.xmin - {tol}
             AND n.ymin <= s.ymax + {tol} AND n.ymax >= s.ymin - {tol}
            GROUP BY s.pid, n.pid, n.rnid, n.fid
        )
        SELECT * FROM pairs WHERE contact > {10 * tol}
        QUALIFY row_number() OVER (PARTITION BY pid ORDER BY contact DESC, dest_fid)
            = 1
    """)
    # Grouped per destination so each neighbour geometry is read once, not per piece.
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_attached AS
        WITH c AS (
            SELECT b.dest_pid, list(d.pid ORDER BY d.pid) AS pids,
                   list(d.geom ORDER BY d.pid) AS pieces
            FROM _det_d d JOIN _det_best b USING (pid)
            WHERE d.small
            GROUP BY b.dest_pid
        ),
        u AS (
            SELECT UNNEST(c.pids) AS pid,
                   UNNEST(list_transform(
                       c.pieces, lambda x: ST_NumGeometries(ST_Union(x, n.geom)) = 1
                   )) AS attached
            FROM c JOIN _det_near n ON n.pid = c.dest_pid
        )
        SELECT pid FROM u WHERE attached
    """)
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_dest AS
        SELECT d.pid, d.rnid, d.fid, d.overlay_fid, d.source_file, d.area, d.geom,
               b.dest_rnid, b.dest_fid,
               CASE WHEN b.dest_pid IS NULL THEN 'isolated'
                    WHEN d.small IS NOT TRUE THEN 'too-large'
                    WHEN d.pid IN (SELECT pid FROM _det_attached) THEN 'candidate'
                    ELSE 'unattached' END AS outcome
        FROM _det_d d LEFT JOIN _det_best b USING (pid)
    """)
    if original_table is None:
        conn.execute("""--sql
            UPDATE _det_dest SET outcome = 'no-original' WHERE outcome = 'candidate'
        """)
    else:
        _classify_detached_candidates(conn, overlay_source)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{issues_table}" AS
        SELECT 'detached-part-' || fid || '-'
                   || row_number() OVER (PARTITION BY fid ORDER BY pid) AS key,
               'detached-part' AS kind,
               fid AS unit_a, dest_fid AS unit_b, overlay_fid::BIGINT AS overlay_fid,
               CASE outcome WHEN 'merged' THEN 'merged into neighbouring polygon'
                            WHEN 'too-large' THEN 'kept: too large to merge'
                            WHEN 'no-original' THEN 'kept: no original layer'
                            WHEN 'lobe' THEN 'kept: matches original shape'
                            ELSE 'kept: merge did not attach' END AS reason,
               area * {m2_per_deg2} AS area_m2,
               (ST_MaximumInscribedCircle(geom)).radius * 2 * {METERS_PER_DEGREE}
                   AS max_width_m,
               4 * pi() * ST_Area(geom) / POWER(ST_Perimeter(geom), 2)
                   AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::DOUBLE AS unit_a_area_change_m2,
               NULL::DOUBLE AS unit_b_area_change_m2,
               NULL::DOUBLE AS filled_area_m2, outcome = 'merged' AS fixed,
               source_file, geom
        FROM _det_dest WHERE outcome <> 'isolated' ORDER BY pid
    """)
    count = conn.execute(
        "SELECT count(*) FROM _det_dest WHERE outcome = 'merged'"
    ).fetchone()[0]
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        WITH moved AS (SELECT * FROM _det_dest WHERE outcome = 'merged'),
        touched AS (SELECT rnid FROM moved UNION SELECT dest_rnid FROM moved),
        pieces AS (
            SELECT p.rnid, p.geom FROM _det_parts p SEMI JOIN touched USING (rnid)
            WHERE p.pid NOT IN (SELECT pid FROM moved)
            UNION ALL
            SELECT dest_rnid, geom FROM moved
        ),
        rebuilt AS (
            SELECT rnid, ST_Multi(ST_Union_Agg(geom)) AS geom
            FROM pieces GROUP BY rnid
        )
        SELECT t.* EXCLUDE (rnid) REPLACE (COALESCE(r.geom, t.geom) AS geom)
        FROM _det_all t LEFT JOIN rebuilt r USING (rnid)
        ORDER BY t.rnid
    """)
    for tmp in (
        "_det_all",
        "_det_parts",
        "_det_pre",
        "_det_pts",
        "_det_q",
        "_det_probe",
        "_det_onsrc",
        "_det_offsrc",
        "_det_split",
        "_det_oparts",
        "_det_group",
        "_det_rank",
        "_det_d",
        "_det_npair",
        "_det_near",
        "_det_sbuf",
        "_det_nseg",
        "_det_best",
        "_det_attached",
        "_det_dest",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    if count:
        logger.info("merged %d clip-detached piece(s) in %s", count, table_in)
    return count


def _load_owned_original(
    conn: DuckDBPyConnection, original_table: str, probe_table: str
) -> None:
    """Write _det_oparts: raw original parts near a probe, with their owning fid.

    Ownership is by interior point, so no key column is shared with the input.
    """
    tol = SNAP_TOLERANCE
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_obox AS
        SELECT {bbox_columns_sql("geom")} FROM "{original_table}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_ohit AS
        SELECT DISTINCT o.xmin, o.xmax, o.ymin, o.ymax
        FROM _det_obox o JOIN "{probe_table}" p
          ON p.xmin - {tol} <= o.xmax AND p.xmax + {tol} >= o.xmin
         AND p.ymin - {tol} <= o.ymax AND p.ymax + {tol} >= o.ymin
    """)
    # Re-reading by exact bbox streams the original and needs no stable row order.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_ofeat AS
        WITH r AS (SELECT geom, {bbox_columns_sql("geom")} FROM "{original_table}")
        SELECT row_number() OVER () AS oid, r.geom
        FROM r SEMI JOIN _det_ohit h USING (xmin, xmax, ymin, ymax)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_oparts AS
        WITH owner AS (
            SELECT f.oid, q.fid FROM (
                SELECT oid, pos, ST_X(pos) AS px, ST_Y(pos) AS py
                FROM (SELECT oid, ST_PointOnSurface(geom) AS pos FROM _det_ofeat)
            ) f JOIN _det_pre q
              ON q.xmin <= f.px AND q.xmax >= f.px
             AND q.ymin <= f.py AND q.ymax >= f.py AND ST_Intersects(q.geom, f.pos)
            QUALIFY row_number() OVER (PARTITION BY f.oid ORDER BY q.qid) = 1
        ),
        parts AS (
            SELECT o.fid, UNNEST(ST_Dump(f.geom)).geom AS geom
            FROM _det_ofeat f JOIN owner o USING (oid)
        )
        SELECT *, {bbox_columns_sql("geom")} FROM parts
    """)
    for tmp in ("_det_obox", "_det_ohit", "_det_ofeat"):
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")


def _owned_original_parts(
    conn: DuckDBPyConnection,
    probe_table: str,
    out_table: str,
    *,
    pids_only: bool = False,
) -> None:
    """Write (pid, geom) for each owned original part touching a probe, validated."""
    select = "DISTINCT p.pid" if pids_only else "p.pid, ST_MakeValid(pp.geom) AS geom"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{out_table}" AS
        SELECT {select}
        FROM "{probe_table}" p JOIN _det_oparts pp
          ON pp.fid = p.fid AND p.xmin <= pp.xmax AND p.xmax >= pp.xmin
         AND p.ymin <= pp.ymax AND p.ymax >= pp.ymin
        WHERE ST_Intersects(pp.geom, p.geom)
    """)


def _classify_detached_candidates(
    conn: DuckDBPyConnection, overlay_source: str
) -> None:
    """Mark each merge candidate in _det_dest as 'merged' or 'lobe'."""
    tol = SNAP_TOLERANCE
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_probe AS
        SELECT pid, fid, geom, {bbox_columns_sql("geom")}
        FROM (SELECT pid, fid, ST_Buffer(geom, {tol}) AS geom FROM _det_dest
              WHERE outcome = 'candidate')
    """)
    _owned_original_parts(conn, "_det_probe", "_det_src")
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_su AS
        WITH u AS (
            SELECT s.pid, d.overlay_fid, ST_Union_Agg(s.geom) AS geom
            FROM _det_src s JOIN _det_dest d USING (pid)
            GROUP BY s.pid, d.overlay_fid
        )
        SELECT *, {bbox_columns_sql("geom")} FROM u
    """)
    # Split once so each piece joins only the overlay pieces near it, never the
    # whole (possibly multi-million-vertex) overlay polygon per piece.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_ovtile AS
        WITH t AS (
            SELECT ov.fid AS overlay_fid, UNNEST({subdivide_sql("ov.geom")}) AS geom
            FROM {overlay_source} ov
            WHERE ov.fid IN (SELECT overlay_fid FROM _det_su)
        )
        SELECT *, {bbox_columns_sql("geom")} FROM t
    """)
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _det_ovnear AS
        SELECT u.pid, ST_Union_Agg(t.geom) AS geom
        FROM _det_su u JOIN _det_ovtile t
          ON t.overlay_fid = u.overlay_fid
         AND t.xmin <= u.xmax AND t.xmax >= u.xmin
         AND t.ymin <= u.ymax AND t.ymax >= u.ymin
        GROUP BY u.pid
    """)
    # Original land the overlay clipped away beside a piece (the "neck") is
    # large when the clip sliced across the unit, near zero for a drawn lobe.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _det_rule AS
        WITH c AS (
            SELECT pid, fid, overlay_fid, area, geom FROM _det_dest
            WHERE outcome = 'candidate'
        ),
        su AS (SELECT pid, geom FROM _det_su),
        outside AS (
            SELECT c.pid, UNNEST(ST_Dump(
                CASE WHEN oc.geom IS NULL THEN su.geom
                     ELSE ST_Difference(su.geom, ST_CollectionExtract(oc.geom, 3))
                END
            )).geom AS geom
            FROM c JOIN su USING (pid)
            SEMI JOIN (SELECT DISTINCT overlay_fid FROM _det_ovtile) ex
              ON ex.overlay_fid = c.overlay_fid
            LEFT JOIN _det_ovnear oc USING (pid)
        ),
        neck AS (
            SELECT c.pid, sum(ST_Area(o.geom)) AS neck_area
            FROM c JOIN outside o USING (pid)
            WHERE ST_Dimension(o.geom) = 2
              AND ST_Intersects(o.geom, ST_Buffer(c.geom, {tol}))
            GROUP BY c.pid
        )
        SELECT c.pid,
               COALESCE(ST_Area(ST_CollectionExtract(
                   ST_Intersection(c.geom, su.geom), 3
               )), 0) / c.area AS share,
               COALESCE(n.neck_area, 0) / c.area AS neck
        FROM c LEFT JOIN su USING (pid) LEFT JOIN neck n USING (pid)
    """)
    conn.execute(f"""--sql
        UPDATE _det_dest d SET outcome = CASE
            WHEN r.share < {DETACHED_MAX_ORIGINAL_SHARE}
              OR r.neck >= {DETACHED_MIN_NECK_RATIO} THEN 'merged'
            ELSE 'lobe' END
        FROM _det_rule r WHERE r.pid = d.pid
    """)
    for tmp in (
        "_det_rule",
        "_det_probe",
        "_det_src",
        "_det_su",
        "_det_ovtile",
        "_det_ovnear",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")


def micro_issues_sql(
    conn: DuckDBPyConnection, table: str, *, source_file_expr: str = "source_file"
) -> str | None:
    """Return a SELECT over `{table}_micro`'s merge rows, or None if absent."""
    exists = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [f"{table}_micro"]
    ).fetchone()[0]
    if not exists:
        return None
    return f'SELECT * REPLACE ({source_file_expr} AS source_file) FROM "{table}_micro"'


def has_valid_topology(
    conn: DuckDBPyConnection, table: str, *, gap_maximum_width: float = SNAP_TOLERANCE
) -> bool:
    """Return True if `table.geom` has no overlap, edge mismatch, gap or micro part."""
    return (
        not has_invalid_edges(conn, table)
        and not has_gaps(
            conn, table, gap_maximum_width=gap_maximum_width, coverage=True
        )
        and not has_micro_polygons(conn, table)
    )


def check_valid_topology(
    conn: DuckDBPyConnection, table: str, *, gap_maximum_width: float = SNAP_TOLERANCE
) -> None:
    """Raise RuntimeError on any overlap, edge mismatch, gap or micro-polygon."""
    check_invalid_edges(conn, table)
    check_gaps(conn, table, gap_maximum_width=gap_maximum_width, coverage=True)
    check_micro_polygons(conn, table)


def coverage_clean(  # noqa: PLR0913 (each param is a distinct required input, not decomposable)
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    *,
    fids: list[int] | None,
    gap_maximum_width: float | None = SNAP_TOLERANCE,
    snapping_distance: float | None = SNAP_TOLERANCE,
    micro_issues_table: str | None = None,
) -> None:
    """Write table_out from table_in with ST_CoverageClean applied to a subset (or all).

    Micro-polygons are merged first: ST_CoverageClean returns an all-micro row
    EMPTY. Rows map back via a synthetic rnid, since fid may repeat across sources.
    """
    issues = micro_issues_table or "_clean_micro"
    source = "_clean_in" if has_micro_polygons(conn, table_in) else table_in
    if source == table_in:
        conn.execute(f'CREATE OR REPLACE TABLE "{issues}" AS {_EMPTY_ISSUES_SQL}')
    else:
        merge_micro_polygons(conn, table_in, source, issues_table=issues)
    where = "" if fids is None else f"WHERE fid IN ({','.join(str(f) for f in fids)})"
    snap_arg = -1 if snapping_distance is None else snapping_distance
    gap_arg = -1 if gap_maximum_width is None else gap_maximum_width
    cc = f"ST_CoverageClean(list(geom ORDER BY rn), {snap_arg}, {gap_arg})"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _clean_all AS
        -- ST_CoverageClean's result depends on input order, so fix it by content.
        SELECT row_number() OVER (ORDER BY hash(s.geom), hash(s)) AS rnid, *
        FROM "{source}" AS s
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        WITH sel AS (
            SELECT rnid, geom, row_number() OVER (ORDER BY rnid) AS rn
            FROM _clean_all {where}
        ),
        coll AS (
            SELECT {cc} AS g FROM sel
        ),
        dumped AS (
            SELECT (d).path[1] AS rn, (d).geom AS sub
            FROM (SELECT UNNEST(ST_Dump(g)) AS d FROM coll)
        ),
        grouped AS (
            SELECT rn, list(sub) AS subs FROM dumped GROUP BY rn
        ),
        parts AS (
            SELECT rn,
                   CASE WHEN len(subs) = 1 THEN subs[1] ELSE ST_Collect(subs) END
                       AS cleaned_geom
            FROM grouped
        ),
        mapping AS (
            SELECT sel.rnid, parts.cleaned_geom
            FROM sel JOIN parts USING (rn)
        )
        SELECT t.* EXCLUDE (geom, rnid),
               COALESCE(m.cleaned_geom, t.geom) AS geom
        FROM _clean_all t
        LEFT JOIN mapping m USING (rnid)
    """)
    # Snapping can narrow a part just over the micro width to under it.
    if has_micro_polygons(conn, table_out):
        merge_micro_polygons(conn, table_out, table_out, issues_table="_clean_micro2")
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{issues}" AS
            SELECT * REPLACE (
                'micro-polygon-' || row_number() OVER (ORDER BY unit_a, hash(geom))
                    AS key
            )
            FROM (
                SELECT * FROM "{issues}" UNION ALL BY NAME SELECT * FROM _clean_micro2
            )
        """)
    for tmp in ("_clean_all", "_clean_in", "_clean_micro", "_clean_micro2"):
        if tmp != micro_issues_table:
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")


def coverage_clean_escalating(  # noqa: PLR0913 (mirrors coverage_clean's own inputs)
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    *,
    fids: list[int] | None,
    gap_maximum_width: float | None = SNAP_TOLERANCE,
    micro_issues_table: str | None = None,
) -> None:
    """Coverage-clean, widening snapping_distance only as far as needed."""
    snap = SNAP_TOLERANCE
    for step in range(SNAP_ESCALATION_MAX_STEPS + 1):
        coverage_clean(
            conn,
            table_in,
            table_out,
            fids=fids,
            gap_maximum_width=gap_maximum_width,
            snapping_distance=snap,
            micro_issues_table=micro_issues_table,
        )
        if not has_invalid_edges(conn, table_out):
            if step:
                logger.info(
                    "coverage-clean: resolved invalid edges by escalating "
                    "snapping_distance to %s (step %d) on %s",
                    snap,
                    step,
                    table_out,
                )
            return
        snap += SNAP_ESCALATION_STEP


# Two edges this close count as touching, not as a notch.
_NOTCH_TOUCH = 1e-9
_NOTCH_TMP = (
    "_notch_all",
    "_notch_ext",
    "_notch_near",
    "_notch_ends",
    "_notch_moves",
    "_notch_runs",
    "_notch_fseg",
)
_NOTCH_FIX_TMP = (
    "_notch_flags",
    "_notch_pairs",
    "_notch_w",
    "_notch_one",
    "_notch_holes",
    *(
        f"_notch_{s}_{t}"
        for s in ("a", "b")
        for t in ("parts", "rings", "v", "seg", "near", "moved", "out")
    ),
)


def _build_notch_runs(conn: DuckDBPyConnection, table: str) -> None:
    """Build `_notch_runs(ua, ub, blob, score)` over `table`'s rowids."""
    s = NOTCH_SPACING
    r = s / 8
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_all AS
        SELECT rowid AS rnid, fid, geom FROM "{table}"
    """)
    # Unshared segments only: a segment whose endpoints another unit also has is shared.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_ext AS
        WITH rings AS (
            SELECT rnid, UNNEST(ST_Dump(ST_Boundary(geom))).geom AS g FROM _notch_all
        ),
        pts AS (
            SELECT rnid,
                   list_transform(
                       ST_Dump(ST_Points(g)), lambda x: [ST_X(x.geom), ST_Y(x.geom)]
                   ) AS xy
            FROM rings
        ),
        segs AS (
            SELECT rnid, e, least(e[1], e[2]) AS k1, greatest(e[1], e[2]) AS k2
            FROM (
                SELECT rnid,
                       UNNEST(list_transform(
                           range(1, len(xy)), lambda i: [xy[i], xy[i + 1]]
                       )) AS e
                FROM pts
            )
        ),
        shared AS (
            SELECT k1, k2 FROM segs GROUP BY k1, k2 HAVING count(DISTINCT rnid) > 1
        ),
        ext AS (
            SELECT row_number() OVER () AS sid, rnid,
                   ST_MakeLine(ST_Point(e[1][1], e[1][2]), ST_Point(e[2][1], e[2][2]))
                       AS geom
            FROM segs ANTI JOIN shared USING (k1, k2)
        )
        SELECT *, {bbox_columns_sql("geom")} FROM ext
    """)
    # Per segment, its length within r of the other unit minus the length touching it.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_near AS
        WITH pairs AS (
            SELECT a.sid, a.rnid AS own, b.rnid AS other,
                   any_value(a.geom) AS ag, ST_Union_Agg(b.geom) AS og,
                   list(b.sid) AS osids
            FROM _notch_ext a JOIN _notch_ext b
              ON a.rnid <> b.rnid
             AND b.xmin <= a.xmax + {r} AND b.xmax >= a.xmin - {r}
             AND b.ymin <= a.ymax + {r} AND b.ymax >= a.ymin - {r}
             AND ST_DWithin(a.geom, b.geom, {r})
            GROUP BY a.sid, a.rnid, b.rnid
        ),
        pieces AS (
            SELECT *, ST_Intersection(ag, ST_Buffer(og, {r}, 2)) AS piece FROM pairs
        )
        SELECT sid, own, other, osids,
               least(own, other) AS ua, greatest(own, other) AS ub, piece,
               ST_Length(piece)
                 - ST_Length(ST_Intersection(ag, ST_Buffer(og, {_NOTCH_TOUCH}, 2)))
                   AS len
        FROM pieces
    """)
    # close_notches moves an endpoint touching no third unit onto the other unit
    # when it lies within NOTCH_MAX_GAP_RATIO of its longest flagged segment.
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _notch_ends AS
        SELECT n.sid, n.own, n.other, ST_Length(e.geom) AS seglen,
               UNNEST([ST_StartPoint(e.geom), ST_EndPoint(e.geom)]) AS p
        FROM _notch_near n JOIN _notch_ext e USING (sid)
        WHERE n.len > 1e-12
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_moves AS
        WITH v AS (
            SELECT own, other, x, y, any_value(p) AS p,
                   max(seglen) * {NOTCH_MAX_GAP_RATIO} AS lim
            FROM (SELECT *, ST_X(p) AS x, ST_Y(p) AS y FROM _notch_ends)
            GROUP BY own, other, x, y
        ),
        vd AS (
            SELECT v.own, v.other, v.x, v.y,
                   coalesce(min(ST_Distance(v.p, s.geom))
                                FILTER (WHERE s.rnid = v.other), 'inf'::DOUBLE)
                       BETWEEN {_NOTCH_TOUCH} AND v.lim
                   AND NOT coalesce(bool_or(s.rnid <> v.other
                                       AND ST_DWithin(v.p, s.geom, {_NOTCH_TOUCH})),
                               FALSE) AS moves
            FROM v JOIN _notch_ext s
              ON s.rnid <> v.own
             AND s.xmin <= v.x + v.lim AND s.xmax >= v.x - v.lim
             AND s.ymin <= v.y + v.lim AND s.ymax >= v.y - v.lim
            GROUP BY v.own, v.other, v.x, v.y, v.lim
        )
        SELECT e.sid, e.other, bool_or(coalesce(vd.moves, FALSE)) AS moves
        FROM _notch_ends e LEFT JOIN vd
          ON vd.own = e.own AND vd.other = e.other
         AND vd.x = ST_X(e.p) AND vd.y = ST_Y(e.p)
        GROUP BY e.sid, e.other
    """)
    # A run is a notch only if close_notches moves an end of one of its segments.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_runs AS
        WITH k AS (SELECT sid, other FROM _notch_moves WHERE moves),
        moved AS (
            SELECT n.sid, n.other FROM _notch_near n SEMI JOIN k USING (sid, other)
            UNION
            SELECT n.sid, n.other
            FROM (SELECT sid, own, other, UNNEST(osids) AS osid FROM _notch_near) n
            JOIN k ON k.sid = n.osid AND k.other = n.own
        ),
        h AS (
            SELECT n.*, m.sid IS NOT NULL AS moved FROM _notch_near n
            LEFT JOIN moved m USING (sid, other)
            WHERE len > 1e-12
        ),
        b AS (
            SELECT row_number() OVER () AS bid, ua, ub, blob FROM (
                SELECT ua, ub,
                       UNNEST(ST_Dump(
                           ST_Union_Agg(ST_Buffer(piece, {s * 1.5}, 2))
                       )).geom AS blob
                FROM h GROUP BY ua, ub
            )
        )
        SELECT b.ua, b.ub, any_value(b.blob) AS blob, sum(h.len) / {s} AS score
        FROM h JOIN b ON h.ua = b.ua AND h.ub = b.ub AND ST_Intersects(h.piece, b.blob)
        GROUP BY b.bid, b.ua, b.ub
        HAVING sum(h.len) / {s} >= {NOTCH_MIN_SCORE} AND bool_or(h.moved)
    """)


def detect_notches(conn: DuckDBPyConnection, table: str, out_table: str) -> int:
    """Write `out_table(n, unit_a, unit_b, score, geom)` per notch; return the count."""
    _build_notch_runs(conn, table)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{out_table}" AS
        SELECT row_number() OVER (ORDER BY a.fid, b.fid, hash(r.blob)) AS n,
               a.fid AS unit_a, b.fid AS unit_b, r.score, r.blob AS geom
        FROM _notch_runs r
        JOIN _notch_all a ON a.rnid = r.ua JOIN _notch_all b ON b.rnid = r.ub
    """)
    for tmp in _NOTCH_TMP:
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    return conn.execute(f'SELECT count(*) FROM "{out_table}"').fetchone()[0]


def _project_notch_side_sql(  # noqa: PLR0913 (each param is a distinct required input)
    side: str, src: str, line: str, *, lim: float, pair: tuple[int, int], own: int
) -> list[str]:
    """Rebuild window part `src`, moving its flagged endpoints exactly onto `line`."""
    p = f"_notch_{side}"
    ua, ub = pair
    return [
        f"""CREATE OR REPLACE TABLE {p}_parts AS SELECT row_number() OVER () AS pid, g
            FROM (SELECT UNNEST(ST_Dump({src})).geom AS g FROM _notch_w)""",
        f"""CREATE OR REPLACE TABLE {p}_rings AS
            SELECT pid, 0 AS rn, ST_ExteriorRing(g) AS r FROM {p}_parts
            UNION ALL
            SELECT pid, i, ST_InteriorRingN(g, i::INTEGER) FROM {p}_parts,
                   generate_series(1, ST_NumInteriorRings(g)) t(i)""",
        # Closing vertex dropped; each ring is re-closed from its moved first vertex.
        f"""CREATE OR REPLACE TABLE {p}_v AS
            SELECT row_number() OVER () AS vid, pid, rn, d.path[1] AS k, d.geom AS g
            FROM (SELECT pid, rn, ST_NPoints(r) AS np,
                         UNNEST(ST_Dump(ST_Points(r))) AS d
                  FROM {p}_rings)
            WHERE d.path[1] < np""",
        f"""CREATE OR REPLACE TABLE {p}_seg AS
            SELECT ST_MakeLine(xs[i], xs[i + 1]) AS s
            FROM (SELECT list_transform(ST_Dump(ST_Points(l)), lambda x: x.geom) AS xs
                  FROM (SELECT UNNEST(ST_Dump({line})).geom AS l FROM _notch_w)),
                 generate_series(1, len(xs) - 1) t(i)""",
        f"""CREATE OR REPLACE TABLE {p}_near AS
            SELECT v.vid, ST_ClosestPoint(s.s, v.g) AS cp, ST_Distance(v.g, s.s) AS dd
            FROM {p}_v v JOIN {p}_seg s ON ST_DWithin(v.g, s.s, {lim})
            QUALIFY row_number() OVER (
                PARTITION BY v.vid ORDER BY ST_Distance(v.g, s.s)
            ) = 1""",
        f"""CREATE OR REPLACE TABLE {p}_moved AS
            SELECT v.pid, v.rn, v.k,
                   CASE WHEN ST_Distance(v.g, ST_Boundary(w.win)) <= {_NOTCH_TOUCH}
                          OR ST_DWithin(v.g, w.others, {_NOTCH_TOUCH})
                        THEN v.g
                        WHEN n.dd > {_NOTCH_TOUCH}
                         AND n.dd <= f.seglen * {NOTCH_MAX_GAP_RATIO}
                        THEN n.cp ELSE v.g END AS g
            FROM {p}_v v
            LEFT JOIN {p}_near n USING (vid)
            LEFT JOIN _notch_flags f
              ON f.ua = {ua} AND f.ub = {ub} AND f.own = {own}
             AND f.x = ST_X(v.g) AND f.y = ST_Y(v.g),
            _notch_w w""",
        f"""CREATE OR REPLACE TABLE {p}_out AS
            SELECT ST_MakeValid(ST_Union_Agg(ST_MakePolygon(shell, holes))) AS g FROM (
                SELECT pid, any_value(line) FILTER (WHERE rn = 0) AS shell,
                       COALESCE(list(line ORDER BY rn) FILTER (WHERE rn > 0), [])
                           AS holes
                FROM (
                    SELECT pid, rn, ST_MakeLine(list_append(l, l[1])) AS line
                    FROM (SELECT pid, rn, list(g ORDER BY k) AS l
                          FROM {p}_moved GROUP BY pid, rn)
                )
                GROUP BY pid
            )""",
    ]


def _fill_enclosed_notch_gaps(conn: DuckDBPyConnection, ua: int, ub: int) -> None:
    """Merge holes the fix enclosed, ua and ub's neighbours included, into ua or ub."""
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_holes AS
        WITH nbr AS (
            SELECT coalesce(ST_Union_Agg(x.geom), 'POLYGON EMPTY'::GEOMETRY) AS g
            FROM _notch_all x, _notch_w w
            WHERE x.rnid NOT IN ({ua}, {ub}) AND ST_Intersects(x.geom, w.win)
        ),
        u AS (
            SELECT FALSE AS before, ST_Union(ST_Union(na, nb), nbr.g) AS g
            FROM _notch_one, nbr
            UNION ALL
            SELECT TRUE, ST_Union(ST_Union(a.geom, b.geom), nbr.g)
            FROM _notch_all a, _notch_all b, nbr WHERE a.rnid = {ua} AND b.rnid = {ub}
        ),
        parts AS (SELECT before, UNNEST(ST_Dump(g)).geom AS p FROM u),
        holes AS (
            SELECT before, UNNEST(ST_Dump(
                ST_Difference(ST_MakePolygon(ST_ExteriorRing(p)), p)
            )).geom AS h
            FROM parts
        )
        SELECT h FROM holes n
        WHERE NOT n.before AND NOT ST_IsEmpty(n.h)
          AND NOT EXISTS (
              SELECT 1 FROM holes o
              WHERE o.before AND ST_Intersects(o.h, ST_PointOnSurface(n.h))
          )
    """)
    conn.execute(f"""--sql
        DELETE FROM _notch_holes n WHERE EXISTS (
            SELECT 1 FROM _notch_all x
            WHERE x.rnid NOT IN ({ua}, {ub}) AND ST_Intersects(x.geom, n.h)
              AND ST_Area(ST_Intersection(x.geom, n.h)) > 0
        )
    """)
    conn.execute("""--sql
        UPDATE _notch_one o SET
            na = ST_CollectionExtract(ST_MakeValid(ST_Union(o.na, f.ga)), 3),
            nb = ST_CollectionExtract(ST_MakeValid(ST_Union(o.nb, f.gb)), 3)
        FROM (
            SELECT
                coalesce(ST_Union_Agg(h) FILTER (WHERE to_a), 'POLYGON EMPTY') AS ga,
                coalesce(ST_Union_Agg(h) FILTER (WHERE NOT to_a), 'POLYGON EMPTY') AS gb
            FROM (
                SELECT h,
                       ST_Length(ST_Intersection(ST_Boundary(h), ST_Boundary(na)))
                       >= ST_Length(ST_Intersection(ST_Boundary(h), ST_Boundary(nb)))
                       AS to_a
                FROM _notch_holes, _notch_one
            )
        ) f
    """)


def close_notches(conn: DuckDBPyConnection, table: str) -> int:
    """Close `table`'s notches in place; return the number of unit pairs touched.

    Endpoints of flagged segments move exactly onto the other unit, inside a
    window per pair; callers run coverage_clean() after for any leftover mismatch.
    """
    _build_notch_runs(conn, table)
    pairs = conn.execute("SELECT DISTINCT ua, ub FROM _notch_runs").fetchall()
    if not pairs:
        for tmp in _NOTCH_TMP:
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        return 0
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _notch_fseg AS
        SELECT DISTINCT n.ua, n.ub, n.own, e.sid, e.geom, ST_Length(e.geom) AS seglen
        FROM _notch_near n
        JOIN _notch_ext e USING (sid)
        JOIN _notch_runs r
          ON r.ua = n.ua AND r.ub = n.ub AND ST_Intersects(n.piece, r.blob)
        WHERE n.len > 1e-12
    """)
    conn.execute("""--sql
        CREATE OR REPLACE TABLE _notch_flags AS
        SELECT ua, ub, own, ST_X(p) AS x, ST_Y(p) AS y, max(seglen) AS seglen
        FROM (
            SELECT ua, ub, own, seglen, ST_StartPoint(geom) AS p FROM _notch_fseg
            UNION ALL
            SELECT ua, ub, own, seglen, ST_EndPoint(geom) FROM _notch_fseg
        )
        GROUP BY ALL
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE _notch_pairs AS
        SELECT ua, ub,
               ST_Expand(
                   ST_Extent(ST_Collect(list(g))), {NOTCH_WINDOW_MARGIN}
               )::GEOMETRY AS win,
               max(seglen) * {NOTCH_MAX_GAP_RATIO} AS lim
        FROM (
            SELECT ua, ub, blob AS g, 0 AS seglen FROM _notch_runs
            UNION ALL
            SELECT ua, ub, geom, seglen FROM _notch_fseg
        )
        GROUP BY ua, ub
    """)
    for ua, ub in pairs:
        lim = conn.execute(
            "SELECT lim FROM _notch_pairs WHERE ua = ? AND ub = ?", [ua, ub]
        ).fetchone()[0]
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE _notch_w AS
            SELECT p.win, ST_Intersection(ST_Boundary(b.geom), p.win) AS tb,
                   NULL::GEOMETRY AS ta,
                   ST_Intersection(a.geom, p.win) AS ai,
                   ST_Difference(a.geom, p.win) AS ao,
                   ST_Intersection(b.geom, p.win) AS bi,
                   ST_Difference(b.geom, p.win) AS bo,
                   (SELECT ST_Union_Agg(ST_Boundary(x.geom)) FROM _notch_all x
                    WHERE x.rnid NOT IN ({ua}, {ub}) AND ST_Intersects(x.geom, p.win))
                       AS others
            FROM _notch_pairs p
            JOIN _notch_all a ON a.rnid = p.ua JOIN _notch_all b ON b.rnid = p.ub
            WHERE p.ua = {ua} AND p.ub = {ub}
        """)
        for q in _project_notch_side_sql(
            "a", "ai", "tb", lim=lim, pair=(ua, ub), own=ua
        ):
            conn.execute(q)
        conn.execute("""--sql
            UPDATE _notch_w
            SET ta = ST_Intersection(ST_Boundary((SELECT g FROM _notch_a_out)), win)
        """)
        for q in _project_notch_side_sql(
            "b", "bi", "ta", lim=lim, pair=(ua, ub), own=ub
        ):
            conn.execute(q)
        # Each side's vertices go into the other's segments, so closed edges are shared.
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE _notch_one AS
            WITH n AS (
                SELECT ST_MakeValid(ST_Snap(b.g, a.g, {_NOTCH_TOUCH})) AS nb, a.g AS fa
                FROM _notch_a_out a, _notch_b_out b
            )
            SELECT ST_CollectionExtract(ST_MakeValid(ST_Union(
                       w.ao, ST_MakeValid(ST_Snap(n.fa, n.nb, {_NOTCH_TOUCH}))
                   )), 3) AS na,
                   ST_CollectionExtract(ST_MakeValid(ST_Union(w.bo, n.nb)), 3) AS nb
            FROM _notch_w w, n
        """)
        _fill_enclosed_notch_gaps(conn, ua, ub)
        for rnid, col in ((ua, "na"), (ub, "nb")):
            conn.execute(f"""--sql
                UPDATE _notch_all SET geom = (SELECT {col} FROM _notch_one)
                WHERE rnid = {rnid}
            """)
    conn.execute(f"""--sql
        UPDATE "{table}" t SET geom = a.geom FROM _notch_all a
        WHERE t.rowid = a.rnid
          AND a.rnid IN (SELECT ua FROM _notch_pairs UNION SELECT ub FROM _notch_pairs)
    """)
    for tmp in _NOTCH_TMP + _NOTCH_FIX_TMP:
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    logger.info("closed notches between %d unit pair(s) in %s", len(pairs), table)
    return len(pairs)
