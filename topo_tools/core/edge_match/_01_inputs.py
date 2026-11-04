"""Coverage-cleans the input layer; loads the overlay layer raw."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.assign import load_overlay
from topo_tools.core.io import read_reproject_and_clean


def load_and_clean_input(
    conn: DuckDBPyConnection, name: str, input_path: Path | str
) -> None:
    """Load and coverage-clean one input file, tagged with its own source_file."""
    read_reproject_and_clean(conn, f"{name}_input", input_path, fix_notches=True)
    # assign_one groups input polygons by source_file (each input file is one group).
    conn.execute(f"""--sql
        ALTER TABLE "{name}_input_01"
        ADD COLUMN source_file VARCHAR DEFAULT '{input_path}'
    """)


def main(
    conn: DuckDBPyConnection,
    name: str,
    input_path: Path | str,
    overlay_path: Path | str,
) -> None:
    """Coverage-clean the input layer; load the overlay layer raw."""
    load_and_clean_input(conn, name, input_path)
    load_overlay(conn, name, overlay_path, require_coverage=True)
