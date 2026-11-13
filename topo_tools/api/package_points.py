"""Public API: one label point per admin unit per detected level."""

from logging import getLogger
from pathlib import Path

from topo_tools.core.duckdb_utils import (
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
)
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    resolve_input_path,
)
from topo_tools.core.package_points import _01_inputs as inputs
from topo_tools.core.package_points import _02_points as points_stage
from topo_tools.core.package_points import _03_outputs as outputs
from topo_tools.core.schema_map._level_columns import detect_level_columns_or_single
from topo_tools.core.schema_map._levels import detect_levels
from topo_tools.core.schema_map._target_schema import resolve_explicit_target_schema

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "points", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    # "points" is absent: per-level table names are dynamic ("{n}_02_pts{level}").
    "outputs": [],
}


def package_points(  # noqa: PLR0913
    input_path: str | Path,
    output_path: str | Path | None = None,
    name_field: str | None = None,
    code_field: str | None = None,
    *,
    depth_column: str = "adm_lvl",
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
    check_topology: bool = True,
) -> None:
    """One pole-of-inaccessibility label point per admin unit, every level combined.

    With no name_field/code_field, levels are auto-detected.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = resolve_input_path(input_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else default_output_path(input_path, "_points")
    )
    check_overwrite(output_path, overwrite=overwrite)
    schema = resolve_explicit_target_schema(name_field, code_field)

    name = input_basename(input_path).replace(".", "_") + "_package_points"

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
            if debug:
                logger.info("=== %s ===", s)
            if s == "inputs":
                inputs.main(conn, name, input_path, check_topology=check_topology)
            elif s == "points":
                if schema is not None:
                    levels = detect_levels(conn, f"{name}_01", schema)
                else:
                    levels = sorted(detect_level_columns_or_single(conn, f"{name}_01"))
                points_stage.main(conn, name, levels, schema, depth_column)
            elif s == "outputs":
                outputs.main(conn, name, output_path, debug=debug)
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
