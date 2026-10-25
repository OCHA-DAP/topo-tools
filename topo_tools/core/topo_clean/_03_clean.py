"""Fixes gap/overlap defects in one ST_CoverageClean call, then micro-polygons."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.coverage import (
    close_notches,
    coverage_clean,
    gap_issues_sql,
    has_gaps,
    has_invalid_edges,
    merge_micro_polygons,
)
from topo_tools.core.units import METERS_PER_DEGREE

from ._constants import (
    AREA_NOISE_FACTOR,
    AUTO_GAP_WIDTH_EPSILON_FACTOR,
    DEFAULT_THINNESS_RATIO,
    GAP_MAXIMUM_WIDTH_ALL_DEG,
    OVERLAP_LOSS_HEADROOM,
)

logger = getLogger(__name__)


def _resolve_gap_maximum_width_deg(
    conn: DuckDBPyConnection,
    name: str,
    gap_maximum_width: tuple[str, float | None],
    table: str,
) -> float | None:
    mode, value = gap_maximum_width
    if mode == "all":
        has_gap = conn.execute(f"""--sql
            SELECT EXISTS (SELECT 1 FROM "{name}_02" WHERE kind = 'gap')
        """).fetchall()[0][0]
        return GAP_MAXIMUM_WIDTH_ALL_DEG if has_gap else None
    if mode == "default":
        has_gap = conn.execute(f"""--sql
            SELECT EXISTS (SELECT 1 FROM "{name}_02" WHERE kind = 'gap')
        """).fetchall()[0][0]
        return SNAP_TOLERANCE if has_gap else None
    if mode == "thin":
        # Measured on `table`: notch closing reshapes gaps, e.g. trims a notch sliver.
        widest_m = conn.execute(f"""--sql
            SELECT MAX(max_width_m) FROM ({gap_issues_sql(conn, table)})
            WHERE thinness_ratio <= {DEFAULT_THINNESS_RATIO}
        """).fetchall()[0][0]
        if widest_m is None:
            return None
        return widest_m / METERS_PER_DEGREE * AUTO_GAP_WIDTH_EPSILON_FACTOR
    return value


def _total_area(conn: DuckDBPyConnection, table: str) -> float:
    return (
        conn.execute(f'SELECT SUM(ST_Area(geom)) FROM "{table}"').fetchall()[0][0]
        or 0.0
    )


def _overlap_area(conn: DuckDBPyConnection, name: str) -> float:
    return (
        conn.execute(f"""--sql
            SELECT SUM(ST_Area(geom)) FROM "{name}_02" WHERE kind = 'overlap'
        """).fetchall()[0][0]
        or 0.0
    )


def _defect_unrelated_fid_outcomes(
    conn: DuckDBPyConnection, name: str, table_in: str, table_out: str
) -> tuple[int, int]:
    """Return (collapsed, drifted) counts for fids untouched by any detected defect.

    Defect-adjacent fids are exempt from both checks.
    """
    return conn.execute(f"""--sql
        WITH defect_adjacent AS (
            SELECT unit_a AS fid FROM "{name}_02" WHERE kind IN ('overlap', 'notch')
            UNION
            SELECT unit_b AS fid FROM "{name}_02" WHERE kind IN ('overlap', 'notch')
            UNION
            SELECT DISTINCT i.fid
            FROM "{table_in}" i, "{name}_02" g
            WHERE g.kind = 'gap' AND ST_Intersects(i.geom, g.geom)
        )
        SELECT
            COUNT(*) FILTER (WHERE ST_IsEmpty(o.geom) AND i.area > 0),
            COUNT(*) FILTER (WHERE NOT ST_IsEmpty(o.geom) AND o.area != i.area)
        FROM (SELECT fid, ST_Area(geom) AS area FROM "{table_in}") i
        JOIN (SELECT fid, geom, ST_Area(geom) AS area FROM "{table_out}") o USING (fid)
        WHERE i.fid NOT IN (SELECT fid FROM defect_adjacent)
    """).fetchall()[0]


def _bad_geometry_type_count(conn: DuckDBPyConnection, table: str) -> int:
    """Count fids whose fixed geometry isn't Polygon/MultiPolygon."""
    return conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{table}"
        WHERE ST_GeometryType(geom) NOT IN ('POLYGON', 'MULTIPOLYGON')
    """).fetchall()[0][0]


def _fix(
    conn: DuckDBPyConnection,
    name: str,
    *,
    gap_maximum_width: tuple[str, float | None],
    snapping_distance: tuple[str, float | None],
) -> None:
    """Fix gap/overlap/notch defects in `{name}_01`, writing `{name}_03`."""
    source = f"{name}_01"
    table = source
    out_table = f"{name}_03"
    if conn.execute(
        f"""SELECT count(*) FROM "{name}_02" WHERE kind = 'notch'"""
    ).fetchone()[0]:
        table = f"{name}_03_tmp0"
        conn.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM "{source}"')
        close_notches(conn, table)

    gap_maximum_width_deg = _resolve_gap_maximum_width_deg(
        conn, name, gap_maximum_width, table
    )
    # has_invalid_edges() never detects gaps.
    if not has_invalid_edges(conn, table) and gap_maximum_width_deg is None:
        conn.execute(
            f'CREATE OR REPLACE TABLE "{out_table}" AS SELECT * FROM "{table}"'
        )
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp0"')
        return

    snap_mode, snap_value = snapping_distance
    snapping_distance_deg = SNAP_TOLERANCE if snap_mode == "default" else snap_value
    input_area = _total_area(conn, source)
    overlap_area = _overlap_area(conn, name)
    min_area = (
        input_area * (1 - AREA_NOISE_FACTOR) - overlap_area * OVERLAP_LOSS_HEADROOM
    )

    coverage_clean(
        conn,
        table,
        out_table,
        fids=None,
        gap_maximum_width=gap_maximum_width_deg,
        snapping_distance=snapping_distance_deg,
        micro_issues_table=f"{name}_03_tmp1",
    )

    output_area = _total_area(conn, out_table)
    collapsed, drifted = _defect_unrelated_fid_outcomes(conn, name, source, out_table)
    conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp0"')
    bad_types = _bad_geometry_type_count(conn, out_table)
    invalid_edges = has_invalid_edges(conn, out_table)
    narrow_gap_remains = (
        not invalid_edges
        and gap_maximum_width_deg is not None
        and has_gaps(
            conn, out_table, gap_maximum_width=gap_maximum_width_deg, coverage=True
        )
    )
    if (
        invalid_edges
        or narrow_gap_remains
        or output_area < min_area
        or collapsed
        or bad_types
    ):
        msg = (
            f"invalid, collapsed, or under-filled coverage-clean output (area "
            f"{output_area} vs input {input_area}, {collapsed} fid(s) with no "
            f"detected defect collapsed to empty, {bad_types} fid(s) with a "
            f"non-polygon geometry type, invalid edges: {invalid_edges}, narrow "
            f"gap remaining: {narrow_gap_remains}) at "
            f"gap_maximum_width={gap_maximum_width_deg} on {source}"
        )
        raise RuntimeError(msg)

    if drifted:
        logger.warning(
            "clean: %d fid(s) with no connection to any detected gap/overlap "
            "shifted area anyway (ST_CoverageClean's global renoding) on %s",
            drifted,
            source,
        )
    pct_change = (output_area - input_area) / input_area * 100 if input_area else 0.0
    logger.info(
        "clean: total area %s %.4f%% (%.6f -> %.6f) on %s",
        "gained" if pct_change >= 0 else "lost",
        abs(pct_change),
        input_area,
        output_area,
        source,
    )


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    gap_maximum_width: tuple[str, float | None],
    snapping_distance: tuple[str, float | None],
) -> None:
    """Fix `{name}_01` into `{name}_03`; micro-polygon rows go to `{name}_03_micro`."""
    _fix(
        conn,
        name,
        gap_maximum_width=gap_maximum_width,
        snapping_distance=snapping_distance,
    )
    out_table = f"{name}_03"
    merge_micro_polygons(conn, out_table, out_table, issues_table=f"{name}_03_tmp2")
    parts = [
        f'SELECT * FROM "{t}"'
        for t in (f"{name}_03_tmp1", f"{name}_03_tmp2")
        if conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [t]
        ).fetchone()[0]
    ]
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_micro" AS
        SELECT * REPLACE (
            'micro-polygon-' || row_number() OVER (ORDER BY unit_a, hash(geom)) AS key
        )
        FROM ({" UNION ALL BY NAME ".join(parts)})
    """)
    for tmp in (f"{name}_03_tmp1", f"{name}_03_tmp2"):
        conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
