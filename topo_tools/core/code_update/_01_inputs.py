"""Loads the OLD (already-coded) and NEW (uncoded candidate) flat finest inputs."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.io import read_reproject_and_clean


def main(
    conn: DuckDBPyConnection, name: str, old_path: Path | str, new_path: Path | str
) -> None:
    """Load and coverage-clean both layers, keeping NEW's raw geometry to write."""
    read_reproject_and_clean(conn, f"{name}_a", old_path)
    read_reproject_and_clean(conn, f"{name}_b", new_path, keep_raw=True)
