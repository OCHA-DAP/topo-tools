"""Public API: fix the safe name defects of one coded admin layer, rule-based."""

from logging import getLogger
from pathlib import Path

from topo_tools.core.coverage import finalize_geometry
from topo_tools.core.duckdb_utils import (
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
)
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    export_geometry_table,
    input_basename,
    resolve_input_path,
)
from topo_tools.core.name_clean import _04_fix as fix
from topo_tools.core.name_detect import _01_inputs as inputs
from topo_tools.core.name_detect import _02_levels as levels
from topo_tools.core.name_detect import _03_checks as checks
from topo_tools.core.name_detect import _04_outputs as outputs
from topo_tools.core.name_detect._macros import create_macros

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "levels", "checks", "fix", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "levels": ["{n}_02"],
    "checks": ["{n}_03"],
    "fix": ["{n}_01"],
    "outputs": [],
}


def _paths(
    input_path: Path | str,
    output_path: str | Path | None,
    issues_path: str | Path | None,
) -> tuple[Path, Path]:
    """Return the output and issues paths, defaulted and checked."""
    output_path = (
        Path(output_path)
        if output_path is not None
        else default_output_path(input_path, "_cleaned")
    )
    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else default_output_path(input_path, "_name_issues").with_suffix(".parquet")
    )
    if issues_path.suffix not in outputs.REPORT_SUFFIXES:
        msg = f"issues_path must end in one of {outputs.REPORT_SUFFIXES}: {issues_path}"
        raise ValueError(msg)
    return output_path, issues_path


def clean(  # noqa: PLR0913
    input_path: str | Path,
    output_path: str | Path | None = None,
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
    """Fix the safe name defects of one coded admin layer, reporting every finding.

    Processes exactly one file per call. Only spacing, invisible characters,
    NFC and verified encoding repairs are applied; the report marks them fixed.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    input_path = resolve_input_path(input_path)
    output_path, issues_path = _paths(input_path, output_path, issues_path)
    check_overwrite(output_path, overwrite=overwrite)
    check_overwrite(issues_path, overwrite=overwrite)

    # "_name_clean" keeps every table/file this call creates distinct from
    # another tool's run against the same input_path/tmp_dir.
    name = input_basename(input_path).replace(".", "_") + "_name_clean"

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
                checks.main(conn, name, debug=debug)
            elif s == "fix":
                create_macros(conn)
                fix.main(conn, name)
            elif s == "outputs":
                finalize_geometry(conn, f"{name}_01", validate="none")
                export_geometry_table(conn, f"{name}_01", output_path)
                outputs.main(
                    conn, name, issues_path, fixed=fix.fixed_sql(), debug=debug
                )
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
