"""Imports geodata and reprojects to EPSG:4326, merging micro-polygons only."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import is_noise_column
from topo_tools.core.coverage import check_no_overlaps, merge_micro_polygons
from topo_tools.core.io import read_and_reproject


def main(
    conn: DuckDBPyConnection,
    name: str,
    path: Path | str,
    *,
    check_topology: bool = True,
) -> None:
    """Read `{name}_01` in EPSG:4326, micro-polygons merged, overlaps checked.

    Also strips ArcGIS/export-tool artifact columns, since this table doubles
    as the finest level's own output.
    """
    read_and_reproject(conn, name, path)
    table = f"{name}_01"
    columns = [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    noise = [c for c in columns if c not in {"fid", "geom"} and is_noise_column(c)]
    for c in noise:
        conn.execute(f'ALTER TABLE "{table}" DROP "{c}"')
    merge_micro_polygons(conn, table, table, issues_table=f"{name}_01_micro")
    if check_topology:
        check_no_overlaps(conn, table)
