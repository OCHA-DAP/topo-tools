"""Public API: fit already-extended input polygons into a new overlay layer."""

import shutil
from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.api._schema_fill_compose import apply_optional_fill, validate_fill_flags
from topo_tools.core.assign import (
    assign_one,
    bboxes_overlap,
    fill_unmatched_overlays,
    input_bbox_extent,
    load_input,
    load_nearby_overlay,
    load_original,
    load_overlay,
    overlay_fid_bboxes,
    prepare_overlay_tiles,
    resolve_merge_columns,
    union_bbox,
    validate_merge_flags,
)
from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.duckdb_utils import (
    bbox_columns_sql,
    collect_spilled,
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
    spill_table,
)
from topo_tools.core.edge_clip import (
    build_tiles,
    collect_candidates,
    drop_points,
    pick_points,
)
from topo_tools.core.edge_mosaic import _01_clip as clip
from topo_tools.core.edge_mosaic import _02_outputs as outputs
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    reproject_select_sql,
    resolve_input_path,
    sort_paths_by_column_count_desc,
)

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "assign", "clip", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_input_01", "{n}_overlay_01", "{n}_overlay_full", "{n}_original_01"],
    "assign": ["{n}_02_pairs", "{n}_02_assign", "{n}_02_unassigned"],
    "clip": ["{n}_03", "{n}_03_dropped", "{n}_03_detached", "{n}_02_gap_fill"],
    "outputs": [],
}


