"""Public API: match input polygons to overlay boundaries, then extend to fill gaps."""

import shutil
from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.api._schema_fill_compose import apply_optional_fill, validate_fill_flags
from topo_tools.core.assign import (
    assign_many,
    assign_one,
    fill_unmatched_overlays,
    input_bbox_extent,
    load_nearby_overlay,
    load_overlay,
    overlay_fid_bboxes,
    prepare_overlay_tiles,
    resolve_merge_columns,
    union_bbox,
    validate_merge_flags,
)
from topo_tools.core.duckdb_utils import (
    collect_spilled,
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
    spill_table,
)
from topo_tools.core.edge_clip import drop_points
from topo_tools.core.edge_match import _01_inputs as inputs
from topo_tools.core.edge_match import _02_groups as groups
from topo_tools.core.edge_match import _03_clip as clip
from topo_tools.core.edge_match import _04_outputs as outputs
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    resolve_input_path,
    sort_paths_by_column_count_desc,
)

logger = getLogger(__name__)

_ASSIGN_MODES = ("auto", "one", "many")

_STEP_ORDER = ["inputs", "assign", "groups", "clip", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_input_01", "{n}_overlay_01", "{n}_overlay_full"],
    "assign": ["{n}_02_pairs", "{n}_02_assign", "{n}_02_unassigned"],
    # "groups" is absent: group ids aren't known ahead of time (dynamic
    # "{n}_g{overlay_fid}" names), so it falls through to the default below.
    "clip": ["{n}_04", "{n}_04_dropped", "{n}_04_detached", "{n}_02_gap_fill"],
    "outputs": [],
}


