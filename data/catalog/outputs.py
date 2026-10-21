"""Write catalog outputs only when their rows change, so `portolan add` leaves unchanged files alone."""

import json
from pathlib import Path

import duckdb


def same_rows(new: Path, old: Path, *, ordered: bool = False) -> bool:
    """Check that `old` has every column of `new` with the same rows (in the same order if `ordered`), ignoring extras like portolan's `bbox`."""
    con = duckdb.connect()
    con.execute("LOAD spatial")
    columns = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{new}')").fetchall()
    old_columns = {
        name: kind
        for name, kind, *_ in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{old}')"
        ).fetchall()
    }
    if any(old_columns.get(name) != kind for name, kind, *_ in columns):
        return False
    select = ", ".join(
        f"ST_AsWKB({name}) AS {name}" if kind.startswith("GEOMETRY") else name
        for name, kind, *_ in columns
    )
    if ordered:
        select += ", file_row_number"
    options = ", file_row_number = true" if ordered else ""
    a, b = (f"SELECT {select} FROM read_parquet('{p}'{options})" for p in (new, old))
    differ = con.execute(
        f"SELECT count(*) FROM (({a} EXCEPT ALL {b}) UNION ALL ({b} EXCEPT ALL {a}))"
    ).fetchone()[0]
    return differ == 0


def write_parquet(
    con: duckdb.DuckDBPyConnection, query: str, out: Path, *, ordered: bool = False
) -> bool:
    """Write `query` to `out` unless `out` already holds the same rows. Returns True when written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    new = out.with_name(f"{out.stem}.new.parquet")
    con.execute(f"COPY ({query}) TO '{new}' (FORMAT parquet, COMPRESSION zstd)")
    if ordered:
        add_bbox_covering(con, query, new)
    if out.exists() and same_rows(new, out, ordered=ordered):
        new.unlink()
        return False
    new.replace(out)
    return True


def add_bbox_covering(con: duckdb.DuckDBPyConnection, query: str, path: Path) -> None:
    """Rewrite `path` as GeoParquet 1.1 with a bbox covering column, which `portolan add` keeps as is instead of Hilbert-sorting."""
    geo = json.loads(
        con.execute(
            f"SELECT decode(value) FROM parquet_kv_metadata('{path}') WHERE key = 'geo'"
        ).fetchone()[0]
    )
    geo["version"] = "1.1.0"
    geo["columns"]["geometry"]["covering"] = {
        "bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}
    }
    metadata = json.dumps(geo).replace("'", "''")
    con.execute(
        "COPY (SELECT *, struct_pack(xmin := ST_XMin(geometry), ymin := ST_YMin(geometry), "
        f"xmax := ST_XMax(geometry), ymax := ST_YMax(geometry)) AS bbox FROM ({query})) TO '{path}' "
        f"(FORMAT parquet, COMPRESSION zstd, GEOPARQUET_VERSION 'NONE', KV_METADATA {{geo: '{metadata}'}})"
    )


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
