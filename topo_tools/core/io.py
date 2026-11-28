"""Shared geodata read/write helpers."""

import shutil
import time
from logging import getLogger
from pathlib import Path
from urllib.parse import urlparse

import duckdb
from duckdb import DuckDBPyConnection

from .constants import (
    COPY_OPTS,
    CRS,
    GDAL_CRS,
    RESERVED_COLUMN_NAMES,
    SNAP_TOLERANCE,
    WORLD_BOUNDS_WKT,
)
from .coverage import (
    close_notches,
    coverage_clean,
    has_valid_topology,
)
from .duckdb_utils import quote_identifier

logger = getLogger(__name__)
_WORLD_CLIP_FLAG = "_world_clipped"

# Esri FileGDB API type GUID for a feature class (spatial); stable across
# versions, and how GDAL's own OpenFileGDB driver tells layers apart too.
_ESRI_FEATURE_CLASS_TYPE = "{70737809-852C-4A03-9E22-2CECEA5B9BFA}"
_OUTSIDE_WORLD = "a feature lies entirely outside world bounds (-180 -90 180 90)"
_POLE_RING = "a ring circles a pole without edges along it, in source row "
_LON_FLAG = "_lon_normalized"
_POLY_FLAG = "_poly"
# Pieces left this narrow by the antimeridian cut are float overshoot, dropped.
_ANTIMERIDIAN_SLIVER_DEG = 1e-6


def resolve_input_path(path: str | Path) -> Path | str:
    """Keep an http(s):// URL as a raw string; `Path()` mangles its "//"."""
    return path if str(path).startswith(("http://", "https://")) else Path(path)


def input_basename(path: Path | str) -> str:
    """Filesystem-safe basename, for a local `Path` or a remote URL alike."""
    if isinstance(path, Path):
        return path.name
    return Path(urlparse(path).path).name


def check_overwrite(path: Path, *, overwrite: bool) -> None:
    """Raise if path exists and overwrite is off; log and proceed otherwise."""
    if not path.exists():
        return
    if not overwrite:
        msg = f"output already exists: {path}"
        raise FileExistsError(msg)
    logger.info("overwriting existing output: %s", path)


def default_output_path(input_path: Path | str, suffix: str) -> Path:
    """Default output path: input's own directory (CWD if remote), stem + suffix.

    A remote `input_path` has no directory of its own, so it defaults to CWD.
    """
    directory = input_path.parent if isinstance(input_path, Path) else Path()
    base = Path(input_basename(input_path))
    return directory / base.with_stem(base.stem + suffix).name


def _filegdb_spatial_layers(
    conn: DuckDBPyConnection, path: Path | str
) -> list[str] | None:
    """Feature-class names via a FileGDB's own catalog table; never opens a layer."""
    catalog = Path(path) / "a00000004.gdbtable"
    if not catalog.exists():
        return None
    query = (
        f"SELECT Name FROM ST_Read('{catalog}') "
        "WHERE Name <> '' AND Path = '\\' || Name AND Type = ? ORDER BY Name"
    )
    try:
        rows = conn.execute(query, [_ESRI_FEATURE_CLASS_TYPE]).fetchall()
    except duckdb.Error:
        return None
    return [row[0] for row in rows]


def _auto_resolve_layer(conn: DuckDBPyConnection, path: Path | str) -> str | None:
    """Auto-pick the sole geometry-bearing layer; None defers to ST_Read's default."""
    filegdb_layers = _filegdb_spatial_layers(conn, path)
    if filegdb_layers:
        if len(filegdb_layers) == 1:
            return filegdb_layers[0]
        candidates = ", ".join(repr(name) for name in filegdb_layers)
        msg = (
            f"{path} is a FileGDB with {len(filegdb_layers)} feature classes, "
            f"not exactly 1 ({candidates}); pass layer= (CLI: --layer) with "
            "the layer name"
        )
        raise ValueError(msg)
    if filegdb_layers is not None:
        return None

    meta = conn.execute("SELECT layers FROM ST_Read_Meta(?)", [str(path)]).fetchall()
    if not meta or not meta[0][0] or len(meta[0][0]) <= 1:
        return None
    layers = meta[0][0]
    spatial = [layer for layer in layers if layer["geometry_fields"]]
    if len(spatial) == 1:
        return spatial[0]["name"]
    candidates = ", ".join(
        f"{layer['name']!r} ({layer['feature_count']} features)" for layer in layers
    )
    msg = (
        f"{path} has multiple layers and auto-detection found "
        f"{len(spatial)} carrying geometry, not exactly 1 ({candidates}); "
        "pass layer= (CLI: --layer) with the layer name"
    )
    raise ValueError(msg)