def mosaic(  # noqa: C901, PLR0912, PLR0913, PLR0915
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
    fill_schema: bool = False,
    name_field: str | None = None,
    code_field: str | None = None,
    depth_column: str = "adm_lvl",
    original_paths: str | Path | list[str | Path] | None = None,
) -> None:
    """Fit one or more already-extended input layers into a new overlay layer."""
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
        single_path = resolve_input_path(input_paths)
    else:
        paths = [resolve_input_path(p) for p in input_paths]
        single_path = None

    if single_path is None and step is not None:
        msg = "step is not supported when multiple input_paths are given"
        raise ValueError(msg)

    overlay_path = resolve_input_path(overlay_path)
    if isinstance(original_paths, (str, Path)):
        original_paths = [original_paths]
    originals = [resolve_input_path(p) for p in original_paths or []]
    if output_path is not None:
        output_path = Path(output_path)
    elif single_path is not None:
        output_path = default_output_path(single_path, "_mosaicked")
    else:
        msg = "output_path is required when multiple input_paths are given"
        raise ValueError(msg)
    issues_path = (
        Path(issues_path)
        if issues_path is not None
        else output_path.with_stem(output_path.stem + "_issues")
    )
    if step in (None, "outputs"):
        check_overwrite(output_path, overwrite=overwrite)
        check_overwrite(issues_path, overwrite=overwrite)

    # "_edge_mosaic" keeps every table/file this call creates distinct from an
    # edge_extend()/edge_match() run against the same input_path/tmp_dir.
    name = (
        input_basename(single_path).replace(".", "_") + "_edge_mosaic"
        if single_path is not None
        else output_path.name.replace(".", "_") + "_edge_mosaic"
    )
    original_table = f"{name}_original_01" if originals else None

    with (
        resolve_tmp_dir(tmp_dir, debug=debug) as tmp_dir_path,
        pipeline_connection(
            name, tmp_dir_path, threads=threads, debug=debug, step=step
        ) as conn,
    ):
        logger.info("starting: %s", name)
        if single_path is None:
            _mosaic_multi_file(
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
                originals=originals,
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
                    load_input(conn, name, paths)
                    load_overlay(conn, name, overlay_path)
                    if originals:
                        load_original(conn, name, originals)
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
                    assign_one(
                        conn,
                        name,
                        overlay_match_column=overlay_match_column,
                        input_match_column=input_match_column,
                        carry_columns=resolved_overlay_columns,
                        input_columns=resolved_input_columns,
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
                        carry_columns=resolved_overlay_columns,
                        input_columns=resolved_input_columns,
                        passthrough=passthrough,
                        result_table=f"{name}_03",
                        raise_if_empty=False,
                        original_table=original_table,
                    )
                    if passthrough:
                        fill_unmatched_overlays(
                            conn,
                            name,
                            carry_columns=resolved_overlay_columns,
                            result_table=f"{name}_03",
                            overlay_snapshot_table=f"{name}_overlay_full",
                            points_table=f"{name}_02_pt_pts",
                        )
                    if not debug:
                        drop_points(conn, f"{name}_02_pt")
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM "{name}_03"'
                    ).fetchone()[0]
                    if count == 0:
                        msg = f"mosaic: no input polygon got an overlay for {name}"
                        raise RuntimeError(msg)
                elif s == "outputs":
                    apply_optional_fill(
                        conn,
                        name,
                        f"{name}_03",
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


def _original_bboxes(
    conn: DuckDBPyConnection, originals: list[Path | str]
) -> list[tuple[Path | str, tuple[float, float, float, float] | None]]:
    """Return each original file's (xmin, ymin, xmax, ymax) extent, None if empty."""
    result = []
    for path in originals:
        row = conn.execute(f"""--sql
            SELECT MIN(xmin), MIN(ymin), MAX(xmax), MAX(ymax)
            FROM (
                SELECT {bbox_columns_sql("geom")}
                FROM ({reproject_select_sql(conn, path, make_valid=False)})
            )
        """).fetchone()
        result.append((path, None if row[0] is None else tuple(row)))
    return result


def _load_nearby_originals(
    conn: DuckDBPyConnection,
    name: str,
    original_bboxes: list[tuple[Path | str, tuple[float, float, float, float] | None]],
) -> None:
    """Expose only the originals overlapping `{name}_input_01` as the original view."""
    bbox = input_bbox_extent(conn, name)
    nearby = [
        path
        for path, obox in original_bboxes
        if bbox is not None
        and obox is not None
        and bboxes_overlap(bbox, obox, 2 * SNAP_TOLERANCE)
    ]
    # One far-away file keeps the view's schema when none is nearby.
    load_original(conn, name, nearby or [original_bboxes[0][0]])


def _mosaic_multi_file(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
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
    originals: list[Path | str],
) -> None:
    """Assign/clip one input file at a time against one already-loaded overlay layer."""
    load_overlay(conn, name, overlay_path)
    original_bboxes = _original_bboxes(conn, originals) if originals else []
    conn.execute(f"""--sql
        CREATE TABLE "{name}_overlay_full" AS SELECT * FROM "{name}_overlay_01"
    """)
    paths = sort_paths_by_column_count_desc(conn, paths)

    combined_bbox: tuple[float, float, float, float] | None = None
    resolved_overlay_columns: list[str] | None = None
    resolved_input_columns: list[str] | None = None
    for i, file_path in enumerate(paths):
        load_input(conn, name, [file_path])
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
    points = f"{name}_02_pt"
    drop_points(conn, points)

    spilled = {
        f"{name}_03_iter": f"{name}_03",
        f"{name}_input_01": f"{name}_input_01",
        f"{name}_02_assign": f"{name}_02_assign",
        f"{name}_02_unassigned": f"{name}_02_unassigned",
        f"{name}_03_iter_dropped": f"{name}_03_dropped",
        f"{name}_03_iter_detached": f"{name}_03_detached",
    }
    spill_dir = tmp_dir_path / f"{name}_spill"
    shutil.rmtree(spill_dir, ignore_errors=True)
    assigned = (
        f"{name}_input_01",
        f"{name}_02_assign",
        f"{name}_02_unassigned",
        f"{name}_overlay_01",
    )
    assigned_dir = tmp_dir_path / f"{name}_assigned"
    shutil.rmtree(assigned_dir, ignore_errors=True)

    # Every file's shared points are collected before any clip, so both sides
    # of a border between two files snap to the same points.
    overlay_bboxes = overlay_fid_bboxes(conn, f"{name}_overlay_full")
    fid_offset = 0
    for i, file_path in enumerate(paths):
        load_input(conn, name, [file_path])
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
        build_tiles(
            conn,
            f'"{name}_overlay_full"',
            f'SELECT overlay_fid FROM "{name}_02_assign"',
            points,
        )
        conn.execute(f"""--sql
            CREATE OR REPLACE VIEW "{points}_in" AS
            SELECT c.fid, a.overlay_fid, c.geom
            FROM "{name}_input_01" c JOIN "{name}_02_assign" a ON a.input_fid = c.fid
        """)
        collect_candidates(conn, f"{points}_in", points)
        for table in assigned:
            spill_table(conn, assigned_dir, table, i)
        new_max = conn.execute(f'SELECT MAX(fid) FROM "{name}_input_01"').fetchone()[0]
        fid_offset = new_max if new_max is not None else fid_offset
    conn.execute(f'DROP VIEW IF EXISTS "{points}_in"')
    pick_points(conn, points)

    for i in range(len(paths)):
        for table in assigned:
            path = assigned_dir / table / f"{i:06d}.parquet"
            conn.execute(f"""--sql
                CREATE OR REPLACE TABLE "{table}" AS
                SELECT * FROM read_parquet('{path}')
            """)
        if originals:
            _load_nearby_originals(conn, name, original_bboxes)
        clip.main(
            conn,
            name,
            tmp_dir_path,
            threads=threads,
            debug=debug,
            carry_columns=resolved_overlay_columns,
            input_columns=resolved_input_columns,
            passthrough=passthrough,
            result_table=f"{name}_03_iter",
            raise_if_empty=False,
            original_table=f"{name}_original_01" if originals else None,
            points=points,
        )

        for table in spilled:
            spill_table(conn, spill_dir, table, i)

        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter_dropped"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter_detached"')
    if not debug:
        shutil.rmtree(assigned_dir, ignore_errors=True)

    for table, dest_table in spilled.items():
        collect_spilled(conn, spill_dir, table, dest_table)
    if not debug:
        shutil.rmtree(spill_dir, ignore_errors=True)

    if passthrough:
        # The last loop iteration left _overlay_01 narrowed to its own
        # file's matched fids; restore it before gap-fill needs every fid.
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_overlay_01" AS
            SELECT * FROM "{name}_overlay_full"
        """)
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_overlay_full"')
        conn.execute(f'DROP VIEW IF EXISTS "{name}_original_01"')
    if passthrough:
        fill_unmatched_overlays(
            conn,
            name,
            carry_columns=resolved_overlay_columns,
            result_table=f"{name}_03",
            overlay_snapshot_table=f"{name}_overlay_01",
            points_table=f"{points}_pts",
        )
    if not debug:
        drop_points(conn, points)

    count = conn.execute(f'SELECT COUNT(*) FROM "{name}_03"').fetchone()[0]
    if count == 0:
        msg = f"mosaic: no input polygon got an overlay for {name}"
        raise RuntimeError(msg)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        SELECT * EXCLUDE (fid), ROW_NUMBER() OVER () AS fid, fid AS input_fid
        FROM "{name}_03"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_passthrough" AS
        SELECT fid FROM "{name}_03"
        WHERE input_fid IN (SELECT input_fid FROM "{name}_02_unassigned")
    """)
    conn.execute(f'ALTER TABLE "{name}_03" DROP COLUMN input_fid')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_overlay_parts"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_overlay_tiles"')

    apply_optional_fill(
        conn,
        name,
        f"{name}_03",
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
        passthrough_fids=f'SELECT fid FROM "{name}_03_passthrough"',
        debug=debug,
    )
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_passthrough"')
