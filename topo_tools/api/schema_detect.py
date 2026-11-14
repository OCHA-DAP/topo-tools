"""Public API: check one admin layer's column schema and hierarchy nesting."""

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
from topo_tools.core.schema_detect import _01_inputs as inputs
from topo_tools.core.schema_detect import _02_levels as levels
from topo_tools.core.schema_detect import _03_checks as checks
from topo_tools.core.schema_detect import _04_outputs as outputs

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "levels", "checks", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "levels": ["{n}_02"],
    "checks": ["{n}_03"],
    "outputs": [],
}


def detect(  # noqa: PLR0913
    input_path: str | Path,
    issues_path: str | Path | None = None,
    *,
    name_field: str | None = None,
    code_field: str | None = None,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Check one admin layer's column schema and nesting, writing an issues report.

    Processes exactly one file per call. The report is always written, even
    with no rows; it defaults to input_path with a "_schema_issues" suffix, Parquet.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = resolve_input_path(input_path)
    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else default_output_path(input_path, "_schema_issues").with_suffix(".parquet")
    )
    if issues_path.suffix not in outputs.REPORT_SUFFIXES:
        msg = f"issues_path must end in one of {outputs.REPORT_SUFFIXES}: {issues_path}"
        raise ValueError(msg)
    check_overwrite(issues_path, overwrite=overwrite)

    # "_schema_detect" keeps every table/file this call creates distinct from
    # another tool's run against the same input_path/tmp_dir.
    name = input_basename(input_path).replace(".", "_") + "_schema_detect"

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
                inputs.main(conn, name, input_path)
            elif s == "levels":
                levels.main(conn, name, name_field=name_field, code_field=code_field)
            elif s == "checks":
                checks.main(
                    conn,
                    name,
                    name_field=name_field,
                    code_field=code_field,
                    debug=debug,
                )
            elif s == "outputs":
                outputs.main(conn, name, issues_path, debug=debug)
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
