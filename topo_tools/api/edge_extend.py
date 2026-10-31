"""Public API: extend polygon boundaries outward with Voronoi diagrams."""

import time
from decimal import Decimal
from logging import getLogger
from pathlib import Path

from topo_tools.core.duckdb_utils import (
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
)
from topo_tools.core.edge_extend import _01_inputs as inputs
from topo_tools.core.edge_extend import _02_lines as lines
from topo_tools.core.edge_extend import _05_merge as merge
from topo_tools.core.edge_extend import _06_outputs as outputs
from topo_tools.core.edge_extend import attempt
from topo_tools.core.edge_extend._constants import MAX_POINTS
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    resolve_input_path,
)

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "lines", "attempt", "merge", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "lines": ["{n}_02"],
    "attempt": [
        "{n}_03a",
        "{n}_03_tmp1",
        "{n}_03_tmp2",
        "{n}_03_tmp3",
        "{n}_03_tmp4",
        "{n}_03b",
        "{n}_04",
        "{n}_04_tmp1",
        "{n}_04_tmp2",
    ],
    "merge": ["{n}_05", "{n}_05_tmp1", "{n}_05_tmp2", "{n}_05_tmp3"],
    "outputs": [],
}


def extend(  # noqa: PLR0913
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
    distance: Decimal | str | None = None,
    max_points: int = MAX_POINTS,
) -> None:
    """Extend polygon boundaries outward with Voronoi diagrams to fill coverage gaps.

    Processes exactly one file per call. If output_path is omitted, it defaults
    to input_path with an "_extended" suffix in the same directory.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = resolve_input_path(input_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else default_output_path(input_path, "_extended")
    )
    check_overwrite(output_path, overwrite=overwrite)

    name = input_basename(input_path).replace(".", "_")

    with (
        resolve_tmp_dir(tmp_dir, debug=debug) as tmp_dir_path,
        pipeline_connection(
            name, tmp_dir_path, threads=threads, debug=debug, step=step
        ) as conn,
    ):
        logger.info("starting: %s", name)
        for s in _STEP_ORDER:
            if step and step != s:
                continue
            logger.info("=== %s ===", s)
            start = time.monotonic()
            if s == "inputs":
                inputs.main(conn, name, input_path)
            elif s == "lines":
                lines.main(conn, name)
            elif s == "attempt":
                attempt.main(
                    conn,
                    name,
                    distance=None if distance is None else Decimal(distance),
                    max_points=max_points,
                    debug=debug,
                )
            elif s == "merge":
                merge.main(conn, name, debug=debug)
            elif s == "outputs":
                outputs.main(conn, name, output_path, debug=debug)
            logger.info("%s done in %.0fs", s, time.monotonic() - start)
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