def reproject_select_sql(
    conn: DuckDBPyConnection,
    path: Path | str,
    layer: str | None = None,
    *,
    make_valid: bool = True,
    flag_world_clip: bool = False,
) -> str:
    """Build the read+reproject-to-EPSG:4326 SELECT for one file, as unexecuted SQL.

    Composable as a subquery, so multiple files can combine via
    `UNION ALL BY NAME` into one `CREATE TABLE ... AS SELECT`, no per-file table.
    """
    is_parquet = Path(input_basename(path)).suffix == ".parquet"
    if layer is None and not is_parquet:
        layer = _auto_resolve_layer(conn, path)

    if is_parquet:
        read_expr = f"SELECT * FROM '{path}'"
    elif layer is not None:
        read_expr = f"SELECT * FROM ST_Read('{path}', layer='{layer}')"
    else:
        read_expr = f"SELECT * FROM ST_Read('{path}')"

    schema = conn.execute(f"DESCRIBE {read_expr}").fetchall()
    geom_match = next(
        ((col[0], col[1]) for col in schema if col[1].startswith("GEOMETRY")), None
    )
    if geom_match is None:
        layer_note = f" (layer={layer!r})" if layer else ""
        msg = (
            f"no geometry column found reading {path}{layer_note}; this source "
            "may have multiple layers that couldn't be auto-detected (a known "
            "gap for FileGDB). Find the layer name (e.g. `gdal vector info "
            f"{path}`) and pass layer= (CLI: --layer)"
        )
        raise ValueError(msg)
    geom_col, geom_type = geom_match
    # A source column already named "fid"/"OGC_FID" would otherwise collide
    # with our own row_number() AS fid below; rename it once here.
    reserved = (*RESERVED_COLUMN_NAMES, _POLY_FLAG, _LON_FLAG, _WORLD_CLIP_FLAG)
    colliding_cols = [col[0] for col in schema if col[0] in reserved]
    if colliding_cols:
        logger.warning(
            "renaming source column(s) %s to %s to avoid colliding with "
            "topo-tools' internal columns / GDAL's reserved OGC_FID field",
            colliding_cols,
            [f"{c}_orig" for c in colliding_cols],
        )
    exclude_cols = [
        col[0]
        for col in schema
        if col[1].startswith("GEOMETRY")
        or (col[0].endswith("_bbox") and col[1].startswith("STRUCT"))
    ] + colliding_cols
    exclude_sql = ", ".join(f'"{c}"' for c in exclude_cols)
    rename_sql = "".join(f', "{c}" AS "{c}_orig"' for c in colliding_cols)

    # ST_Read tags geometry with source CRS; single-arg ST_Transform infers it.
    # A CRS-less geometry is assumed to already be WGS84 lon/lat, so only tagged;
    # one already in CRS skips the transform, which roughly doubles peak memory.
    reproject = geom_type not in ("GEOMETRY", f"GEOMETRY('{CRS}')")
    raw = f'"{geom_col}"'
    if reproject:
        raw = f"ST_MakeValid({raw})" if make_valid else raw
        base = f"ST_Transform({raw}, '{CRS}', true)"
    else:
        base = f"ST_SetCRS({raw}, '{CRS}')"
    norm = _normalize_longitude_sql("geom", str(path).replace("'", "''"))
    # ST_MakeValid runs again after transform, since reprojection can break
    # validity; ST_Force2D drops Z/M coords downstream GEOS ops can't handle.
    if make_valid:
        # ST_MakeValid can collapse a polygon sliver to a line; keep polygonal parts.
        norm = (
            f"CASE WHEN {_POLY_FLAG} "
            f"THEN ST_CollectionExtract(ST_MakeValid({norm}), 3) "
            f"ELSE ST_MakeValid({norm}) END"
        )
    geom_expr = f"ST_SetCRS(ST_Force2D({norm}), '{CRS}')"

    lon_flag_sql = (
        f", {_lon_normalize_needed_sql('geom')} AS {_LON_FLAG}"
        if flag_world_clip
        else ""
    )
    flag_sql = (
        f", ({_LON_FLAG} OR NOT {_within_world_sql('geom')}) AS {_WORLD_CLIP_FLAG}"
        if flag_world_clip
        else ""
    )
    lon_exclude_sql = f"EXCLUDE ({_LON_FLAG}) " if flag_world_clip else ""
    return f"""
        SELECT * {lon_exclude_sql}REPLACE ({_world_clip_sql("geom")} AS geom){flag_sql}
        FROM (
            SELECT * EXCLUDE ({_POLY_FLAG}) REPLACE ({geom_expr} AS geom){lon_flag_sql}
            FROM (
                SELECT * EXCLUDE ({exclude_sql}){rename_sql},
                       row_number() OVER () AS fid,
                       {base} AS geom,
                       ST_Dimension("{geom_col}") = 2 AS {_POLY_FLAG}
                FROM ({read_expr})
            )
        )
    """


