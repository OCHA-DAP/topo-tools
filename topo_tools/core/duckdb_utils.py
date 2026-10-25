"""Shared utilities: DuckDB connection and pipeline helpers."""

import contextlib
import multiprocessing
import re
import shutil
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from logging import FileHandler, Formatter, getLogger
from pathlib import Path
from types import FrameType
from typing import NoReturn

from duckdb import DuckDBPyConnection
from duckdb import connect as duckdb_connect

from topo_tools.core.constants import PARQUET_OPTS

logger = getLogger(__name__)

_GEO_PARQUET = (
    "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2')"
)
_PARQUET = "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15)"
_MEM_Q = "SELECT COALESCE(SUM(memory_usage_bytes), 0) FROM duckdb_memory()"


class ProfiledConnection:
    """Proxy around DuckDBPyConnection; logs RSS peak and duckdb delta per execute()."""

    def __init__(self, conn: DuckDBPyConnection, *, debug: bool = False) -> None:  # noqa: D107
        self._conn = conn
        self._debug = debug
        self._process = None
        if debug:
            import psutil  # noqa: PLC0415 (only installed/needed for --debug)

            self._process = psutil.Process()

    def execute(self, query: str, parameters: list | None = None):  # noqa: ANN201
        """Log wall-clock time, RSS peak, and duckdb delta/total, then forward."""
        if not self._debug:
            return (
                self._conn.execute(query, parameters)
                if parameters is not None
                else self._conn.execute(query)
            )
        t0 = time.perf_counter()
        before_rss = self._process.memory_info().rss
        before_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        peak_rss = [before_rss]
        stop = threading.Event()

        def _poll() -> None:
            while not stop.is_set():
                with contextlib.suppress(Exception):
                    peak_rss[0] = max(peak_rss[0], self._process.memory_info().rss)
                stop.wait(0.05)

        threading.Thread(target=_poll, daemon=True).start()

        result = (
            self._conn.execute(query, parameters)
            if parameters is not None
            else self._conn.execute(query)
        )
        # Materialize before the after-memory queries, which would otherwise
        # invalidate the result cursor on the same connection.
        rows = result.fetchall()

        stop.set()
        after_rss = self._process.memory_info().rss
        peak_rss[0] = max(peak_rss[0], after_rss)
        after_ddb = self._conn.execute(_MEM_Q).fetchall()[0][0]

        elapsed = time.perf_counter() - t0
        logger.info(
            "query %.3fs | rss peak %.0f MB | duckdb %+.0f MB | %.0f MB total | %s",
            elapsed,
            peak_rss[0] / 1e6,
            (after_ddb - before_ddb) / 1e6,
            after_ddb / 1e6,
            _query_label(query),
        )
        return _EagerResult(rows)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __getattr__(self, name: str) -> object:  # noqa: D105
        return getattr(self._conn, name)


