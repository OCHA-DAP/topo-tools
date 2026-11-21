"""Exports the joined layer and its issues report."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import (
    empty_issues_sql,
    finalize_geometry,
    short_source_file_sql,
)
from topo_tools.core.io import export_geometry_table, export_issues_table

_ISSUE_COLUMNS = """
    NULL::DOUBLE AS max_width_m, NULL::DOUBLE AS thinness_ratio,
    NULL::DOUBLE AS near_length_m,
    NULL::BIGINT AS unit_b, NULL::DOUBLE AS unit_a_area_change_m2,
    NULL::DOUBLE AS unit_b_area_change_m2, NULL::DOUBLE AS filled_area_m2,
    FALSE AS fixed
"""


def _build_issues(conn: DuckDBPyConnection, name: str, min_overlap: float) -> None:
    """Build `{name}_04`: no-overlap, low-overlap, and value-mismatch rows."""
    source_file = short_source_file_sql("c.source_file")
    empty = ""
    if sql := empty_issues_sql(conn, f"{name}_03", source_file_expr="NULL"):
        # A dropped row has no output row, so the reason names its input fid.
        empty = f"""UNION ALL BY NAME SELECT * EXCLUDE (overlay_fid) REPLACE (
            NULL::BIGINT AS unit_a, reason || ' (input fid ' || unit_a || ')' AS reason
        ) FROM ({sql})"""
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT 'no-overlap-' || o.out_fid AS key, 'no-overlap' AS kind,
               o.out_fid AS unit_a, NULL::BIGINT AS join_fid,
               'input polygon overlaps no join polygon; join columns left NULL'
               AS reason,
               NULL::DOUBLE AS area_m2, {_ISSUE_COLUMNS},
               {source_file} AS source_file, c.geom
        FROM "{name}_input_01" c
        JOIN "{name}_03" o ON o.fid = c.fid
        WHERE c.fid NOT IN (SELECT input_fid FROM "{name}_02_assign")
        UNION ALL BY NAME
        SELECT 'low-overlap-' || o.out_fid AS key, 'low-overlap' AS kind,
               o.out_fid AS unit_a, s.overlay_fid AS join_fid,
               printf('best join polygon covers %.2f of input polygon', s.overlap_share)
               AS reason,
               s.input_area - s.shared_area AS area_m2, {_ISSUE_COLUMNS},
               {source_file} AS source_file, c.geom
        FROM "{name}_02_share" s
        JOIN "{name}_input_01" c ON c.fid = s.input_fid
        JOIN "{name}_03" o ON o.fid = c.fid
        WHERE s.overlap_share < {min_overlap}
        UNION ALL BY NAME
        SELECT 'value-mismatch-' || o.out_fid || '-' || m.column_name AS key,
               'value-mismatch' AS kind, o.out_fid AS unit_a, m.overlay_fid AS join_fid,
               printf('%s: input ''%s'' vs join ''%s''',
                      m.column_name, m.input_value, m.join_value) AS reason,
               NULL::DOUBLE AS area_m2, {_ISSUE_COLUMNS},
               {source_file} AS source_file, c.geom
        FROM "{name}_03_mismatch" m
        JOIN "{name}_input_01" c ON c.fid = m.input_fid
        JOIN "{name}_03" o ON o.fid = c.fid
        {empty}
        ORDER BY unit_a, kind
    """)


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    min_overlap: float,
    debug: bool = False,
) -> None:
    """Export `{name}_03` to dest and its issues to issues_dest.

    No topology check: schema-join only rounds geometry to the output grid.
    """
    finalize_geometry(conn, f"{name}_03", validate="none")
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        SELECT * REPLACE (row_number() OVER (ORDER BY out_fid) AS out_fid)
        FROM "{name}_03"
    """)
    _build_issues(conn, name, min_overlap)
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP VIEW "{name}_03_export" AS
        SELECT * EXCLUDE (out_fid, source_file) FROM "{name}_03"
    """)
    export_geometry_table(conn, f"{name}_03_export", dest)
    export_issues_table(conn, f"{name}_04", issues_dest)

    if not debug:
        conn.execute(f'DROP VIEW IF EXISTS "{name}_03_export"')
        for t in (
            "input_01",
            "overlay_01",
            "02_pairs",
            "02_assign",
            "02_unassigned",
            "02_share",
            "03",
            "03_mismatch",
            "03_empty",
            "04",
        ):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_{t}"')