def _let(value: str, var: str, body: str) -> str:
    """Bind `value` to lambda parameter `var` inside `body`, evaluated once."""
    return f"list_transform([{value}], lambda {var}: {body})[1]"


def _jump_signs_sql(xy: str) -> str:
    """Per-vertex -1/0/1 shift where the edge into it crosses 180°."""
    pole = 90 - SNAP_TOLERANCE
    is_pole = f"""lambda i: i > 1 AND abs({xy}[i][2]) >= {pole}
        AND abs({xy}[i - 1][2]) >= {pole}"""
    short = f"""list_transform(range(1, len({xy}) + 1), lambda i: CASE
        WHEN i = 1 OR (abs({xy}[i][2]) >= {pole} AND abs({xy}[i - 1][2]) >= {pole})
        THEN 0
        WHEN {xy}[i][1] - {xy}[i - 1][1] > 180 THEN -1
        WHEN {xy}[i][1] - {xy}[i - 1][1] < -180 THEN 1
        ELSE 0 END)"""
    # Pole edges take the shift that closes the ring; a ring all on ±180 is a band.
    closed = f"""CASE
        WHEN list_bool_and(list_transform({xy}, lambda v: abs(v[1]) = 180))
        THEN list_transform(s0, lambda v: 0)
        WHEN list_sum(s0) = 0 OR len(pe) = 0 THEN s0
        ELSE list_transform(range(1, len(s0) + 1),
            lambda i: CASE WHEN i = pe[1] THEN -list_sum(s0) ELSE s0[i] END) END"""
    poles = f"list_filter(range(1, len({xy}) + 1), {is_pole})"
    return _let(short, "s0", _let(poles, "pe", closed))


def _ring_xy_sql(ring: str) -> str:
    """List of [x, y] vertex pairs of linestring `ring`, in order."""
    return (
        f"list_transform(ST_Dump(ST_Points({ring})), "
        "lambda d: [ST_X(d['geom']), ST_Y(d['geom'])])"
    )


def _rings_sql(part: str) -> str:
    """List of polygon `part`'s rings, shell first."""
    return f"""list_transform(range(0, ST_NumInteriorRings({part}) + 1),
        lambda k: CASE WHEN k = 0 THEN ST_ExteriorRing({part})
        ELSE ST_InteriorRingN({part}, k::INTEGER) END)"""


def _cut_sql(poly: str) -> str:
    """Cut `poly` into 360°-wide windows, each shifted back into ±180."""
    windows = (
        f"range(floor((ST_XMin({poly}) + 180) / 360)::BIGINT, "
        f"floor((ST_XMax({poly}) + 180) / 360)::BIGINT + 1)"
    )
    pieces = f"""list_filter(list_transform({windows}, lambda w:
        ST_Translate(ST_Intersection({poly}, ST_MakeEnvelope(
            360 * w - 180, -1000, 360 * w + 180, 1000)), -360 * w, 0)),
        lambda g: NOT ST_IsEmpty(g))"""
    sliver = _ANTIMERIDIAN_SLIVER_DEG
    wide = f"list_filter(ps, lambda g: ST_XMax(g) - ST_XMin(g) > {sliver})"
    return _let(
        pieces, "ps", f"ST_Collect(CASE WHEN len({wide}) > 0 THEN {wide} ELSE ps END)"
    )


