"""Clips the whole reassembled table, one subprocess per distinct overlay fid."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.edge_clip import main as clip_main
from topo_tools.core.edge_clip import prepare_points
from topo_tools.core.edge_match._constants import PASSTHROUGH_OVERLAY_FID


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    tmp_dir: Path,
    *,
    threads: int | None = None,
    debug: bool = False,
    passthrough: bool = False,
) -> None:
    """Clip every real group's rows to its own overlay_fid's geometry.

    With passthrough=True, the orphan group's rows (PASSTHROUGH_OVERLAY_FID)
    are split out first and unioned back in afterward, unclipped. The shared
    points are kept in `{name}_03_pt_*` for the gap fill.
    """
    clip_in = f"{name}_03a"
    if passthrough:
        clip_in = f"{name}_03a_real"
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{clip_in}" AS
            SELECT * FROM "{name}_03a" WHERE overlay_fid != {PASSTHROUGH_OVERLAY_FID}
        """)

    prepare_points(conn, clip_in, f'"{name}_overlay_01"', f"{name}_03_pt")
    clip_main(
        conn,
        clip_in,
        f'"{name}_overlay_01"',
        f"{name}_04",
        tmp_dir,
        threads=threads,
        debug=debug,
        original_table=f"{name}_input_01",
        points=f"{name}_03_pt",
    )

    if passthrough:
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_04" AS
            SELECT * FROM "{name}_04"
            UNION ALL BY NAME
            SELECT * EXCLUDE (overlay_fid) FROM "{name}_03a"
            WHERE overlay_fid = {PASSTHROUGH_OVERLAY_FID}
        """)
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{clip_in}"')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')
