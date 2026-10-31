"""Shared geodata read/write helpers."""

import shutil
from logging import getLogger
from pathlib import Path
from urllib.parse import urlparse

import duckdb
from duckdb import DuckDBPyConnection

from .constants import COPY_OPTS, CRS, GDAL_CRS, RESERVED_COLUMN_NAMES
from .coverage import close_notches, coverage_clean, has_valid_topology
from .duckdb_utils import quote_identifier

logger = getLogger(__name__)

# Esri FileGDB API type GUID for a feature class (spatial); stable across
# versions, and how GDAL's own OpenFileGDB driver tells layers apart too.
_ESRI_FEATURE_CLASS_TYPE = "{70737809-852C-4A03-9E22-2CECEA5B9BFA}"


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
    colliding_cols = [col[0] for col in schema if col[0] in RESERVED_COLUMN_NAMES]
    if colliding_cols:
        logger.warning(
            "renaming source column(s) %s to %s to avoid colliding with "
            "topo-tools' internal fid / GDAL's reserved OGC_FID field",
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
    geom_expr = (
        "ST_Force2D(ST_MakeValid(ST_Transform("
        f"ST_MakeValid(\"{geom_col}\"), '{CRS}', true)))"
        if reproject
        else f"ST_SetCRS(ST_Force2D(ST_MakeValid(\"{geom_col}\")), '{CRS}')"
    )
    if not make_valid:
        geom_expr = (
            f"ST_Force2D(ST_Transform(\"{geom_col}\", '{CRS}', true))"
            if reproject
            else f"ST_SetCRS(ST_Force2D(\"{geom_col}\"), '{CRS}')"
        )

    # ST_MakeValid runs again after transform, since reprojection can break
    # validity; ST_Force2D drops Z/M coords downstream GEOS ops can't handle.
    return f"""
        SELECT * EXCLUDE ({exclude_sql}){rename_sql},
               row_number() OVER () AS fid,
               {geom_expr} AS geom
        FROM ({read_expr})
    """


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
    select_sql = reproject_select_sql(conn, path, layer)
    try:
        conn.execute(f'CREATE OR REPLACE TABLE "{name}_01" AS {select_sql}')
    except duckdb.Error as e:
        msg = (
            f"failed to read/reproject {path}: {e}; the source file may contain "
            "invalid geometry (e.g. an unclosed ring) that ST_MakeValid cannot repair"
        )
        raise ValueError(msg) from e

    count = conn.execute(f'SELECT COUNT(*) FROM "{name}_01"').fetchone()[0]
    if count == 0:
        layer_note = f" (layer={layer!r})" if layer else ""
        msg = (
            f"read 0 features from {path}{layer_note}. Try re-exporting it "
            "first, e.g. `gdal vector convert` or `pyogrio` to GeoParquet/GPKG"
        )
        raise ValueError(msg)


def read_reproject_and_clean(
    conn: DuckDBPyConnection,
    name: str,
    path: Path | str,
    layer: str | None = None,
    *,
    fix_notches: bool = False,
) -> None:
    """Read, reproject, and coverage-clean geodata into table `{name}_01`."""
    read_and_reproject(conn, name, path, layer)

    table = f"{name}_01"
    if fix_notches:
        close_notches(conn, table)
    if not has_valid_topology(conn, table, gap_maximum_width=0):
        logger.info("cleaning coverage: invalid edges or gaps detected")
        coverage_clean(conn, table, table, fids=None)


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