def _ring_pieces_sql(ring: str, label: str) -> str:
    """Unwrap `ring` across 180° into a polygon, then cut it into ±180."""
    unwrapped = """ST_MakeLine(list_transform(range(1, len(q) + 1), lambda i: ST_Point(
        q[i][1] + 360 * coalesce(list_sum(list_transform(
            list_filter(jp, lambda j: j <= i), lambda j: s[j])), 0),
        q[i][2])))"""
    poly = f"ST_CollectionExtract(ST_MakeValid(ST_MakePolygon({unwrapped})), 3)"
    cut = _let(poly, "pg", _cut_sql("pg"))
    body = f"""CASE WHEN coalesce(list_sum(s), 0) <> 0
        THEN error('{_POLE_RING}' || fid || ' of {label}') ELSE {cut} END"""
    with_jumps = _let(
        "list_filter(range(1, len(s) + 1), lambda j: s[j] <> 0)", "jp", body
    )
    return _let(_ring_xy_sql(ring), "q", _let(_jump_signs_sql("q"), "s", with_jumps))


def _part_needs_sql(part: str) -> str:
    """Build SQL true where polygon `part` crosses 180° or goes past ±180."""
    ring_jumps = _let(_ring_xy_sql("rg"), "q", _jump_signs_sql("q"))
    return f"""CASE
        WHEN ST_GeometryType({part})::VARCHAR <> 'POLYGON' THEN false
        WHEN ST_XMin({part}) < -180 OR ST_XMax({part}) > 180 THEN true
        WHEN ST_XMax({part}) - ST_XMin({part}) > 180
        THEN coalesce(list_bool_or(list_transform({_rings_sql(part)},
            lambda rg: list_bool_or(list_transform({ring_jumps}, lambda x: x <> 0)))),
            false)
        ELSE false END"""


def _lon_normalize_needed_sql(col: str) -> str:
    """Build SQL true where any polygon part of `col` needs normalizing."""
    # DuckDB evaluates both sides of AND on every row; CASE runs THEN only where true.
    wide = _let(
        f"ST_Extent({col})",
        "b",
        "ST_XMin(b) < -180 OR ST_XMax(b) > 180 OR ST_XMax(b) - ST_XMin(b) > 180",
    )
    return f"""CASE WHEN coalesce({wide}, false)
        THEN coalesce(ST_Dimension({col}) = 2
            AND list_bool_or(list_transform(ST_Dump({col}),
                lambda d: {_part_needs_sql("d['geom']")})), false)
        ELSE false END"""


def _normalize_part_sql(part: str, label: str) -> str:
    """Cut each ring of polygon `part` into ±180, then subtract the holes."""
    shell = _ring_pieces_sql(f"ST_ExteriorRing({part})", label)
    hole = _ring_pieces_sql(f"ST_InteriorRingN({part}, h::INTEGER)", label)
    holes = f"""ST_CollectionExtract(ST_Collect(list_transform(
        range(1, ST_NumInteriorRings({part}) + 1), lambda h: {hole})), 3)"""
    diff = f"""CASE WHEN ST_NumInteriorRings({part}) = 0 THEN {shell}
        ELSE ST_Difference({shell}, {holes}) END"""
    return f"ST_CollectionExtract(ST_MakeValid({diff}), 3)"


def _normalize_longitude_sql(col: str, label: str) -> str:
    """Build SQL splitting `col`'s polygon parts at 180° and wrapping longitudes."""
    fixed = _normalize_part_sql("d['geom']", label)
    parts = f"""list_transform(ST_Dump(ST_Force2D({col})), lambda d: CASE
        WHEN {_part_needs_sql("d['geom']")} THEN {fixed} ELSE d['geom'] END)"""
    return f"""CASE WHEN {_lon_normalize_needed_sql(col)}
        THEN ST_SetCRS(ST_CollectionExtract(ST_Collect({parts}), 3), '{CRS}')
        ELSE {col} END"""


