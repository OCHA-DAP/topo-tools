"""Validates the cleaned output and exports the cleaned dataset + issues report."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import GEOMETRY_TYPE
from topo_tools.core.coverage import (
    check_invalid_edges,
    detect_notches,
    gap_issues_sql,
)
from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.io import export_geometry_table, export_issues_table
from topo_tools.core.units import m2_per_deg2_factor

logger = getLogger(__name__)


def _add_outcome_columns(conn: DuckDBPyConnection, name: str) -> None:
    """Extend `{name}_02` with what actually happened to each issue during the fix.

    `fixed` is TRUE for every overlap row; a gap or notch row is fixed when no
    defect of its kind left in `{name}_03` overlaps it. A new gap gets a row.
    """
    m2_per_deg2 = m2_per_deg2_factor(conn, f"{name}_01")
    remaining = f"{name}_04_tmp1"
    conn.execute(
        f'CREATE OR REPLACE TABLE "{remaining}" '
        f"(unit_a BIGINT, unit_b BIGINT, geom {GEOMETRY_TYPE})"
    )
    if conn.execute(
        f"""SELECT count(*) FROM "{name}_02" WHERE kind = 'notch'"""
    ).fetchone()[0]:
        detect_notches(conn, f"{name}_03", remaining)
    open_gaps = f"{name}_04_tmp2"
    conn.execute(
        f'CREATE OR REPLACE TABLE "{open_gaps}" AS '
        f"SELECT * FROM ({gap_issues_sql(conn, f'{name}_03', coverage=True)})"
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        WITH before AS (SELECT fid, ST_Area(geom) AS area FROM "{name}_01"),
             after AS (SELECT fid, ST_Area(geom) AS area FROM "{name}_03"),
             fixed_bbox AS (
                 SELECT geom, {bbox_columns_sql("geom")} FROM "{name}_03"
             ),
             gap_bbox AS (
                 SELECT key, geom, {bbox_columns_sql("geom")}
                 FROM "{name}_02" WHERE kind = 'gap'
             ),
             gap_union AS (
                 SELECT g.key, ST_Union_Agg(f.geom) AS geom
                 FROM gap_bbox g
                 JOIN fixed_bbox f
                   ON f.xmax >= g.xmin AND f.xmin <= g.xmax
                  AND f.ymax >= g.ymin AND f.ymin <= g.ymax
                 GROUP BY g.key
             )
        SELECT
            i.key, i.kind, i.area_m2, i.max_width_m, i.thinness_ratio,
            i.near_length_m,
            i.unit_a, i.unit_b,
            (after_a.area - before_a.area) * {m2_per_deg2} AS unit_a_area_change_m2,
            (after_b.area - before_b.area) * {m2_per_deg2} AS unit_b_area_change_m2,
            CASE WHEN i.kind = 'gap'
                 THEN ST_Area(ST_Intersection(i.geom, gu.geom)) * {m2_per_deg2}
            END AS filled_area_m2,
            CASE WHEN i.kind = 'gap'
                 THEN NOT EXISTS (
                     SELECT 1 FROM "{open_gaps}" r
                     WHERE ST_Intersects(r.geom, i.geom)
                       AND ST_Area(ST_Intersection(r.geom, i.geom)) > 0
                 )
                 WHEN i.kind = 'notch'
                 THEN NOT EXISTS (
                     SELECT 1 FROM "{remaining}" r
                     WHERE least(r.unit_a, r.unit_b) = least(i.unit_a, i.unit_b)
                       AND greatest(r.unit_a, r.unit_b) = greatest(i.unit_a, i.unit_b)
                       AND ST_Intersects(r.geom, i.geom)
                 )
                 ELSE TRUE
            END AS fixed,
            NULL::BIGINT AS overlay_fid, NULL::VARCHAR AS reason,
            NULL::VARCHAR AS source_file,
            i.geom
        FROM "{name}_02" i
        LEFT JOIN before before_a ON before_a.fid = i.unit_a
        LEFT JOIN after after_a ON after_a.fid = i.unit_a
        LEFT JOIN before before_b ON before_b.fid = i.unit_b
        LEFT JOIN after after_b ON after_b.fid = i.unit_b
        LEFT JOIN gap_union gu ON gu.key = i.key
        WHERE i.kind != 'micro-polygon'
        UNION ALL BY NAME
        SELECT * FROM "{name}_03_micro"
        UNION ALL BY NAME
        SELECT * REPLACE (
            'gap-new-' || row_number() OVER (ORDER BY hash(geom)) AS key,
            'gap opened by the fix' AS reason
        )
        FROM "{open_gaps}" r
        WHERE NOT EXISTS (
            SELECT 1 FROM "{name}_02" i
            WHERE i.kind = 'gap' AND ST_Intersects(r.geom, i.geom)
              AND ST_Area(ST_Intersection(r.geom, i.geom)) > 0
        )
    """)
    conn.execute(f'DROP TABLE IF EXISTS "{remaining}"')
    conn.execute(f'DROP TABLE IF EXISTS "{open_gaps}"')


def _warn_on_unfilled_gaps(conn: DuckDBPyConnection, name: str) -> None:
    """Log (never raise) the count of gaps `{name}_02.fixed` marks as still unfilled."""
    row = conn.execute(f"""--sql
        SELECT COUNT(*) FILTER (WHERE NOT fixed), COUNT(*)
        FROM "{name}_02" WHERE kind = 'gap'
    """).fetchall()[0]
    remaining, total = row
    if remaining:
        logger.warning(
            "clean: %d of %d detected gap(s) remain unfilled, see the issues file",
            remaining,
            total,
        )


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    debug: bool = False,
) -> None:
    """Validate `{name}_03` and export the cleaned dataset + issues report."""
    check_invalid_edges(conn, f"{name}_03")
    _add_outcome_columns(conn, name)
    _warn_on_unfilled_gaps(conn, name)

    export_geometry_table(conn, f"{name}_03", dest)
    export_issues_table(conn, f"{name}_02", issues_dest)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_micro"')
