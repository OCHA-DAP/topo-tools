"""Validates topology and exports the mosaicked output."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import (
    assign_issue_rows_sql,
    check_valid_topology,
    gap_issues_sql,
    micro_issues_sql,
    short_source_file_sql,
)
from topo_tools.core.io import export_geometry_table, export_issues_table

logger = getLogger(__name__)

_ISSUE_COLUMNS = """
    NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
    NULL::DOUBLE AS thinness_ratio, NULL::DOUBLE AS near_length_m,
    NULL::BIGINT AS unit_b,
    NULL::DOUBLE AS unit_a_area_change_m2, NULL::DOUBLE AS unit_b_area_change_m2,
    NULL::DOUBLE AS filled_area_m2, FALSE AS fixed
"""


def _build_issues(
    conn: DuckDBPyConnection,
    name: str,
    *,
    code_join: bool = False,
    passthrough: bool = False,
    fill_gaps: bool = False,
) -> None:
    """Build `{name}_05`: every unassigned input polygon, gap-fill rows, and gaps."""
    table = f"{name}_04"
    short_source_file = short_source_file_sql("source_file")
    parts = [
        f"""
        SELECT 'clip-empty-' || fid AS key, 'clip-empty' AS kind,
               fid AS unit_a, overlay_fid,
               'clip intersection with its overlay polygon was empty' AS reason,
               {_ISSUE_COLUMNS}, {short_source_file} AS source_file, geom
        FROM "{name}_03_dropped"
        """,
        gap_issues_sql(
            conn,
            table,
            within=f"""SELECT geom FROM "{name}_overlay_01" WHERE fid IN (
                SELECT overlay_fid FROM "{name}_02_assign")""",
            coverage=True,
        ),
        f"""SELECT * REPLACE ({short_source_file} AS source_file)
        FROM "{name}_03_detached"
        """,
    ]
    if passthrough:
        parts.append(f"""
        SELECT 'passthrough-' || fid AS key, 'passthrough' AS kind,
               fid AS unit_a, NULL::BIGINT AS overlay_fid,
               NULL::VARCHAR AS reason, {_ISSUE_COLUMNS},
               {short_source_file} AS source_file, geom
        FROM "{name}_input_01"
        WHERE fid IN (SELECT input_fid FROM "{name}_02_unassigned")
        """)
    else:
        parts.append(f"""
        SELECT 'unassigned-' || input_fid AS key, 'unassigned' AS kind,
               input_fid AS unit_a, NULL::BIGINT AS overlay_fid,
               NULL::VARCHAR AS reason, {_ISSUE_COLUMNS},
               {short_source_file} AS source_file, geom
        FROM "{name}_02_unassigned"
        """)
    if fill_gaps:
        parts.append(f"""
        SELECT 'gap-fill-' || overlay_fid AS key, 'gap-fill' AS kind,
               NULL::BIGINT AS unit_a, overlay_fid,
               'overlay polygon had no matched input polygons; '
               || 'kept unclipped in the output' AS reason,
               {_ISSUE_COLUMNS}, NULL::VARCHAR AS source_file, geom
        FROM "{table}"
        WHERE overlay_fid IS NOT NULL
        """)
    if micro := micro_issues_sql(conn, table, source_file_expr=short_source_file):
        parts.append(micro)
    if code_join:
        parts.append(
            assign_issue_rows_sql(
                name, source_file_expr=short_source_file_sql("c.source_file")
            )
        )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_05" AS
        {" UNION ALL BY NAME ".join(parts)}
    """)


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    code_join: bool = False,
    passthrough: bool = False,
    fill_gaps: bool = False,
    debug: bool = False,
) -> None:
    """Output the mosaicked layer + issues report to dest/issues_dest."""
    check_valid_topology(conn, f"{name}_04")

    _build_issues(
        conn, name, code_join=code_join, passthrough=passthrough, fill_gaps=fill_gaps
    )

    remaining = conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{name}_05" WHERE kind = 'gap'
    """).fetchall()[0][0]
    if remaining:
        logger.warning(
            "mosaic: %d gap(s) wider than the noise floor remain inside the overlay "
            "polygons the output was clipped to, see the issues file",
            remaining,
        )

    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP VIEW "{name}_04_export" AS
        SELECT * EXCLUDE (source_file) FROM "{name}_04"
    """)
    export_geometry_table(conn, f"{name}_04_export", dest)
    export_issues_table(conn, f"{name}_05", issues_dest)

    if not debug:
        conn.execute(f'DROP VIEW IF EXISTS "{name}_04_export"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_input_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_overlay_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_pairs"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_assign"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_unassigned"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_dropped"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_detached"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_micro"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
