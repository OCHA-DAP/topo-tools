"""Public API: fit already-extended input polygons into a new overlay layer."""

import shutil
from logging import getLogger
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection

from topo_tools.api._schema_fill_compose import apply_optional_fill, validate_fill_flags
from topo_tools.core.assign import (
    assign_one,
    bboxes_overlap,
    check_input,
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
    union_input_schema,
    validate_merge_flags,
)
from topo_tools.core.clip import build_tiles
from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.coverage import check_no_overlaps
from topo_tools.core.duckdb_utils import (
    bbox_columns_sql,
    collect_spilled,
    create_lookup_copy,
    maybe_export_debug_tables,
    pipeline_connection,
    resolve_tmp_dir,
    spill_table,
)
from topo_tools.core.edge_mosaic import _01_clip as clip
from topo_tools.core.edge_mosaic import _02_clean as clean_stage
from topo_tools.core.edge_mosaic import _03_outputs as outputs
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    input_basename,
    log_world_clipped,
    raise_world_bounds_error,
    reproject_select_sql,
    resolve_input_path,
    sort_paths_by_column_count_desc,
)
from topo_tools.core.schema_map._level_columns import detect_level_codes

logger = getLogger(__name__)

_STEP_ORDER = ["inputs", "assign", "clip", "clean", "outputs"]

_STEP_TABLES = {
    "inputs": ["{n}_input_01", "{n}_overlay_01", "{n}_overlay_full", "{n}_original_01"],
    "assign": ["{n}_02_pairs", "{n}_02_assign", "{n}_02_unassigned"],
    "clip": [
        "{n}_03",
        "{n}_03_dropped",
        "{n}_03_detached",
        "{n}_03_cover",
        "{n}_02_gap_fill",
    ],
    "clean": ["{n}_03", "{n}_03_micro"],
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
    fill: dict[str, str] | None = None,
    clean: bool = True,
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
        fill=fill,
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
                fill=fill,
                clean=clean,
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
                    check_input(conn, name, single_path)
                    load_overlay(conn, name, overlay_path, require_coverage=True)
                    if originals and _needs_originals(conn, name, single_path):
                        keyless: dict[Path | str, list[Path | str]] = {}
                        _load_keyed_originals(
                            conn,
                            name,
                            _original_keys(conn, originals),
                            single_path,
                            keyless=keyless,
                        )
                        _warn_keyless(keyless)
                    if passthrough:
                        create_lookup_copy(
                            conn, f"{name}_overlay_01", f"{name}_overlay_full"
                        )
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
                                fill=fill,
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
                                fill=fill,
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
                        original_table=_paired_original(conn, name)
                        if originals
                        else None,
                    )
                    if passthrough:
                        fill_unmatched_overlays(
                            conn,
                            name,
                            carry_columns=resolved_overlay_columns,
                            result_table=f"{name}_03",
                            overlay_snapshot_table=f"{name}_overlay_full",
                            fill=fill,
                        )
                    count = conn.execute(
                        f'SELECT COUNT(*) FROM "{name}_03"'
                    ).fetchone()[0]
                    if count == 0:
                        msg = f"mosaic: no input polygon got an overlay for {name}"
                        raise RuntimeError(msg)
                elif s == "clean":
                    if clean:
                        clean_stage.main(conn, name, passthrough=passthrough)
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
                        clean=clean,
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
        select = reproject_select_sql(
            conn, path, make_valid=False, flag_world_clip=True
        )
        try:
            row = conn.execute(f"""--sql
                SELECT MIN(xmin), MIN(ymin), MAX(xmax), MAX(ymax),
                       count(*) FILTER (WHERE _world_clipped)
                FROM (SELECT {bbox_columns_sql("geom")}, _world_clipped
                      FROM ({select}))
            """).fetchone()
        except duckdb.Error as e:
            raise_world_bounds_error(e, str(path))
            raise
        log_world_clipped(str(path), row[4])
        result.append((path, None if row[0] is None else tuple(row[:4])))
    return result


def _original_keys(
    conn: DuckDBPyConnection, originals: list[Path | str]
) -> dict[Path | str, str | None]:
    """Return each original file's own finest-level code column, None if undetected."""
    keys: dict[Path | str, str | None] = {}
    for path in originals:
        source = reproject_select_sql(conn, path, make_valid=False)
        conn.execute(
            f"CREATE OR REPLACE TEMP TABLE _original_attrs AS "
            f"SELECT * EXCLUDE (geom) FROM ({source})"
        )
        keys[path] = None
        # One row gives detection no structure to rank levels by.
        if conn.execute("SELECT count(*) FROM _original_attrs").fetchall()[0][0] > 1:
            try:
                codes = detect_level_codes(conn, "_original_attrs")
                keys[path] = codes[max(codes)]
            except ValueError as e:
                logger.debug("original %s: %s", path, e)
        conn.execute("DROP TABLE _original_attrs")
    return keys


