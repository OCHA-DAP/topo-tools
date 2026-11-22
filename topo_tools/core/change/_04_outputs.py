"""Builds the overlay render layer and exports both output files."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import TABLE_COPY_OPTS
from topo_tools.core.coverage import finalize_geometry
from topo_tools.core.io import (
    add_csv_bom,
    export_geometry_table,
    restore_raw_geometry,
)


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    overlay_dest: Path,
    *,
    debug: bool = False,
) -> None:
    """Build the overlay render layer, then export both output files.

    No topology hard-gate here (unlike extend/match/clean): change is a
    read-only comparison, not a fix, so there's nothing to validate against.
    """
    restore_raw_geometry(conn, f"{name}_a_01", f"{name}_a_00")
    restore_raw_geometry(conn, f"{name}_b_01", f"{name}_b_00")
    # Every new-version unit plus every "removed" old-version unit (no new
    # polygon stands in for it), together covering the comparison area.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT b.geom AS geom, pc.cluster_id AS cluster_id,
               pc.relationship_class AS relationship_class,
               'b' AS piece_side, NULL::BIGINT AS a_fid, b.fid AS b_fid
        FROM "{name}_b_01" b
        JOIN "{name}_03b" pc ON pc.side = 'b' AND pc.fid = b.fid

        UNION ALL

        SELECT a.geom AS geom, pc.cluster_id AS cluster_id,
               pc.relationship_class AS relationship_class,
               'a' AS piece_side, a.fid AS a_fid, NULL::BIGINT AS b_fid
        FROM "{name}_a_01" a
        JOIN "{name}_03b" pc ON pc.side = 'a' AND pc.fid = a.fid
        WHERE pc.relationship_class = 'removed'
    """)

    dest.parent.mkdir(exist_ok=True, parents=True)

    conn.execute(f"""--sql
        COPY (SELECT * FROM "{name}_03c") TO '{dest}' {TABLE_COPY_OPTS[dest.suffix]}
    """)
    add_csv_bom(dest)
    finalize_geometry(conn, f"{name}_04", validate="none")
    export_geometry_table(conn, f"{name}_04", overlay_dest, exclude_fid=False)

    if not debug:
        for t in ("a_01", "b_01", "02", "03a", "03b", "03c", "04"):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_{t}"')
