"""Clips every row to its own overlay_fid's geometry, isolated per overlay polygon."""

import contextlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import GEOMETRY_TYPE, PARQUET_OPTS
from topo_tools.core.coverage import merge_detached_parts
from topo_tools.core.duckdb_utils import (
    bbox_columns_sql,
    get_connection,
    log_file,
    spawn_worker,
    worker_result,
)

from ._tiles import pair_batches

if TYPE_CHECKING:
    import multiprocessing


def main(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    table_in: str,
    overlay_source: str,
    table_out: str,
    tmp_dir: Path,
    *,
    threads: int | None = None,
    debug: bool = False,
    original_table: str | None = None,
    tiles: str,
) -> None:
    """Clip every row of table_in to its own overlay_fid's geometry.

    table_in MUST already carry a overlay_fid column, and `tiles` names the
    build_tiles table covering it. An empty-intersection input polygon is dropped
    from table_out but kept in "{table_out}_dropped". A clip-detached piece is
    merged or reported in "{table_out}_detached".
    """
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        SELECT * EXCLUDE (overlay_fid) FROM "{table_in}" WHERE FALSE
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}_dropped" AS
        SELECT * FROM "{table_in}" WHERE FALSE
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}_cover" (overlay_fid BIGINT, area DOUBLE)
    """)
    overlay_fids = [
        row[0]
        for row in conn.execute(
            f'SELECT DISTINCT overlay_fid FROM "{table_in}" ORDER BY overlay_fid'
        ).fetchall()
    ]

    for overlay_fid in overlay_fids:
        group_dir = tmp_dir / f"{table_out}_p{overlay_fid}"
        # Always clear first: a stale group_dir from an earlier input
        # file reusing this overlay_fid under --debug would collide on create.
        shutil.rmtree(group_dir, ignore_errors=True)
        group_dir.mkdir(parents=True, exist_ok=True)

        _export_group(conn, table_in, tiles, overlay_fid, group_dir)

        exitcode, err = spawn_worker(_clip_one_worker, (group_dir, threads, debug))
        output_path = group_dir / "output.parquet"
        if exitcode != 0 or err or not output_path.exists():
            msg = (
                f"clip: subprocess for overlay_fid={overlay_fid} failed "
                f"(exitcode={exitcode}, error={err}, see {group_dir} "
                "for exported inputs)"
            )
            raise RuntimeError(msg)

        conn.execute(f"""--sql
            INSERT INTO "{table_out}" BY NAME
            SELECT * FROM read_parquet('{output_path}')
        """)
        conn.execute(f"""--sql
            INSERT INTO "{table_out}_cover"
            SELECT {overlay_fid}, coalesce(sum(ST_Area(geom)), 0)
            FROM read_parquet('{output_path}')
        """)
        conn.execute(f"""--sql
            INSERT INTO "{table_out}_dropped" BY NAME
            SELECT *, {overlay_fid} AS overlay_fid
            FROM read_parquet('{group_dir / "dropped.parquet"}')
        """)

        if not debug:
            shutil.rmtree(group_dir, ignore_errors=True)

    merge_detached_parts(
        conn,
        table_out,
        table_out,
        pre_clip_table=table_in,
        overlay_source=overlay_source,
        original_table=original_table,
        issues_table=f"{table_out}_detached",
    )


def _export_group(
    conn: DuckDBPyConnection,
    table_in: str,
    tiles: str,
    overlay_fid: int,
    group_dir: Path,
) -> None:
    """Write one overlay_fid's rows and tiles."""
    conn.execute(f"""--sql
        COPY (SELECT * FROM "{table_in}" WHERE overlay_fid = {overlay_fid})
        TO '{group_dir / "input.parquet"}' {PARQUET_OPTS}
    """)
    conn.execute(f"""--sql
        COPY (SELECT * FROM "{tiles}" WHERE overlay_fid = {overlay_fid})
        TO '{group_dir / "tiles.parquet"}' {PARQUET_OPTS}
    """)


def _clip_one_worker(
    group_dir: Path,
    threads: int | None,
    debug: bool,  # noqa: FBT001
    result_queue: "multiprocessing.Queue",
) -> None:
    """Child-process entry point; must stay module-level for spawn picklability."""
    with (
        worker_result(result_queue),
        log_file("clip", group_dir) if debug else contextlib.nullcontext(),
    ):
        worker_conn = get_connection("clip", group_dir, threads=threads, debug=debug)
        x = worker_conn.execute
        x(f"""--sql
            CREATE TABLE clip_inputs AS
            SELECT *, {bbox_columns_sql("geom")}
            FROM read_parquet('{group_dir / "input.parquet"}')
        """)
        x(f"""--sql
            CREATE TABLE clip_tiles AS
            SELECT * FROM read_parquet('{group_dir / "tiles.parquet"}')
        """)
        if not x("SELECT count(*) FROM clip_tiles").fetchone()[0]:
            msg = "no overlay geometry for this overlay_fid"
            raise ValueError(msg)
        pair_batches(worker_conn, "clip_inputs", "clip_tiles", "clip_bat")
        x(f"CREATE TABLE clip_po (fid BIGINT, geom {GEOMETRY_TYPE})")
        for (b,) in x("SELECT DISTINCT b FROM clip_bat ORDER BY 1").fetchall():
            x(f"""--sql
                INSERT INTO clip_po
                SELECT p.fid, ST_CollectionExtract(
                    ST_Union_Agg(ST_Intersection(i.geom, t.geom)), 3
                )
                FROM clip_bat p JOIN clip_inputs i USING (fid)
                JOIN clip_tiles t USING (tid)
                WHERE p.b = {b}
                GROUP BY p.fid
            """)
        # A row split across batches is unioned once more; a row with no tile
        # pair keeps a NULL geom and is dropped.
        x("""--sql
            CREATE TABLE clip_result AS
            SELECT c.* EXCLUDE (geom, xmin, xmax, ymin, ymax, overlay_fid),
                   c.geom AS orig_geom,
                   ST_Multi(ST_CollectionExtract(r.geom, 3)) AS geom
            FROM clip_inputs c
            LEFT JOIN (
                SELECT fid, CASE WHEN count(*) = 1 THEN any_value(geom)
                                 ELSE ST_Union_Agg(geom) END AS geom
                FROM clip_po GROUP BY fid
            ) r USING (fid)
        """)
        x(f"""--sql
            COPY (
                SELECT * EXCLUDE (orig_geom) FROM clip_result
                WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) ORDER BY fid
            ) TO '{group_dir / "output.parquet"}' {PARQUET_OPTS}
        """)
        x(f"""--sql
            COPY (
                SELECT * EXCLUDE (geom) RENAME (orig_geom AS geom)
                FROM clip_result WHERE geom IS NULL OR ST_IsEmpty(geom)
                ORDER BY fid
            ) TO '{group_dir / "dropped.parquet"}' {PARQUET_OPTS}
        """)
        worker_conn.close()