def match(  # noqa: C901, PLR0912, PLR0913, PLR0915
    input_paths: str | Path | list[str | Path],
    overlay_path: str | Path,
    output_path: str | Path | None = None,
    issues_path: str | Path | None = None,
    *,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
    step: str | None = None,
    match_column: str | None = None,
    overlay_match_column: str | None = None,
    input_match_column: str | None = None,
    merge: bool = False,
    overlay_include: list[str] | None = None,
    overlay_exclude: list[str] | None = None,
    input_include: list[str] | None = None,
    input_exclude: list[str] | None = None,
    prefer: str | None = None,
    assign: str = "auto",
    fill_schema: bool = False,
    name_field: str | None = None,
    code_field: str | None = None,
    depth_column: str = "adm_lvl",
) -> None:
    """Match one or more input layers to their best-overlapping overlay polygon."""
    if match_column is not None and (overlay_match_column or input_match_column):
        msg = (
            "match_column is mutually exclusive with overlay polygon/input_match_column"
        )
        raise ValueError(msg)
    if bool(overlay_match_column) != bool(input_match_column):
        msg = "overlay_match_column and input_match_column must be given together"
        raise ValueError(msg)
    if match_column is not None:
        overlay_match_column = input_match_column = match_column

    if step is not None and step not in _STEP_ORDER:
        msg = f"step must be one of {_STEP_ORDER}, got {step!r}"
        raise ValueError(msg)
    validate_merge_flags(
        merge=merge,
        overlay_include=overlay_include,
        overlay_exclude=overlay_exclude,
        input_include=input_include,
        input_exclude=input_exclude,
        prefer=prefer,
    )
    validate_fill_flags(
        fill_schema=fill_schema, name_field=name_field, code_field=code_field
    )
    passthrough = merge

    if isinstance(input_paths, (str, Path)):
        paths = [resolve_input_path(input_paths)]
        single_path = paths[0]
    else:
        paths = [resolve_input_path(p) for p in input_paths]
        single_path = None

    if single_path is None and step is not None:
        msg = "step is not supported when multiple input_paths are given"
        raise ValueError(msg)
    if assign not in _ASSIGN_MODES:
        msg = f"assign must be one of {_ASSIGN_MODES}, got {assign!r}"
        raise ValueError(msg)
    if single_path is None and assign == "many":
        msg = "assign='many' is not supported when multiple input_paths are given"
        raise ValueError(msg)

    overlay_path = resolve_input_path(overlay_path)
    if output_path is not None:
        output_path = Path(output_path)
    elif single_path is not None:
        output_path = default_output_path(single_path, "_matched")
    else:
        msg = "output_path is required when multiple input_paths are given"
        raise ValueError(msg)
    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else output_path.with_stem(output_path.stem + "_issues")
    )
    check_overwrite(output_path, overwrite=overwrite)
    check_overwrite(issues_path, overwrite=overwrite)

    # "_edge_match" keeps this call's tables/files distinct from an
    # edge_extend() run sharing the same input_path/tmp_dir.
    name = (
        input_basename(single_path).replace(".", "_") + "_edge_match"
        if single_path is not None
        else output_path.name.replace(".", "_") + "_edge_match"
    )

    with (
        resolve_tmp_dir(tmp_dir, debug=debug) as tmp_dir_path,
        pipeline_connection(
            name, tmp_dir_path, threads=threads, debug=debug, step=step
        ) as conn,
    ):
        logger.info("starting: %s", name)
        if single_path is None:
            if assign == "auto":
                logger.info(
                    "match: assign one (auto): many isn't available with more "
                    "than one input file"
                )
            else:
                logger.info("match: assign one")
            _match_multi_file(
                conn,
                name,
                paths,
                overlay_path,
                output_path,
                issues_path,
                tmp_dir_path,
                threads=threads,
                debug=debug,
                overlay_match_column=overlay_match_column,
                input_match_column=input_match_column,
                merge=merge,
                overlay_include=overlay_include,
                overlay_exclude=overlay_exclude,
                input_include=input_include,
                input_exclude=input_exclude,
                prefer=prefer,
                fill_schema=fill_schema,
                name_field=name_field,
                code_field=code_field,
                depth_column=depth_column,
            )
        else:
            resolved_overlay_columns: list[str] | None = None
            resolved_input_columns: list[str] | None = None
            merge_resolved = False
            for s in _STEP_ORDER:
                if step and step != s:
                    continue
                if debug:
                    logger.info("=== %s ===", s)
                if s == "inputs":
                    inputs.main(conn, name, single_path, overlay_path)
                    if passthrough:
                        conn.execute(f"""--sql
                            CREATE OR REPLACE TABLE "{name}_overlay_full" AS
                            SELECT * FROM "{name}_overlay_01"
                        """)
                elif s == "assign":
                    if not merge_resolved:
                        resolved_overlay_columns, resolved_input_columns = (
                            resolve_merge_columns(
                                conn,
                                name,
                                merge=merge,
                                overlay_include=overlay_include,
                                overlay_exclude=overlay_exclude,
                                input_include=input_include,
                                input_exclude=input_exclude,
                                prefer=prefer,
                            )
                        )
                        merge_resolved = True
                    _assign(
                        conn,
                        name,
                        assign,
                        overlay_match_column=overlay_match_column,
                        input_match_column=input_match_column,
                        carry_columns=resolved_overlay_columns,
                        input_columns=resolved_input_columns,
                    )
                elif s == "groups":
                    if not merge_resolved:
                        resolved_overlay_columns, resolved_input_columns = (
                            resolve_merge_columns(
                                conn,
                                name,
                                merge=merge,
                                overlay_include=overlay_include,
                                overlay_exclude=overlay_exclude,
                                input_include=input_include,
                                input_exclude=input_exclude,
                                prefer=prefer,
                            )
                        )
                        merge_resolved = True
                    groups.main(
                        conn,
                        name,
                        tmp_dir_path,
                        threads=threads,
                        debug=debug,
                        carry_columns=resolved_overlay_columns,
                        input_columns=resolved_input_columns,
                        passthrough=passthrough,
                    )
                elif s == "clip":
                    if not merge_resolved:
                        resolved_overlay_columns, resolved_input_columns = (
                            resolve_merge_columns(
                                conn,
                                name,
                                merge=merge,
                                overlay_include=overlay_include,
                                overlay_exclude=overlay_exclude,
                                input_include=input_include,
                                input_exclude=input_exclude,
                                prefer=prefer,
                            )
                        )
                        merge_resolved = True
                    clip.main(
                        conn,
                        name,
                        tmp_dir_path,
                        threads=threads,
                        debug=debug,
                        passthrough=passthrough,
                    )
                    if passthrough:
                        fill_unmatched_overlays(
                            conn,
                            name,
                            carry_columns=resolved_overlay_columns,
                            result_table=f"{name}_04",
                            overlay_snapshot_table=f"{name}_overlay_full",
                            points_table=f"{name}_03_pt_pts",
                        )
                    if not debug:
                        drop_points(conn, f"{name}_03_pt")
                elif s == "outputs":
                    apply_optional_fill(
                        conn,
                        name,
                        f"{name}_04",
                        requested=fill_schema,
                        name_field=name_field,
                        code_field=code_field,
                        depth_column=depth_column,
                        debug=debug,
                    )
                    outputs.main(
                        conn,
                        name,
                        output_path,
                        issues_path,
                        code_join=bool(overlay_match_column and input_match_column),
                        passthrough=passthrough,
                        fill_gaps=passthrough,
                        debug=debug,
                    )
        maybe_export_debug_tables(
            conn, tmp_dir_path, name, step, _STEP_TABLES, debug=debug
        )
        logger.info("done: %s", name)


