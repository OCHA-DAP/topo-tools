"""Unit tests for core/io.py's remote-URL-aware path helpers."""

from pathlib import Path

import duckdb
import pytest

from topo_tools.core.io import (
    _auto_resolve_layer,
    default_output_path,
    export_geometry_table,
    input_basename,
    read_and_reproject,
    reproject_select_sql,
    resolve_input_path,
)

_URL = "https://data.source.coop/hdx/cod-ab/lka/latest/adm2/original.parquet"


def test_resolve_input_path_keeps_url_unmangled():
    resolved = resolve_input_path(_URL)
    assert resolved == _URL
    assert isinstance(resolved, str)


def test_resolve_input_path_wraps_local_path():
    resolved = resolve_input_path("example.geojson")
    assert resolved == Path("example.geojson")


def test_input_basename_local_path():
    assert input_basename(Path("some/dir/original.parquet")) == "original.parquet"


def test_input_basename_url():
    assert input_basename(_URL) == "original.parquet"


def test_default_output_path_matches_with_stem_for_local_input():
    local = Path("some/dir/original.parquet")
    assert default_output_path(local, "_extended") == local.with_stem(
        local.stem + "_extended"
    )


def test_default_output_path_defaults_to_cwd_for_url_input():
    assert default_output_path(_URL, "_extended") == Path("original_extended.parquet")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeMetaConn:
    """Duck-types DuckDBPyConnection.execute() for ST_Read_Meta results only."""

    def __init__(self, layers):
        self._layers = layers

    def execute(self, _sql, _params=None):
        return _FakeCursor([] if self._layers is None else [(self._layers,)])


def _layer(name, feature_count, *, spatial):
    return {
        "name": name,
        "feature_count": feature_count,
        "geometry_fields": [{"name": "geom"}] if spatial else [],
    }


def test_auto_resolve_layer_none_when_meta_empty():
    assert _auto_resolve_layer(_FakeMetaConn(None), "x") is None


def test_auto_resolve_layer_none_for_single_layer():
    assert (
        _auto_resolve_layer(_FakeMetaConn([_layer("a", 1, spatial=True)]), "x") is None
    )


def test_auto_resolve_layer_picks_sole_spatial_layer():
    layers = [_layer("codebook", 3, spatial=False), _layer("zones", 519, spatial=True)]
    assert _auto_resolve_layer(_FakeMetaConn(layers), "x") == "zones"


def test_auto_resolve_layer_raises_on_multiple_spatial_layers():
    layers = [_layer("a", 1, spatial=True), _layer("b", 2, spatial=True)]
    with pytest.raises(ValueError, match=r"'a' \(1 features\).*'b' \(2 features\)"):
        _auto_resolve_layer(_FakeMetaConn(layers), "x")


def test_auto_resolve_layer_raises_when_no_layer_is_spatial():
    layers = [_layer("a", 1, spatial=False), _layer("b", 2, spatial=False)]
    with pytest.raises(ValueError, match="found 0 carrying geometry"):
        _auto_resolve_layer(_FakeMetaConn(layers), "x")


def test_reproject_select_sql_raises_clear_error_without_geometry(tmp_path):
    path = tmp_path / "no_geom.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"COPY (SELECT 1 AS x) TO '{path}'")
        with pytest.raises(ValueError, match="no geometry column found"):
            reproject_select_sql(conn, path)


def test_read_and_reproject_raises_clear_error_on_invalid_geometry(tmp_path):
    path = tmp_path / "unclosed_ring.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "COPY (SELECT ST_GeomFromText("
            f"'POLYGON((0 0, 1 0, 1 1, 0 1))') AS geom) TO '{path}'"
        )
        with pytest.raises(ValueError, match="invalid geometry"):
            read_and_reproject(conn, "out", path)


def test_read_and_reproject_repairs_geometry_broken_by_reprojection(tmp_path):
    path = tmp_path / "sliver.gpkg"
    sliver = (
        "POLYGON ((454786.21 7408884.019999642, 454785.90625 7408884.499999642, "
        "454785.90625000006 7408884.499999642, 454786.21 7408884.019999642))"
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial; SET geometry_always_xy = true;")
        conn.execute(
            f"COPY (SELECT ST_GeomFromText('{sliver}') AS geom, 1 AS x) TO '{path}' "
            "(FORMAT GDAL, DRIVER 'GPKG', SRS 'EPSG:32721')"
        )
        read_and_reproject(conn, "out", path)
        assert conn.execute(
            'SELECT bool_and(ST_IsValid(geom)) FROM "out_01"'
        ).fetchone()[0]


def test_read_and_reproject_raises_on_zero_rows(tmp_path):
    path = tmp_path / "empty.gpkg"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "CREATE TABLE t AS SELECT ST_GeomFromText('POINT(0 0)') AS geom, 1 AS x"
        )
        conn.execute(
            f"COPY (SELECT * FROM t WHERE false) TO '{path}' "
            "(FORMAT GDAL, DRIVER 'GPKG')"
        )
        with pytest.raises(ValueError, match="read 0 features"):
            read_and_reproject(conn, "out", path)


def test_explicit_layer_overrides_auto_detection(tmp_path):
    path = tmp_path / "namecheck.gpkg"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "CREATE TABLE t AS SELECT ST_GeomFromText('POINT(0 0)') AS geom, 1 AS x"
        )
        conn.execute(f"COPY t TO '{path}' (FORMAT GDAL, DRIVER 'GPKG')")
        read_and_reproject(conn, "out", path, layer=path.stem)
        rows = conn.execute('SELECT * FROM "out_01"').fetchall()
    assert len(rows) == 1


def test_export_geometry_table_writes_geometry_first(tmp_path):
    dest = tmp_path / "out.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "CREATE TABLE t AS SELECT 1 AS fid, 'a' AS x, "
            "ST_GeomFromText('POINT(0 0)') AS geom"
        )
        export_geometry_table(conn, "t", dest)
        columns = [r[0] for r in conn.execute(f"DESCRIBE FROM '{dest}'").fetchall()]
    assert columns == ["geometry", "x"]


@pytest.mark.parametrize(
    ("suffix", "copy_opts"),
    [
        (".parquet", "(FORMAT PARQUET, GEOPARQUET_VERSION 'NONE')"),
        (".parquet", "(FORMAT PARQUET, GEOPARQUET_VERSION 'V1')"),
        (".geojson", "(FORMAT GDAL, DRIVER 'GeoJSON')"),
    ],
    ids=["parquet-no-crs", "geoparquet-crs84", "geojson"],
)
def test_read_and_reproject_tags_every_source_epsg4326(tmp_path, suffix, copy_opts):
    path = tmp_path / f"src{suffix}"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "COPY (SELECT 1 AS x, "
            "ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))') AS geom) "
            f"TO '{path}' {copy_opts}"
        )
        read_and_reproject(conn, "out", path)
        geom_type = conn.execute('SELECT typeof(geom) FROM "out_01"').fetchone()[0]
    assert geom_type == "GEOMETRY('EPSG:4326')"


def test_export_geometry_table_tags_epsg4326(tmp_path):
    dest = tmp_path / "out.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            "CREATE TABLE t AS SELECT 1 AS fid, ST_GeomFromText('POINT(0 0)') AS geom"
        )
        export_geometry_table(conn, "t", dest)
        geom_type = conn.execute(f"DESCRIBE FROM '{dest}'").fetchone()[1]
    assert geom_type == "GEOMETRY('EPSG:4326')"