def _within_world_sql(col: str) -> str:
    """Build SQL true where `col` is empty or its bbox is within world bounds."""
    return f"""(ST_IsEmpty({col}) OR (
            ST_XMin({col}) >= -180 AND ST_XMax({col}) <= 180
            AND ST_YMin({col}) >= -90 AND ST_YMax({col}) <= 90
        ))"""


def _world_clip_sql(col: str) -> str:
    """Build SQL clipping `col` to world bounds, only where its bbox goes past them."""
    world = f"ST_GeomFromText('{WORLD_BOUNDS_WKT}')"
    return f"""CASE
        WHEN {_within_world_sql(col)} THEN {col}
        WHEN NOT ST_Intersects({col}, {world})
        THEN error('{_OUTSIDE_WORLD}')
        ELSE ST_SetCRS(ST_CollectionExtract(
            ST_Intersection(ST_MakeValid({col}), {world}), 3
        ), '{CRS}')
        END"""


def raise_world_bounds_error(e: duckdb.Error, label: str) -> None:
    """Re-raise a world-bounds read error from `e` as a ValueError naming the file."""
    text = str(e)
    if _OUTSIDE_WORLD in text:
        msg = f"failed to read {label}: {_OUTSIDE_WORLD}"
        raise ValueError(msg) from e
    if _POLE_RING in text:
        row, _, source = text.split(_POLE_RING, 1)[1].partition(" of ")
        msg = f"failed to read {source.splitlines()[0]}: {_POLE_RING}{row}"
        raise ValueError(msg) from e


def log_world_clipped(label: str, clipped: int) -> None:
    """Warn that `clipped` of `label`'s features were fitted to world bounds."""
    if clipped:
        logger.warning(
            "%s: fitted %d feature(s) to world bounds (split or wrapped at 180° "
            "longitude, clipped past 90° latitude)",
            label,
            clipped,
        )


def warn_world_clipped(conn: DuckDBPyConnection, table: str, label: str) -> None:
    """Log how many of `table`'s rows the world clip changed, then drop the flag."""
    clipped = conn.execute(
        f'SELECT count(*) FILTER (WHERE {_WORLD_CLIP_FLAG}) FROM "{table}"'
    ).fetchall()[0][0]
    log_world_clipped(label, clipped)
    conn.execute(f'ALTER TABLE "{table}" DROP COLUMN {_WORLD_CLIP_FLAG}')


def sort_paths_by_column_count_desc(
    conn: DuckDBPyConnection, paths: list[Path | str]
) -> list[Path | str]:
    """Order paths by column count descending (ties by path), widest schema first."""

    def _column_count(path: Path | str) -> int:
        select_sql = reproject_select_sql(conn, path)
        return len(conn.execute(f"DESCRIBE ({select_sql})").fetchall())

    return sorted(paths, key=lambda p: (-_column_count(p), str(p)))


def read_and_reproject(
    conn: DuckDBPyConnection, name: str, path: Path | str, layer: str | None = None
) -> None:
    """Read geodata, reproject to EPSG:4326, store as canonical table `{name}_01`."""
    select_sql = reproject_select_sql(conn, path, layer, flag_world_clip=True)
    try:
        conn.execute(f'CREATE OR REPLACE TABLE "{name}_01" AS {select_sql}')
    except duckdb.Error as e:
        raise_world_bounds_error(e, str(path))
        msg = (
            f"failed to read/reproject {path}: {e}; the source file may contain "
            "invalid geometry (e.g. an unclosed ring) that ST_MakeValid cannot repair"
        )
        raise ValueError(msg) from e
    warn_world_clipped(conn, f"{name}_01", str(path))

    count = conn.execute(f'SELECT COUNT(*) FROM "{name}_01"').fetchone()[0]
    if count == 0:
        layer_note = f" (layer={layer!r})" if layer else ""
        msg = (
            f"read 0 features from {path}{layer_note}. Try re-exporting it "
            "first, e.g. `gdal vector convert` or `pyogrio` to GeoParquet/GPKG"
        )
        raise ValueError(msg)


