"""Public API: cold-start a hierarchical code on one input file, ranked per parent."""

from logging import getLogger
from pathlib import Path

from topo_tools.core.code import TABLE_COPY_OPTS, CodeFormat, resolve_code_format
from topo_tools.core.code_create import _01_inputs as inputs
from topo_tools.core.code_create import _02_levels as levels_stage
from topo_tools.core.code_create import _03_assign as assign_stage
from topo_tools.core.code_create import _04_outputs as outputs
from topo_tools.core.code_create._constants import SOURCE_CODES
from topo_tools.core.duckdb_utils import (
    maybe_export_debug_tables,
    pipeline_connection,
    quote_identifier,
    resolve_tmp_dir,
)
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    resolve_input_path,
)

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "levels", "assign", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_01"],
    "levels": [],
    "assign": [],
    "outputs": [],
}


def _resolve_format(
    root_code: str, delimiter: str, min_width: int | str, source_codes: str
) -> CodeFormat:
    if source_codes not in SOURCE_CODES:
        msg = f"source_codes must be one of {SOURCE_CODES}, got {source_codes!r}"
        raise ValueError(msg)
    return resolve_code_format(
        root_code,
        delimiter,
        min_width,
        allow_empty_delimiter=True,
    )


def _root_sql(root_code: str | None, root_code_field: str | None) -> str:
    """Return the root as SQL: a quoted literal, or the root column's values."""
    if (root_code is None) == (root_code_field is None):
        msg = "give exactly one of root_code and root_code_field"
        raise ValueError(msg)
    if root_code_field is not None:
        return f"{quote_identifier(root_code_field)}::VARCHAR"
    return "'" + str(root_code).replace("'", "''") + "'"


def code_create(  # noqa: PLR0913
    input_path: str | Path,
    output_path: str | Path | None = None,
    issues_path: str | Path | None = None,
    *,
    root_code: str | None = None,
    root_code_field: str | None = None,
    delimiter: str,
    min_width: int | str,
    source_codes: str = "replace",
    name_field: str | None = None,
    code_field: str | None = None,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Cold-start a hierarchical code on one input file, ranked per parent.

    Give root_code, or root_code_field to number each of its values' rows apart.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)

    root_sql = _root_sql(root_code, root_code_field)
    fmt = _resolve_format(
        root_code or root_code_field or "", delimiter, min_width, source_codes
    )
    input_path = resolve_input_path(input_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else default_output_path(input_path, "_coded")
    )
    check_overwrite(output_path, overwrite=overwrite)

    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else output_path.with_stem(output_path.stem + "_issues").with_suffix(".parquet")
    )
    if issues_path.suffix not in TABLE_COPY_OPTS:
        msg = (
            f"issues file must be one of {sorted(TABLE_COPY_OPTS)} (a tabular "
            f"format, the overflow report has no geometry column), got "
            f"{issues_path.suffix!r}"
        )
        raise ValueError(msg)
    check_overwrite(issues_path, overwrite=overwrite)

    name = input_basename(input_path).replace(".", "_") + "_code_create"

    with (
        resolve_tmp_dir(tmp_dir, debug=debug) as tmp_dir_path,
        pipeline_connection(
            name, tmp_dir_path, threads=threads, debug=debug, step=step
        ) as conn,
    ):
        logger.info("starting: %s", name)
        levels: dict[int, levels_stage.Level] = {}
        for s in _STEP_ORDER:
            if step and step != s:
                continue
            if debug:
                logger.info("=== %s ===", s)
            if s == "inputs":
                inputs.main(conn, name, input_path)
            elif s == "levels":
                levels = levels_stage.main(
                    conn,
                    f"{name}_01",
                    name_field,
                    code_field,
                    root_column=root_code_field,
                )
            elif s == "assign":
                levels = levels or levels_stage.main(
                    conn,
                    f"{name}_01",
                    name_field,
                    code_field,
                    root_column=root_code_field,
                )
                assign_stage.main(
                    conn,
                    f"{name}_01",
                    levels=levels,
                    fmt=fmt,
                    source_codes=source_codes,
                    root_sql=root_sql,
                    root_column=root_code_field,
                )
            elif s == "outputs":
                levels = levels or levels_stage.main(
                    conn,
                    f"{name}_01",
                    name_field,
                    code_field,
                    root_column=root_code_field,
                )
                outputs.main(
                    conn,
                    f"{name}_01",
                    output_path,
                    levels=levels,
                    fmt=fmt,
                    source_codes=source_codes,
                    root_sql=root_sql,
                    issues_dest=issues_path,
                    debug=debug,
                )
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
