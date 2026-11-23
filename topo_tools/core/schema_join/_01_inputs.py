"""Loads the input and join layers raw, raising if the join layer overlaps itself."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.assign import load_input, load_overlay


def main(
    conn: DuckDBPyConnection,
    name: str,
    input_path: Path | str,
    join_path: Path | str,
) -> None:
    """Load `{name}_input_01` and `{name}_overlay_01`."""
    load_input(conn, name, [input_path])
    load_overlay(conn, name, join_path, require_coverage=True)
