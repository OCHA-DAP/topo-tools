"""Loads and cleans the old (Version A) and new (Version B) layers."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.io import read_reproject_and_clean


def main(
    conn: DuckDBPyConnection, name: str, old_path: Path | str, new_path: Path | str
) -> None:
    """Load and coverage-clean both layers, keeping raw geometry for the outputs."""
    read_reproject_and_clean(conn, f"{name}_a", old_path, keep_raw=True)
    read_reproject_and_clean(conn, f"{name}_b", new_path, keep_raw=True)