def _needs_originals(
    conn: DuckDBPyConnection, name: str, input_path: Path | str
) -> bool:
    """Drop any original view; False if the input has one feature to pair."""
    conn.execute(f'DROP VIEW IF EXISTS "{name}_original_01"')
    rows = conn.execute(f'SELECT count(*) FROM "{name}_input_01"').fetchall()[0][0]
    if rows > 1:
        return True
    # A detached piece merges only into a neighbouring row, which one row lacks.
    logger.info("input %s has one feature, so pairs no original", input_path)
    return False


def _load_keyed_originals(
    conn: DuckDBPyConnection,
    name: str,
    original_keys: dict[Path | str, str | None],
    input_path: Path | str,
    *,
    keyless: dict[Path | str, list[Path | str]],
) -> None:
    """Pair originals with `{name}_input_01` by each one's own code, if any pair.

    Each original without a code column gets input_path appended in keyless.
    """
    conn.execute(f'DROP VIEW IF EXISTS "{name}_original_01"')
    for path, key in original_keys.items():
        if key is None:
            keyless.setdefault(path, []).append(input_path)
    columns = {r[0] for r in conn.execute(f'DESCRIBE "{name}_input_01"').fetchall()}
    # A key the input lacks marks another input's original, so none of it pairs.
    usable = {p: k for p, k in original_keys.items() if k in columns}
    counts = load_original(conn, name, usable)
    for path, (paired, unmatched) in counts.items():
        # An original pairing nothing belongs to another input, so stays quiet.
        if paired and unmatched:
            logger.warning(
                "original %s: %d %s codes match no input feature in %s",
                path,
                unmatched,
                usable[path],
                input_path,
            )
    if not any(paired for paired, _ in counts.values()):
        conn.execute(f'DROP VIEW "{name}_original_01"')
        logger.warning(
            "input %s runs without originals: none pairs by code", input_path
        )


def _warn_keyless(keyless: dict[Path | str, list[Path | str]]) -> None:
    """Warn once per original without a code column, naming every input it missed."""
    for path, inputs in keyless.items():
        logger.warning(
            "original %s has no detectable code column, so pairs nothing in %s",
            path,
            ", ".join(str(p) for p in inputs),
        )


def _paired_original(conn: DuckDBPyConnection, name: str) -> str | None:
    """Return the original view's name if originals paired with the input, else None."""
    found = conn.execute(
        f"SELECT 1 FROM duckdb_views() WHERE view_name = '{name}_original_01'"
    ).fetchall()
    return f"{name}_original_01" if found else None


