"""Clips each assigned input polygon to its overlay polygon, one subprocess per fid."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.clip import build_tiles
from topo_tools.core.clip import main as clip_main


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    tmp_dir: Path,
    *,
    threads: int | None = None,
    debug: bool = False,
    carry_columns: list[str] | None = None,
    input_columns: list[str] | None = None,
    passthrough: bool = False,
    result_table: str | None = None,
    raise_if_empty: bool = True,
    original_table: str | None = None,
    tiles: str | None = None,
) -> None:
    """Clip each assigned input polygon to its overlay, isolated per overlay fid.

    Without `tiles`, `{name}_02_tiles` is built from this call's input and dropped.
    """
    result_table = result_table or f"{name}_03"
    carry_sql = "".join(f', a."{c}" AS "{c}"' for c in (carry_columns or []))
    input_select_sql = (
        ", ".join(f'c."{c}"' for c in input_columns)
        if input_columns is not None
        else "c.*"
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_clip_in" AS
        SELECT {input_select_sql}, a.overlay_fid{carry_sql}
        FROM "{name}_input_01" c
        JOIN "{name}_02_assign" a ON a.input_fid = c.fid
    """)
    own_tiles = tiles is None
    if own_tiles:
        tiles = f"{name}_02_tiles"
        conn.execute(f'DROP TABLE IF EXISTS "{tiles}"')
        build_tiles(
            conn,
            f'"{name}_overlay_01"',
            f'SELECT overlay_fid FROM "{name}_02_clip_in"',
            tiles,
        )
    clip_main(
        conn,
        f"{name}_02_clip_in",
        f'"{name}_overlay_01"',
        result_table,
        tmp_dir,
        threads=threads,
        debug=debug,
        original_table=original_table,
        tiles=tiles,
    )
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_clip_in"')
        if own_tiles:
            conn.execute(f'DROP TABLE IF EXISTS "{tiles}"')

    if passthrough:
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{result_table}" AS
            SELECT * FROM "{result_table}"
            UNION ALL BY NAME
            SELECT {input_select_sql} FROM "{name}_input_01" c
            WHERE c.fid IN (SELECT input_fid FROM "{name}_02_unassigned")
        """)

    if raise_if_empty:
        count = conn.execute(f'SELECT COUNT(*) FROM "{result_table}"').fetchone()[0]
        if count == 0:
            msg = f"mosaic: no input polygon got an overlay for {name}"
            raise RuntimeError(msg)
