"""Public API: apply a changelog-driven retention policy to an existing code."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.change._constants import TAU_MATCH_DEFAULT, TAU_SAME_DEFAULT
from topo_tools.core.code import TABLE_COPY_OPTS, CodeFormat
from topo_tools.core.code_update import _01_inputs as inputs
from topo_tools.core.code_update import _02_levels as levels_stage
from topo_tools.core.code_update import _03_dissolve as dissolve_stage
from topo_tools.core.code_update import _04_classify as classify_stage
from topo_tools.core.code_update import _05_reparent as reparent_stage
from topo_tools.core.code_update import _06_assign as assign_stage
from topo_tools.core.code_update import _07_outputs as outputs_stage
from topo_tools.core.code_update._02_levels import SideLevels
from topo_tools.core.code_update._06_assign import ChangeRow
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

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "levels", "process", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_a_01", "{n}_b_00", "{n}_b_01"],
    "levels": [],
    "process": [],
    "outputs": ["{n}_07"],
}


def _process_level(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    n: int,
    prev_level: int | None,
    *,
    side_a: SideLevels,
    side_b: SideLevels,
    fmt: CodeFormat,
    tau_match: float,
    tau_same: float,
    link_by_code: bool,
    link_by_name: bool,
    link_mode: str,
    code_column_a: str | None,
    code_column_b: str | None,
    name_column_a: str | None,
    name_column_b: str | None,
    new_code_by_fid: dict[int, dict[int, str]],
    raw_val_by_fid: dict[int, dict[int, str]],
    changelog: list[ChangeRow],
) -> None:
    """Dissolve, classify, reparent, and assign one level, then drop its geometry.

    Keeps only this level's `_dsl_{n}_b` resident afterward, as the next level's
    reparent parent; every other per-level table is dropped once consumed.
    """
    dissolve_stage.main(
        conn, name, f"{name}_a_01", f"{name}_b_01", n, side_a=side_a, side_b=side_b
    )
    raw_col_b = side_b.columns[n]
    raw_val_by_fid[n] = dict(
        conn.execute(f'SELECT fid, "{raw_col_b}" FROM "{name}_dsl_{n}_b"').fetchall()
    )
    classify_stage.main(
        conn,
        name,
        n,
        tau_match=tau_match,
        tau_same=tau_same,
        link_by_code=link_by_code,
        link_by_name=link_by_name,
        link_mode=link_mode,
        code_col_a=code_column_a or side_a.columns[n],
        code_col_b=code_column_b or side_b.columns[n],
        name_col_a=name_column_a or side_a.names[n],
        name_col_b=name_column_b or side_b.names[n],
    )
    if prev_level is not None:
        reparent_stage.main(conn, name, n, prev_level)
    assign_stage.main(
        conn,
        name,
        n,
        prev_level,
        side_a=side_a,
        side_b=side_b,
        fmt=fmt,
        new_code_by_fid=new_code_by_fid,
        changelog=changelog,
    )
    conn.execute(f'DROP TABLE IF EXISTS "{name}_dsl_{n}_a"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_chg_{n}_03a"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_chg_{n}_03b"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_chg_{n}_03c"')
    if prev_level is not None:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_reparent_{n}_02_assign"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_dsl_{prev_level}_b"')


def code_update(  # noqa: C901, PLR0913, PLR0915
    old_path: str | Path,
    new_path: str | Path,
    output_path: str | Path | None = None,
    changelog_path: str | Path | None = None,
    *,
    root_code: str | None = None,
    delimiter: str | None = None,
    min_width: int | str | None = None,
    name_field_a: str | None = None,
    code_field_a: str | None = None,
    name_field_b: str | None = None,
    code_field_b: str | None = None,
    code_column_a: str | None = None,
    code_column_b: str | None = None,
    name_column_a: str | None = None,
    name_column_b: str | None = None,
    tau_match: float = TAU_MATCH_DEFAULT,
    tau_same: float = TAU_SAME_DEFAULT,
    link_by_code: bool = False,
    link_by_name: bool = False,
    link_mode: str = "either",
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Reconcile an already-coded OLD layer against an uncoded NEW candidate.

    Omitting root_code/delimiter/min_width triggers detection off OLD's own codes.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)
    if link_mode not in ("either", "both"):
        msg = f"link_mode must be 'either' or 'both', got {link_mode!r}"
        raise ValueError(msg)

    old_path = resolve_input_path(old_path)
    new_path = resolve_input_path(new_path)
    output_path = (
        Path(output_path)
        if output_path is not None
        else default_output_path(new_path, "_coded")
    )
    check_overwrite(output_path, overwrite=overwrite)

    changelog_path = (
        Path(changelog_path)
        if changelog_path is not None
        else output_path.with_stem(output_path.stem + "_changelog").with_suffix(".csv")
    )
    if changelog_path.suffix not in TABLE_COPY_OPTS:
        msg = (
            f"changelog file must be one of {sorted(TABLE_COPY_OPTS)} (a tabular "
            f"format, the changelog has no geometry column), got "
            f"{changelog_path.suffix!r}"
        )
        raise ValueError(msg)
    check_overwrite(changelog_path, overwrite=overwrite)

    name = input_basename(old_path).replace(".", "_") + "_code_update"

    with (
        resolve_tmp_dir(tmp_dir, debug=debug) as tmp_dir_path,
        pipeline_connection(
            name, tmp_dir_path, threads=threads, debug=debug, step=step
        ) as conn,
    ):
        logger.info("starting: %s", name)
        side_a = side_b = fmt = None
        new_code_by_fid: dict[int, dict[int, str]] | None = None
        raw_val_by_fid: dict[int, dict[int, str]] = {}
        changelog: list[ChangeRow] | None = None

        def _ensure_levels() -> None:
            nonlocal side_a, side_b, fmt
            if side_a is not None:
                return
            side_a, side_b, fmt = levels_stage.main(
                conn,
                f"{name}_a_01",
                f"{name}_b_01",
                name_field_a=name_field_a,
                code_field_a=code_field_a,
                name_field_b=name_field_b,
                code_field_b=code_field_b,
                root_code=root_code,
                delimiter=delimiter,
                min_width=min_width,
            )

        def _ensure_process() -> None:
            nonlocal new_code_by_fid, changelog
            _ensure_levels()
            if new_code_by_fid is not None:
                return
            new_code_by_fid = {}
            changelog = []
            prev_level = None
            for n in sorted(side_a.columns):
                _process_level(
                    conn,
                    name,
                    n,
                    prev_level,
                    side_a=side_a,
                    side_b=side_b,
                    fmt=fmt,
                    tau_match=tau_match,
                    tau_same=tau_same,
                    link_by_code=link_by_code,
                    link_by_name=link_by_name,
                    link_mode=link_mode,
                    code_column_a=code_column_a,
                    code_column_b=code_column_b,
                    name_column_a=name_column_a,
                    name_column_b=name_column_b,
                    new_code_by_fid=new_code_by_fid,
                    raw_val_by_fid=raw_val_by_fid,
                    changelog=changelog,
                )
                prev_level = n
            conn.execute(f'DROP TABLE IF EXISTS "{name}_dsl_{prev_level}_b"')

        for s in _STEP_ORDER:
            if step and step != s:
                continue
            if debug:
                logger.info("=== %s ===", s)
            if s == "inputs":
                inputs.main(conn, name, old_path, new_path)
            elif s == "levels":
                _ensure_levels()
            elif s == "process":
                _ensure_process()
            elif s == "outputs":
                _ensure_process()
                outputs_stage.main(
                    conn,
                    name,
                    f"{name}_b_01",
                    output_path,
                    changelog_path,
                    side_a=side_a,
                    side_b=side_b,
                    new_code_by_fid=new_code_by_fid,
                    raw_val_by_fid=raw_val_by_fid,
                    changelog=changelog,
                    fmt=fmt,
                    debug=debug,
                )
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
