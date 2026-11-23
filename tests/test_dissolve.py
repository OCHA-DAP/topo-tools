"""Unit tests for core.dissolve's column-classification logic."""

import duckdb
import pytest

from topo_tools.core.dissolve import _02_dissolve as dissolve_stage

_ROWS = [
    # adm1, adm2, name, area_sqkm, population, status, version, bbox, wkt
    (
        "P1",
        "A1",
        "Alpha",
        10.0,
        100,
        "active",
        1,
        "junk1",
        "POLYGON((0 0,1 0,1 1,0 1,0 0))",
    ),
    (
        "P1",
        "A2",
        "Beta",
        20.0,
        200,
        "pending",
        1,
        "junk2",
        "POLYGON((1 0,2 0,2 1,1 1,1 0))",
    ),
    (
        "P2",
        "B1",
        "Gamma",
        5.0,
        50,
        "inactive",
        1,
        "junk3",
        "POLYGON((0 1,1 1,1 2,0 2,0 1))",
    ),
]


@pytest.fixture
def conn():
    with duckdb.connect() as connection:
        connection.execute("INSTALL spatial; LOAD spatial;")
        values = ", ".join(
            f"('{p}', '{a}', '{n}', {area}, {pop}, '{status}', {version}, "
            f"'{bbox}', ST_GeomFromText('{wkt}'))"
            for p, a, n, area, pop, status, version, bbox, wkt in _ROWS
        )
        connection.execute(f"""--sql
            CREATE TABLE t_01 AS
            SELECT row_number() OVER () AS fid, *
            FROM (VALUES {values})
            AS v(adm1_pcode, adm2_pcode, adm2_name, area_sqkm, population,
                 status, version, bbox, geom)
        """)
        yield connection


def test_varying_numeric_column_summed_by_default(conn):
    dissolve_stage.main(conn, "t_01", "t_out", group_by=["adm1_pcode"])
    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            'SELECT adm1_pcode, area_sqkm, population FROM "t_out"'
        ).fetchall()
    }
    assert rows["P1"] == (30.0, 300)
    assert rows["P2"] == (5.0, 50)


def test_constant_column_kept_unaffected(conn):
    dissolve_stage.main(conn, "t_01", "t_out", group_by=["adm1_pcode"])
    versions = [r[0] for r in conn.execute('SELECT version FROM "t_out"').fetchall()]
    assert versions == [1, 1]


def test_varying_non_numeric_column_dropped(conn):
    dissolve_stage.main(conn, "t_01", "t_out", group_by=["adm1_pcode"])
    columns = {row[0] for row in conn.execute('DESCRIBE "t_out"').fetchall()}
    assert "status" not in columns


def test_noise_column_excluded_from_output(conn):
    dissolve_stage.main(conn, "t_01", "t_out", group_by=["adm1_pcode"])
    columns = {row[0] for row in conn.execute('DESCRIBE "t_out"').fetchall()}
    assert "bbox" not in columns


def test_aggregation_override_replaces_default_sum(conn):
    dissolve_stage.main(
        conn,
        "t_01",
        "t_out",
        group_by=["adm1_pcode"],
        aggregations={"area_sqkm": "max"},
    )
    rows = dict(conn.execute('SELECT adm1_pcode, area_sqkm FROM "t_out"').fetchall())
    assert rows == {"P1": 20.0, "P2": 5.0}


def test_unsupported_aggregation_raises(conn):
    with pytest.raises(ValueError, match="unsupported aggregation"):
        dissolve_stage.main(
            conn,
            "t_01",
            "t_out",
            group_by=["adm1_pcode"],
            aggregations={"area_sqkm": "median"},
        )


def test_fid_follows_group_by_order(conn):
    conn.execute("CREATE TABLE t_rev AS SELECT * FROM t_01 ORDER BY fid DESC")
    dissolve_stage.main(conn, "t_rev", "t_out", group_by=["adm2_pcode"])
    rows = conn.execute('SELECT fid, adm2_pcode FROM "t_out" ORDER BY fid').fetchall()
    assert rows == [(1, "A1"), (2, "A2"), (3, "B1")]


def test_ungrouped_dissolve_with_no_kept_column(conn):
    conn.execute('CREATE TABLE t_geo AS SELECT fid, status, geom FROM "t_01"')
    dissolve_stage.main(conn, "t_geo", "t_out", group_by=[])
    columns = [row[0] for row in conn.execute('DESCRIBE "t_out"').fetchall()]
    assert columns == ["fid", "geom"]
