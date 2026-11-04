"""Loads the input and join layers raw, raising if either layer overlaps itself."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.assign import load_input, load_overlay
from topo_tools.core.coverage import check_no_overlaps


def main(
    conn: DuckDBPyConnection,
    name: str,
    input_path: Path | str,
    join_path: Path | str,
) -> None:
    """Load `{name}_input_01` and `{name}_overlay_01`."""
    load_input(conn, name, [input_path])
    check_no_overlaps(conn, f"{name}_input_01")
    load_overlay(conn, name, join_path, require_coverage=True)
