"""Validates topology and exports output files from the dissolved geometry table."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import check_valid_topology, gap_issues_sql
from topo_tools.core.io import export_geometry_table, export_issues_table

logger = getLogger(__name__)


def _build_issues(conn: DuckDBPyConnection, name: str) -> None:
    """Build `{name}_03`: gaps wider than the noise floor in the dissolved output."""
    table = f"{name}_02"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        {gap_issues_sql(conn, table, coverage=True)}
    """)


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    issues_dest: Path,
    *,
    debug: bool = False,
) -> None:
    """Output the dissolved layer + issues report to dest/issues_dest."""
    check_valid_topology(conn, f"{name}_02")

    _build_issues(conn, name)

    remaining = conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{name}_03" WHERE kind = 'gap'
    """).fetchall()[0][0]
    if remaining:
        logger.warning(
            "dissolve: %d gap(s) wider than the noise floor remain in the output "
            "(may be a legitimate unfilled gap, not a defect), see the issues file",
            remaining,
        )

    export_geometry_table(conn, f"{name}_02", dest)
    export_issues_table(conn, f"{name}_03", issues_dest)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03"')
