"""Detects gap, overlap, micro-polygon and notch defects in one layer, no fixing."""

from collections.abc import Callable
from logging import getLogger

from duckdb import DuckDBPyConnection, OutOfMemoryException

from topo_tools.core.constants import NOTCH_SPACING
from topo_tools.core.coverage import (
    detect_notches,
    gap_geometries_sql,
    has_invalid_edges,
    is_micro_sql,
)
from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.units import METERS_PER_DEGREE, m2_per_deg2_factor

logger = getLogger(__name__)


def _detect_or_empty(
    conn: DuckDBPyConnection,
    kind: str,
    source: str,
    empty_sql: str,
    build: Callable[[DuckDBPyConnection, str], None],
) -> None:
    """Call build(conn, source); on failure, log and run empty_sql instead."""
    try:
        build(conn, source)
    except OutOfMemoryException:
        raise
    except Exception as e:  # noqa: BLE001 (GEOS topology failures surface as generic duckdb errors)
        logger.warning(
            "%s detection failed on %s (%s); reporting none", kind, source, e
        )
        conn.execute(empty_sql)


def _build_gaps(conn: DuckDBPyConnection, tmp: str, table: str) -> None:
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        SELECT row_number() OVER (ORDER BY hash(geom)) AS n, geom
        FROM {gap_geometries_sql(table)}
    """)


def _build_overlaps(conn: DuckDBPyConnection, tmp: str, table: str) -> None:
    # ST_Overlaps/ST_Contains, not ST_Intersects: skips merely-touching
    # adjacent pairs; a narrow (fid, geom) projection avoids a wide-table self-join.
    narrow = f"{table}_narrow"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{narrow}" AS
        SELECT fid, geom, {bbox_columns_sql("geom")}
        FROM "{table}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        WITH pairs AS (
            SELECT a.fid AS unit_a, b.fid AS unit_b,
                   ST_MakeValid(
                       ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 3)
                   ) AS geom
            FROM "{narrow}" a JOIN "{narrow}" b
              ON a.fid < b.fid
              AND b.xmax >= a.xmin
              AND b.xmin <= a.xmax
              AND b.ymax >= a.ymin
              AND b.ymin <= a.ymax
              AND (
                  ST_Overlaps(a.geom, b.geom)
                  OR ST_Contains(a.geom, b.geom)
                  OR ST_Contains(b.geom, a.geom)
              )
        )
        SELECT row_number() OVER (ORDER BY unit_a, unit_b) AS n, unit_a, unit_b, geom
        FROM pairs
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)
    conn.execute(f'DROP TABLE IF EXISTS "{narrow}"')


def _build_micro(conn: DuckDBPyConnection, tmp: str, table: str) -> None:
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{tmp}" AS
        SELECT row_number() OVER (ORDER BY fid, hash(geom)) AS n, fid AS unit_a, geom
        FROM (SELECT fid, UNNEST(ST_Dump(geom)).geom AS geom FROM "{table}")
        WHERE {is_micro_sql("geom")}
    """)


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    debug: bool = False,
) -> None:
    """Detect gap/overlap/micro-polygon/notch issues in `{name}_01` into `{name}_02`."""
    table = f"{name}_01"

    gaps_tmp = f"{name}_02_tmp1"
    overlaps_tmp = f"{name}_02_tmp2"
    micro_tmp = f"{name}_02_tmp3"
    notches_tmp = f"{name}_02_tmp4"

    _detect_or_empty(
        conn,
        "gap",
        table,
        f'CREATE OR REPLACE TABLE "{gaps_tmp}" AS '
        "SELECT NULL::BIGINT AS n, NULL::GEOMETRY AS geom WHERE FALSE",
        lambda c, t: _build_gaps(c, gaps_tmp, t),
    )
    empty_overlaps_sql = (
        f'CREATE OR REPLACE TABLE "{overlaps_tmp}" AS '
        "SELECT NULL::BIGINT AS n, NULL::BIGINT AS unit_a, "
        "NULL::BIGINT AS unit_b, NULL::GEOMETRY AS geom WHERE FALSE"
    )
    if has_invalid_edges(conn, table):
        _detect_or_empty(
            conn,
            "overlap",
            table,
            empty_overlaps_sql,
            lambda c, t: _build_overlaps(c, overlaps_tmp, t),
        )
    else:
        conn.execute(empty_overlaps_sql)
    _detect_or_empty(
        conn,
        "micro-polygon",
        table,
        f'CREATE OR REPLACE TABLE "{micro_tmp}" AS '
        "SELECT NULL::BIGINT AS n, NULL::BIGINT AS unit_a, "
        "NULL::GEOMETRY AS geom WHERE FALSE",
        lambda c, t: _build_micro(c, micro_tmp, t),
    )
    _detect_or_empty(
        conn,
        "notch",
        table,
        f'CREATE OR REPLACE TABLE "{notches_tmp}" AS '
        "SELECT NULL::BIGINT AS n, NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b, "
        "NULL::DOUBLE AS score, NULL::GEOMETRY AS geom WHERE FALSE",
        lambda c, t: detect_notches(c, t, notches_tmp),
    )
    # max_width_m skips the cos(lat) factor, exact N-S, approximate E-W.
    m2_per_deg2 = m2_per_deg2_factor(conn, table)
    width_m = f"(ST_MaximumInscribedCircle(geom)).radius * 2 * {METERS_PER_DEGREE}"
    # Polsby-Popper thinness ratio, computed directly in raw degree-space.
    thinness_ratio = "4 * pi() * ST_Area(geom) / POWER(ST_Perimeter(geom), 2)"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT 'gap-' || n AS key, 'gap' AS kind,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               {width_m} AS max_width_m,
               {thinness_ratio} AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b, geom
        FROM "{gaps_tmp}"
        UNION ALL
        SELECT 'overlap-' || n AS key, 'overlap' AS kind,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               {width_m} AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               unit_a, unit_b, geom
        FROM "{overlaps_tmp}"
        UNION ALL
        SELECT 'micro-polygon-' || n AS key, 'micro-polygon' AS kind,
               ST_Area(geom) * {m2_per_deg2} AS area_m2,
               {width_m} AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               unit_a, NULL::BIGINT AS unit_b, geom
        FROM "{micro_tmp}"
        UNION ALL
        SELECT 'notch-' || n AS key, 'notch' AS kind,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               score * {NOTCH_SPACING * METERS_PER_DEGREE} AS near_length_m,
               unit_a, unit_b, geom
        FROM "{notches_tmp}" t
        -- A thin gap or overlap's own tips read as a notch; report it once.
        WHERE NOT EXISTS (
            SELECT 1 FROM "{gaps_tmp}" g WHERE ST_Intersects(t.geom, g.geom)
        )
          AND NOT EXISTS (
            SELECT 1 FROM "{overlaps_tmp}" o
            WHERE least(o.unit_a, o.unit_b) = least(t.unit_a, t.unit_b)
              AND greatest(o.unit_a, o.unit_b) = greatest(t.unit_a, t.unit_b)
              AND ST_Intersects(t.geom, o.geom)
        )
    """)

    if not debug:
        for tmp in (gaps_tmp, overlaps_tmp, micro_tmp, notches_tmp):
            conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
