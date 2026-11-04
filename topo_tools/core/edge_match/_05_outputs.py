"""Validates topology, or reports invalid edges, and exports the matched output."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import (
    assign_issue_rows_sql,
    check_valid_topology,
    gap_issues_sql,
    invalid_edge_issues,
    merge_micro_polygons,
    micro_issues_sql,
    short_source_file_sql,
)
from topo_tools.core.edge_match._04_clean import coverage_rows_sql
from topo_tools.core.edge_match._constants import PASSTHROUGH_OVERLAY_FID
from topo_tools.core.io import export_geometry_table, export_issues_table

logger = getLogger(__name__)

_ISSUE_COLUMNS = """
    NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
    NULL::DOUBLE AS thinness_ratio, NULL::DOUBLE AS near_length_m,
    NULL::BIGINT AS unit_b,
    NULL::DOUBLE AS unit_a_area_change_m2, NULL::DOUBLE AS unit_b_area_change_m2,
    NULL::DOUBLE AS filled_area_m2, FALSE AS fixed
"""


def _clip_targets_sql(name: str) -> str:
    """Build SQL for the overlay polygons the output was clipped to."""
    return f"""
        SELECT geom FROM "{name}_overlay_01" WHERE fid IN (
            SELECT overlay_fid FROM "{name}_02_assign"
            WHERE overlay_fid != {PASSTHROUGH_OVERLAY_FID}
        )
    """


def _build_issues(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    *,
    code_join: bool = False,
    passthrough: bool = False,
    fill_gaps: bool = False,
    clean: bool = True,
) -> None:
    """Build `{name}_05`: unassigned/dropped input polygons, gaps or invalid edges.

    passthrough=True omits 'unassigned' (superseded by 'passthrough'/'dropped_group').
    """
    table = f"{name}_04"
    short_source_file = short_source_file_sql("source_file")
    parts = []
    if not passthrough:
        parts.append(f"""
        SELECT 'unassigned-' || input_fid AS key, 'unassigned' AS kind,
               input_fid AS unit_a, NULL::BIGINT AS overlay_fid,
               NULL::VARCHAR AS reason, {_ISSUE_COLUMNS},
               {short_source_file} AS source_file, geom
        FROM "{name}_02_unassigned"
        """)
    parts += [
        f"""
        SELECT 'dropped_group-' || input_fid AS key, 'dropped_group' AS kind,
               input_fid AS unit_a, overlay_fid, reason, {_ISSUE_COLUMNS},
               {short_source_file} AS source_file, geom
        FROM "{name}_03b"
        """,
        f"""
        SELECT 'clip-empty-' || fid AS key, 'clip-empty' AS kind,
               fid AS unit_a, overlay_fid,
               'clip intersection with its overlay polygon was empty' AS reason,
               {_ISSUE_COLUMNS}, {short_source_file} AS source_file, geom
        FROM "{name}_04_dropped"
        """,
        f"""SELECT * REPLACE ({short_source_file} AS source_file)
        FROM "{name}_04_detached"
        """,
        gap_issues_sql(
            conn, f"{table}_coverage", within=_clip_targets_sql(name), coverage=True
        )
        if clean
        else f'SELECT * FROM "{name}_04_invalid"',
    ]
    if passthrough:
        parts.append(f"""
        SELECT 'passthrough-' || input_fid AS key, 'passthrough' AS kind,
               input_fid AS unit_a, NULL::BIGINT AS overlay_fid,
               'no overlapping overlay polygon; extended alone and kept unclipped in '
               'the output' AS reason, {_ISSUE_COLUMNS},
               {short_source_file} AS source_file, geom
        FROM "{name}_02_unassigned"
        WHERE input_fid NOT IN (SELECT input_fid FROM "{name}_03b")
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
    clean: bool = True,
    debug: bool = False,
) -> None:
    """Output the matched layer + issues report to dest/issues_dest."""
    table = f"{name}_04"
    where = coverage_rows_sql(name, passthrough=passthrough)
    if clean:
        conn.execute(f"""--sql
            CREATE OR REPLACE TEMP VIEW "{table}_coverage" AS
            SELECT * FROM "{table}" WHERE {where}
        """)
        check_valid_topology(conn, f"{table}_coverage")
    else:
        logger.warning("match: output is not coverage-cleaned, seams may remain")
        merge_micro_polygons(conn, table, table, issues_table=f"{table}_micro")
        invalid_edge_issues(conn, table, f"{name}_04_invalid", where=where)

    _build_issues(
        conn,
        name,
        code_join=code_join,
        passthrough=passthrough,
        fill_gaps=fill_gaps,
        clean=clean,
    )

    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP VIEW "{name}_04_export" AS
        SELECT * EXCLUDE (source_file) FROM "{name}_04"
    """)
    export_geometry_table(conn, f"{name}_04_export", dest)
    export_issues_table(conn, f"{name}_05", issues_dest)

    if not debug:
        conn.execute(f'DROP VIEW IF EXISTS "{name}_04_export"')
        conn.execute(f'DROP VIEW IF EXISTS "{table}_coverage"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_input_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_overlay_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_pairs"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_assign"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_unassigned"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_dropped"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_detached"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_micro"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_invalid"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_05"')