@contextlib.contextmanager
def log_file(name: str, tmp_dir: Path) -> Iterator[None]:
    """Tee root logger to tmp/{name}.log for every run."""
    tmp_dir.mkdir(exist_ok=True, parents=True)
    handler = FileHandler(tmp_dir / f"{name}.log", mode="w")
    handler.setFormatter(Formatter("%(asctime)s - %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


def get_connection(
    name: str,
    tmp_dir: Path,
    *,
    threads: int | None = None,
    debug: bool = False,
) -> ProfiledConnection:
    """Create a file-backed DuckDB connection with spatial loaded."""
    conn = duckdb_connect(str(tmp_dir / f"{name}.duckdb"))
    # LOAD alone does not autoinstall, fails on a machine with no cached
    # spatial extension; INSTALL is a cheap no-op once already cached.
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("SET enable_progress_bar = false")
    conn.execute("SET geometry_always_xy = true")
    conn.execute("SET preserve_insertion_order = false")
    if threads is not None:
        conn.execute(f"SET threads = {threads}")
    return ProfiledConnection(conn, debug=debug)


@contextlib.contextmanager
def worker_result(result_queue: "multiprocessing.Queue") -> Iterator[None]:
    """Wrap a spawned worker's body, must not let an exception escape uncaught.

    A spawned process's exception would otherwise vanish silently; puts
    None on success or an error string on any exception instead.
    """
    try:
        yield
        result_queue.put(None)
    except Exception as e:  # noqa: BLE001 (must not raise across the process boundary uncaught)
        result_queue.put(f"{type(e).__name__}: {e}")


def spawn_worker(target: Callable, args: tuple) -> tuple[int | None, str | None]:
    """Spawn `target(*args, result_queue)` isolated, wait, return (exitcode, error)."""
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    process = ctx.Process(target=target, args=(*args, result_queue))
    process.start()
    process.join()
    err = (
        result_queue.get()
        if not result_queue.empty()
        else (
            f"worker exited with no result (exitcode={process.exitcode}); "
            "could be OOM/killed, or a spawn startup failure unrelated "
            "to memory (e.g. the entry point wasn't a real file)"
        )
    )
    return process.exitcode, err


def quote_identifier(name: str) -> str:
    """Double-quote a SQL identifier, escaping any embedded double-quote."""
    return '"' + name.replace('"', '""') + '"'


def bbox_columns_sql(geom_expr: str) -> str:
    """Build a bbox column list, to precompute once rather than inline in a JOIN."""
    return (
        f"ST_XMin({geom_expr}) AS xmin, ST_XMax({geom_expr}) AS xmax, "
        f"ST_YMin({geom_expr}) AS ymin, ST_YMax({geom_expr}) AS ymax"
    )


def cleanup_tmp(name: str, tmp_dir: Path, *, parquet: bool = False) -> None:
    """Remove tmp files for a named pipeline run."""
    for p in tmp_dir.glob(f"{name}.duckdb*"):
        p.unlink(missing_ok=True)
    if parquet:
        for p in tmp_dir.glob(f"{name}*.parquet"):
            p.unlink(missing_ok=True)


def spill_table(conn: DuckDBPyConnection, spill_dir: Path, table: str, i: int) -> None:
    """Write table to spill_dir/table/{i}.parquet, keeping it out of the connection."""
    out_dir = spill_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    conn.execute(f"""--sql
        COPY (SELECT * FROM "{table}") TO '{out_dir / f"{i:06d}.parquet"}'
        {PARQUET_OPTS}
    """)


def collect_spilled(
    conn: DuckDBPyConnection, spill_dir: Path, table: str, dest_table: str
) -> None:
    """Combine every spilled file of table, in spill order, into dest_table."""
    conn.execute(f'DROP TABLE IF EXISTS "{dest_table}"')
    conn.execute(f"""--sql
        CREATE TABLE "{dest_table}" AS
        SELECT * FROM read_parquet(
            '{spill_dir / table}/*.parquet', union_by_name = true
        )
    """)


def export_debug_tables(
    conn: DuckDBPyConnection, tmp_dir: Path, only: set[str] | None = None
) -> None:
    """Export pipeline tables to Parquet files for inspection."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    for (table,) in tables:
        if only is not None and table not in only:
            continue
        out = str(tmp_dir / f"{table}.parquet")
        has_geom = conn.execute(
            "SELECT COUNT(*) > 0 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND data_type = 'GEOMETRY'"
        ).fetchall()[0][0]
        opts = _GEO_PARQUET if has_geom else _PARQUET
        conn.execute(f"COPY \"{table}\" TO '{out}' {opts}")


@contextlib.contextmanager
def resolve_tmp_dir(
    tmp_dir: str | Path | None, *, debug: bool = False
) -> Iterator[Path]:
    """Resolve tmp_dir to a Path, owning a fresh mkdtemp() (and its cleanup) if unset.

    Left in place if the run raises, for post-mortem inspection.
    """
    owns_tmp_dir = tmp_dir is None
    path = (
        Path(tmp_dir)
        if tmp_dir is not None
        else Path(tempfile.mkdtemp(prefix="topo_tools_"))
    )
    path.mkdir(exist_ok=True, parents=True)
    yield path
    if owns_tmp_dir:
        if debug:
            logger.info("tmp_dir preserved for --debug: %s", path)
        else:
            shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def pipeline_connection(
    name: str,
    tmp_dir: Path,
    *,
    threads: int | None = None,
    debug: bool = False,
    step: str | None = None,
) -> Iterator[ProfiledConnection]:
    """Set up logging, a DuckDB connection, and SIGINT handling for one pipeline run."""
    if not step:
        cleanup_tmp(name, tmp_dir, parquet=True)

    with log_file(name, tmp_dir):
        conn = get_connection(name, tmp_dir, threads=threads, debug=debug)

        def _interrupt(_sig: int, _frame: FrameType | None) -> NoReturn:
            conn.interrupt()
            raise KeyboardInterrupt

        old_handler = signal.signal(signal.SIGINT, _interrupt)
        try:
            yield conn
        finally:
            signal.signal(signal.SIGINT, old_handler)
            conn.close()
            if not step and not debug:
                cleanup_tmp(name, tmp_dir)


def maybe_export_debug_tables(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    tmp_dir: Path,
    name: str,
    step: str | None,
    step_tables: dict[str, list[str]],
    *,
    debug: bool,
) -> None:
    """Export debug tables for the run, scoped to one step's tables under --step."""
    if not debug:
        return
    only = None
    if step and step in step_tables:
        only = {t.format(n=name) for t in step_tables[step]}
    export_debug_tables(conn, tmp_dir, only=only)


class _EagerResult:
    """Materialized DuckDB result that survives subsequent execute() calls."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self._idx = 0

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self) -> tuple | None:
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None


def _query_label(query: str) -> str:
    q = " ".join(query.split())
    m = re.search(r'CREATE (?:OR REPLACE )?TABLE "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"CREATE {m.group(1)}"
    m = re.search(r'DROP TABLE (?:IF EXISTS )?"([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"DROP {m.group(1)}"
    m = re.search(r'ALTER TABLE "[^"]+" RENAME TO "([^"]+)"', q, re.IGNORECASE)
    if m:
        return f"RENAME TO {m.group(1)}"
    m = re.search(r"COPY .+? TO '([^']+)'", q, re.IGNORECASE)
    if m:
        return f"COPY {m.group(1)}"
    return q[:80]