def _assign(conn: DuckDBPyConnection, name: str, assign: str, **kwargs: object) -> None:
    """Run assign-one or assign-many, auto picking many when under half overlap."""
    if assign == "many":
        assign_many(conn, name, **kwargs)
        logger.info("match: assign many")
        return
    if assign == "auto":
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_overlay_auto" AS
            SELECT * FROM "{name}_overlay_01"
        """)
    assign_one(conn, name, **kwargs)
    overlapping, assigned = conn.execute(f"""--sql
        SELECT COUNT(pr.input_fid), COUNT(*)
        FROM "{name}_02_assign" a
        LEFT JOIN "{name}_02_pairs" pr
          ON pr.input_fid = a.input_fid AND pr.overlay_fid = a.overlay_fid
         AND pr.shared_area > 0
    """).fetchone()
    if assign == "one":
        logger.info(
            "match: assign one: %d of %d input polygons overlap the winning "
            "overlay polygon",
            overlapping,
            assigned,
        )
        return
    if overlapping * 2 < assigned:
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_overlay_01" AS
            SELECT * FROM "{name}_02_overlay_auto"
        """)
        assign_many(conn, name, **kwargs)
        logger.warning(
            "match: assign many (auto): only %d of %d input polygons overlap "
            "the winning overlay polygon, so each is assigned on its own; "
            "pass --assign one to force a single overlay polygon",
            overlapping,
            assigned,
        )
    else:
        logger.info(
            "match: assign one (auto): %d of %d input polygons overlap the winning "
            "overlay polygon; pass --assign many to assign each on its own",
            overlapping,
            assigned,
        )
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_overlay_auto"')


