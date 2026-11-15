"""Tests for core.duckdb_utils."""

from topo_tools.core.duckdb_utils import get_connection


def test_where_on_crs_geometry_keeps_every_row(tmp_path):
    conn = get_connection("t", tmp_path)
    conn.execute("""--sql
        CREATE TABLE t AS
        SELECT ST_SetCRS(ST_MakeLine(list_transform(
            range(2 + i % 7), lambda k: ST_Point(i + k, k)
        )), 'OGC:CRS84') AS geom
        FROM range(100000) r(i)
    """)
    conn.execute("CHECKPOINT")
    where = conn.execute("SELECT count(*) FROM t WHERE ST_NPoints(geom) >= 6")
    assert where.fetchall() == [(42856,)]
    conn.close()
