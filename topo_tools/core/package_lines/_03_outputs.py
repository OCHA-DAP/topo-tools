"""Exports the combined shared+exterior boundary line output."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import finalize_geometry
from topo_tools.core.io import export_geometry_table


def main(
    conn: DuckDBPyConnection, name: str, dest: Path, *, debug: bool = False
) -> None:
    """Export `{name}_02` (shared and exterior boundaries) to dest."""
    finalize_geometry(conn, f"{name}_02", validate="none")
    export_geometry_table(conn, f"{name}_02", dest, exclude_fid=False)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_dissolved"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
