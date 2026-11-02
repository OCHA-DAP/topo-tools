"""Public API: compare two polygon layer versions and classify what changed."""

from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.change import _01_inputs as inputs
from topo_tools.core.change import _02_overlap as overlap
from topo_tools.core.change import _03_classify as classify
from topo_tools.core.change import _04_outputs as outputs
from topo_tools.core.change._columns import detect_unit_columns
from topo_tools.core.change._constants import (
    TABLE_COPY_OPTS,
    TAU_MATCH_DEFAULT,
    TAU_SAME_DEFAULT,
)
from topo_tools.core.constants import COPY_OPTS
from topo_tools.core.duckdb_utils import (
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
)
from topo_tools.core.io import (
    check_overwrite,
    input_basename,
    resolve_input_path,
)

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "overlap", "classify", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_a_01", "{n}_b_01"],
    "overlap": ["{n}_02"],
    "classify": ["{n}_03a", "{n}_03b", "{n}_03c"],
    "outputs": [],
}


def _resolve_columns(  # noqa: PLR0913, PLR0917
    conn: DuckDBPyConnection,
    table: str,
    side: str,
    explicit: dict[str, str | None],
    fields: tuple[str | None, str | None],
    linked: dict[str, bool],
) -> dict[str, str | None]:
    """Return {kind: column} for one side, detecting any column not given."""
    if all(explicit.values()):
        return explicit
    detected = dict(
        zip(("code", "name"), detect_unit_columns(conn, table, *fields), strict=True)
    )
    resolved = {kind: explicit[kind] or detected[kind] for kind in explicit}
    for kind, column in resolved.items():
        if column is not None:
            continue
        hint = f"pass --{kind}-column-{side} explicitly"
        if linked[kind]:
            msg = f"--link-by-{kind} needs a {kind} column on the {side} side; {hint}"
            raise ValueError(msg)
        logger.warning("no %s column detected on the %s side; %s", kind, side, hint)
    return resolved


def change(  # noqa: C901, PLR0912, PLR0913
    old_path: str | Path,
    new_path: str | Path,
    output_path: str | Path | None = None,
    overlay_path: str | Path | None = None,
    *,
    tau_match: float = TAU_MATCH_DEFAULT,
    tau_same: float = TAU_SAME_DEFAULT,
    link_by_code: bool = False,
    link_by_name: bool = False,
    link_mode: str = "either",
    code_column_a: str | None = None,
    code_column_b: str | None = None,
    name_column_a: str | None = None,
    name_column_b: str | None = None,
    name_field_a: str | None = None,
    code_field_a: str | None = None,
    name_field_b: str | None = None,
    code_field_b: str | None = None,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
) -> None:
    """Compare two polygon layer versions and classify every unit's relationship.

    Processes exactly one old file + one new file per call.
    """
    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)
    if link_mode not in ("either", "both"):
        msg = f"link_mode must be 'either' or 'both', got {link_mode!r}"
        raise ValueError(msg)

    old_path = resolve_input_path(old_path)
    new_path = resolve_input_path(new_path)
    if output_path is not None:
        output_path = Path(output_path)
    else:
        old_stem = Path(input_basename(old_path)).stem
        new_stem = Path(input_basename(new_path)).stem
        directory = old_path.parent if isinstance(old_path, Path) else Path()
        output_path = directory / f"{old_stem}_{new_stem}_changelog.csv"
    if output_path.suffix not in TABLE_COPY_OPTS:
        msg = (
            f"output file must be one of {sorted(TABLE_COPY_OPTS)} (a tabular "
            "format, the changelog has no geometry column), got "
            f"{output_path.suffix!r}"
        )
        raise ValueError(msg)
    overlay_path = (
        Path(overlay_path)
        if overlay_path is not None
        else output_path.with_stem(output_path.stem + "_overlay").with_suffix(
            Path(input_basename(old_path)).suffix
        )
    )
    if overlay_path.suffix not in COPY_OPTS:
        msg = (
            f"overlay file must be one of {sorted(COPY_OPTS)}, "
            f"got {overlay_path.suffix!r}"
        )
        raise ValueError(msg)
    check_overwrite(output_path, overwrite=overwrite)
    check_overwrite(overlay_path, overwrite=overwrite)

    # "_changelog" avoids colliding with another tool's tables/files against
    # the same old_path/tmp_dir, same reasoning as edge_match's "_edge_match".
    name = input_basename(old_path).replace(".", "_") + "_changelog"

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
                inputs.main(conn, name, old_path, new_path)
            elif s == "overlap":
                overlap.main(conn, name)
            elif s == "classify":
                linked = {"code": link_by_code, "name": link_by_name}
                a = _resolve_columns(
                    conn,
                    f"{name}_a_01",
                    "a",
                    {"code": code_column_a, "name": name_column_a},
                    (name_field_a, code_field_a),
                    linked,
                )
                b = _resolve_columns(
                    conn,
                    f"{name}_b_01",
                    "b",
                    {"code": code_column_b, "name": name_column_b},
                    (name_field_b, code_field_b),
                    linked,
                )
                classify.main(
                    conn,
                    name,
                    tau_match=tau_match,
                    tau_same=tau_same,
                    link_by_code=link_by_code,
                    link_by_name=link_by_name,
                    link_mode=link_mode,
                    code_col_a=a["code"],
                    code_col_b=b["code"],
                    name_col_a=a["name"],
                    name_col_b=b["name"],
                )
            elif s == "outputs":
                outputs.main(conn, name, output_path, overlay_path, debug=debug)
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)
