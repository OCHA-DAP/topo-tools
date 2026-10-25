"""Validates topology and exports output files from the stitched geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import (
    check_valid_topology,
    gap_issues_sql,
    micro_issues_sql,
)
from topo_tools.core.io import export_geometry_table, export_issues_table

logger = getLogger(__name__)


def _build_issues(conn: DuckDBPyConnection, name: str) -> None:
    """Build `{name}_03`: non-noise gaps left after coverage-clean, plus micro rows."""
    table = f"{name}_02"
    parts = [gap_issues_sql(conn, table, coverage=True)]
    if micro := micro_issues_sql(conn, table):
        parts.append(micro)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        {" UNION ALL BY NAME ".join(parts)}
    """)


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    debug: bool = False,
) -> None:
    """Output the stitched layer + issues report to dest/issues_dest."""
    check_valid_topology(conn, f"{name}_02")

    _build_issues(conn, name)

    remaining = conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{name}_03" WHERE kind = 'gap'
    """).fetchall()[0][0]
    if remaining:
        logger.warning(
            "stitch: %d gap(s) wider than the noise floor remain in the output "
            "(may be a legitimate unfilled gap, not a defect), see the issues file",
            remaining,
        )

    columns = {row[0] for row in conn.execute(f'DESCRIBE "{name}_02"').fetchall()}
    if "source_file" in columns:
        conn.execute(f"""--sql
            CREATE OR REPLACE TEMP VIEW "{name}_02_export" AS
            SELECT * EXCLUDE (source_file) FROM "{name}_02"
        """)
        export_geometry_table(conn, f"{name}_02_export", dest)
    else:
        export_geometry_table(conn, f"{name}_02", dest)
    export_issues_table(conn, f"{name}_03", issues_dest)

    if not debug:
        conn.execute(f'DROP VIEW IF EXISTS "{name}_02_export"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_micro"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03"')