def read_reproject_and_clean(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    name: str,
    path: Path | str,
    layer: str | None = None,
    *,
    fix_notches: bool = False,
    keep_raw: bool = False,
) -> None:
    """Read, reproject, and coverage-clean geodata into table `{name}_01`.

    keep_raw saves the uncleaned rows as `{name}_00` when a clean runs.
    """
    start = time.monotonic()
    read_and_reproject(conn, name, path, layer)
    logger.info("read %s in %.0fs", path, time.monotonic() - start)

    table = f"{name}_01"
    conn.execute(f'DROP TABLE IF EXISTS "{name}_00"')
    if fix_notches:
        start = time.monotonic()
        close_notches(conn, table)
        logger.info("notches closed in %.0fs", time.monotonic() - start)
    start = time.monotonic()
    valid = has_valid_topology(conn, table, gap_maximum_width=0)
    logger.info("coverage checked in %.0fs", time.monotonic() - start)
    if not valid:
        logger.info("cleaning coverage: invalid edges or gaps detected")
        if keep_raw:
            conn.execute(f'CREATE TABLE "{name}_00" AS SELECT * FROM "{table}"')
        start = time.monotonic()
        coverage_clean(conn, table, table, fids=None)
        logger.info("coverage cleaned in %.0fs", time.monotonic() - start)
        if keep_raw:
            _restore_removed_rows(conn, table, f"{name}_00")


def _restore_removed_rows(conn: DuckDBPyConnection, table: str, raw: str) -> None:
    """Re-add rows the clean merged away, as read, so every input row is matched."""
    removed = f'SELECT r.* FROM "{raw}" r ANTI JOIN "{table}" t USING (fid)'
    count = conn.execute(f"SELECT count(*) FROM ({removed})").fetchall()[0][0]
    if count:
        logger.warning("%s: kept %d row(s) the clean removed, as read", table, count)
        conn.execute(f'INSERT INTO "{table}" BY NAME {removed}')


def raw_rows_sql(conn: DuckDBPyConnection, table: str, raw: str) -> str:
    """Select `table`'s rows with `raw`'s geom where `raw` exists."""
    exists = conn.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [raw]
    ).fetchall()[0][0]
    if not exists:
        return f'SELECT * FROM "{table}"'
    return f"""
        SELECT t.* REPLACE (coalesce(r.geom, t.geom) AS geom)
        FROM "{table}" t LEFT JOIN "{raw}" r USING (fid)
    """


def export_geometry_table(
    conn: DuckDBPyConnection,
    table: str,
    dest: Path,
    *,
    exclude_fid: bool = True,
    renames: dict[str, str] | None = None,
) -> None:
    """Export a geometry table to dest, `geom` as the first column, `geometry`."""
    dest.parent.mkdir(exist_ok=True, parents=True)
    exclude = "geom, fid" if exclude_fid else "geom"
    rename = ", ".join(
        f"{quote_identifier(a)} AS {quote_identifier(b)}"
        for a, b in (renames or {}).items()
    )
    rename_sql = f" RENAME ({rename})" if rename else ""
    crs = CRS if dest.suffix == ".parquet" else GDAL_CRS
    conn.execute(f"""--sql
        COPY (
            SELECT ST_SetCRS(geom, '{crs}') AS geometry,
                   * EXCLUDE ({exclude}){rename_sql}
            FROM "{table}"
        ) TO '{dest}' {COPY_OPTS[dest.suffix]}
    """)


def add_csv_bom(path: Path) -> None:
    """Prefix a `.csv` file with a UTF-8 BOM so spreadsheet apps detect its encoding."""
    if path.suffix != ".csv":
        return
    tmp = path.with_name(path.name + ".tmp")
    with path.open("rb") as src, tmp.open("wb") as dst:
        dst.write(b"\xef\xbb\xbf")
        shutil.copyfileobj(src, dst)
    tmp.replace(path)


def export_issues_table(conn: DuckDBPyConnection, table: str, dest: Path) -> None:
    """Export an issues table to dest, or remove any stale file there if it's empty.

    Never writes an empty issues file: a prior run's stale file at dest is
    deleted instead of being left behind looking like unresolved issues.
    """
    count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    if count == 0:
        dest.unlink(missing_ok=True)
        return
    export_geometry_table(conn, table, dest, exclude_fid=False)