def _load_nearby_originals(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    original_bboxes: list[tuple[Path | str, tuple[float, float, float, float] | None]],
    original_keys: dict[Path | str, str | None],
    input_path: Path | str,
    *,
    keyless: dict[Path | str, list[Path | str]],
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
    if not nearby:
        conn.execute(f'DROP VIEW IF EXISTS "{name}_original_01"')
        logger.warning("input %s runs without originals: none is near it", input_path)
        return
    _load_keyed_originals(
        conn,
        name,
        {p: original_keys[p] for p in nearby},
        input_path,
        keyless=keyless,
    )


def _check_shared_winner(
    conn: DuckDBPyConnection,
    name: str,
    i: int,
    winners: dict[int, list[int]],
    assigned_dir: Path,
) -> None:
    """Raise ValueError if file i overlaps an earlier file on the same overlay fid."""
    for (overlay_fid,) in conn.execute(
        f'SELECT DISTINCT overlay_fid FROM "{name}_02_assign"'
    ).fetchall():
        earlier = winners.setdefault(overlay_fid, [])
        if earlier:
            spilled = [
                str(assigned_dir / f"{name}_input_01" / f"{j:06d}.parquet")
                for j in earlier
            ]
            tmp = f"{name}_02_tmp1"
            conn.execute(f"""--sql
                CREATE OR REPLACE TABLE "{tmp}" AS
                SELECT source_file, geom FROM "{name}_input_01"
                UNION ALL
                SELECT source_file, geom FROM read_parquet({spilled})
            """)
            try:
                check_no_overlaps(conn, tmp, group_column="source_file")
            except ValueError as e:
                msg = f"input files assigned to overlay fid {overlay_fid} overlap: {e}"
                raise ValueError(msg) from e
            finally:
                conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
        earlier.append(i)


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
    fill: dict[str, str] | None,
    clean: bool,
    fill_schema: bool,
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
    originals: list[Path | str],
) -> None:
    """Assign/clip one input file at a time against one already-loaded overlay layer."""
    load_overlay(conn, name, overlay_path, require_coverage=True)
    original_bboxes = _original_bboxes(conn, originals) if originals else []
    original_keys = _original_keys(conn, originals)
    create_lookup_copy(conn, f"{name}_overlay_01", f"{name}_overlay_full")
    paths = sort_paths_by_column_count_desc(conn, paths)

    combined_bbox: tuple[float, float, float, float] | None = None
    for i, file_path in enumerate(paths):
        load_input(conn, name, [file_path])
        check_input(conn, name, file_path)
        union_input_schema(conn, name, first=i == 0)
        bbox = input_bbox_extent(conn, name)
        if bbox is not None:
            combined_bbox = (
                bbox if combined_bbox is None else union_bbox(combined_bbox, bbox)
            )
        conn.execute(f'DROP TABLE IF EXISTS "{name}_input_01"')
    resolved_overlay_columns, resolved_input_columns = resolve_merge_columns(
        conn,
        name,
        merge=merge,
        overlay_include=overlay_include,
        overlay_exclude=overlay_exclude,
        input_include=input_include,
        input_exclude=input_exclude,
        prefer=prefer,
        fill=fill,
        input_table=f"{name}_input_schema",
    )
    conn.execute(f'DROP TABLE IF EXISTS "{name}_input_schema"')
    prepare_overlay_tiles(conn, name, input_bbox=combined_bbox)

    passthrough = merge
    tiles = f"{name}_02_tiles"
    conn.execute(f'DROP TABLE IF EXISTS "{tiles}"')

    spilled = {
        f"{name}_03_iter": f"{name}_03",
        f"{name}_input_01": f"{name}_input_01",
        f"{name}_02_assign": f"{name}_02_assign",
        f"{name}_02_unassigned": f"{name}_02_unassigned",
        f"{name}_03_iter_dropped": f"{name}_03_dropped",
        f"{name}_03_iter_detached": f"{name}_03_detached",
        f"{name}_03_iter_cover": f"{name}_03_cover",
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

    # Every file is assigned before any clip, so files overlapping on an overlay
    # polygon raise before the clip's hours of work.
    overlay_bboxes = overlay_fid_bboxes(conn, f"{name}_overlay_full")
    fid_offset = 0
    winners: dict[int, list[int]] = {}
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
        _check_shared_winner(conn, name, i, winners, assigned_dir)
        build_tiles(
            conn,
            f'"{name}_overlay_full"',
            f'SELECT overlay_fid FROM "{name}_02_assign"',
            tiles,
        )
        for table in assigned:
            spill_table(conn, assigned_dir, table, i)
        new_max = conn.execute(f'SELECT MAX(fid) FROM "{name}_input_01"').fetchone()[0]
        fid_offset = new_max if new_max is not None else fid_offset

    keyless: dict[Path | str, list[Path | str]] = {}
    try:
        for i in range(len(paths)):
            for table in assigned:
                path = assigned_dir / table / f"{i:06d}.parquet"
                conn.execute(f"""--sql
                    CREATE OR REPLACE TABLE "{table}" AS
                    SELECT * FROM read_parquet('{path}')
                """)
            if originals and _needs_originals(conn, name, paths[i]):
                _load_nearby_originals(
                    conn,
                    name,
                    original_bboxes,
                    original_keys,
                    paths[i],
                    keyless=keyless,
                )
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
                original_table=_paired_original(conn, name) if originals else None,
                tiles=tiles,
            )

            for table in spilled:
                spill_table(conn, spill_dir, table, i)

            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter"')
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter_dropped"')
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter_detached"')
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_iter_cover"')
    finally:
        _warn_keyless(keyless)
    if not debug:
        shutil.rmtree(assigned_dir, ignore_errors=True)

    for table, dest_table in spilled.items():
        collect_spilled(conn, spill_dir, table, dest_table)
    if not debug:
        shutil.rmtree(spill_dir, ignore_errors=True)

    # The last loop iteration left _overlay_01 narrowed to its own file's
    # matched fids; gap-fill and the gap rows need every fid.
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
            fill=fill,
        )
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{tiles}"')

    count = conn.execute(f'SELECT COUNT(*) FROM "{name}_03"').fetchone()[0]
    if count == 0:
        msg = f"mosaic: no input polygon got an overlay for {name}"
        raise RuntimeError(msg)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03_passthrough" AS
        SELECT input_fid AS fid FROM "{name}_02_unassigned"
    """)

    if clean:
        clean_stage.main(
            conn,
            name,
            passthrough=passthrough,
            passthrough_fids=f'SELECT fid FROM "{name}_03_passthrough"',
        )
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
        clean=clean,
        debug=debug,
    )
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03_passthrough"')
