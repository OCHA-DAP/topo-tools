"""Exports the detected issues report."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.io import export_geometry_table


def main(
    conn: DuckDBPyConnection, name: str, dest: Path, *, debug: bool = False
) -> None:
    """Export `{name}_02` to dest.

    No topology hard-gate here: detect is read-only inspection, not a fix.
    """
    export_geometry_table(conn, f"{name}_02", dest, exclude_fid=False)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