def _match_multi_file(  # noqa: C901, PLR0913, PLR0917
    conn: DuckDBPyConnection,
    name: str,
    paths: list[Path],
    overlay_path: Path | str,
    output_path: Path,
    issues_path: Path,
    tmp_dir_path: Path,
    *,
    threads: int | None,
    debug: bool,
    overlay_match_column: str | None,
    input_match_column: str | None,
    merge: bool,
    overlay_include: list[str] | None,
    overlay_exclude: list[str] | None,
    input_include: list[str] | None,
    input_exclude: list[str] | None,
    prefer: str | None,
    fill_schema: bool,
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
) -> None:
    """Load/assign one input file at a time, sharing one already-loaded overlay layer.

    Groups/clip/outputs run once over the fully accumulated result
    afterward, so cross-file input polygons sharing a overlay_fid extend together.
    """
    load_overlay(conn, name, overlay_path)
    conn.execute(f"""--sql
        CREATE TABLE "{name}_overlay_full" AS SELECT * FROM "{name}_overlay_01"
    """)
    paths = sort_paths_by_column_count_desc(conn, paths)

    combined_bbox: tuple[float, float, float, float] | None = None
    resolved_overlay_columns: list[str] | None = None
    resolved_input_columns: list[str] | None = None
    for i, file_path in enumerate(paths):
        inputs.load_and_clean_input(conn, name, file_path)
        if i == 0:
            resolved_overlay_columns, resolved_input_columns = resolve_merge_columns(
                conn,
                name,
                merge=merge,
                overlay_include=overlay_include,
                overlay_exclude=overlay_exclude,
                input_include=input_include,
                input_exclude=input_exclude,
                prefer=prefer,
            )
        bbox = input_bbox_extent(conn, name)
        if bbox is not None:
            combined_bbox = (
                bbox if combined_bbox is None else union_bbox(combined_bbox, bbox)
            )
        conn.execute(f'DROP TABLE IF EXISTS "{name}_input_01"')
    prepare_overlay_tiles(conn, name, input_bbox=combined_bbox)

    passthrough = merge

    spilled = (f"{name}_input_01", f"{name}_02_assign", f"{name}_02_unassigned")
    spill_dir = tmp_dir_path / f"{name}_spill"
    shutil.rmtree(spill_dir, ignore_errors=True)

    overlay_bboxes = overlay_fid_bboxes(conn, f"{name}_overlay_full")
    fid_offset = 0
    for i, file_path in enumerate(paths):
        inputs.load_and_clean_input(conn, name, file_path)
        conn.execute(f'UPDATE "{name}_input_01" SET fid = fid + {fid_offset}')
        load_nearby_overlay(conn, name, overlay_bboxes)
        assign_one(
            conn,
            name,
            use_cached_tiles=True,
            overlay_match_column=overlay_match_column,
            input_match_column=input_match_column,
            carry_columns=resolved_overlay_columns,
            input_columns=resolved_input_columns,
        )

        for table in spilled:
            spill_table(conn, spill_dir, table, i)

        new_max = conn.execute(f'SELECT MAX(fid) FROM "{name}_input_01"').fetchone()[0]
        fid_offset = new_max if new_max is not None else fid_offset

    for table in spilled:
        collect_spilled(conn, spill_dir, table, table)
    if not debug:
        shutil.rmtree(spill_dir, ignore_errors=True)

    # groups/clip need every matched overlay fid, not just the last file's
    # narrowed set (assign_one narrows {name}_overlay_01 every iteration).
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_overlay_01" AS
        SELECT * FROM "{name}_overlay_full"
    """)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_overlay_full"')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_overlay_parts"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_overlay_tiles"')

    groups.main(
        conn,
        name,
        tmp_dir_path,
        threads=threads,
        debug=debug,
        carry_columns=resolved_overlay_columns,
        input_columns=resolved_input_columns,
        passthrough=passthrough,
    )
    clip.main(
        conn, name, tmp_dir_path, threads=threads, debug=debug, passthrough=passthrough
    )
    if passthrough:
        # _overlay_01 was restored to full above, identical to _overlay_full,
        # and clip only reads it, so it's still safe to reuse here.
        fill_unmatched_overlays(
            conn,
            name,
            carry_columns=resolved_overlay_columns,
            result_table=f"{name}_04",
            overlay_snapshot_table=f"{name}_overlay_01",
            points_table=f"{name}_03_pt_pts",
        )
    if not debug:
        drop_points(conn, f"{name}_03_pt")
    apply_optional_fill(
        conn,
        name,
        f"{name}_04",
        requested=fill_schema,
        name_field=name_field,
        code_field=code_field,
        depth_column=depth_column,
        debug=debug,
    )
    outputs.main(
        conn,
        name,
        output_path,
        issues_path,
        code_join=bool(overlay_match_column and input_match_column),
        passthrough=passthrough,
        fill_gaps=passthrough,
        debug=debug,
    )
