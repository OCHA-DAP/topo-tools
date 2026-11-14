"""Correctness + portability tests for the package_lines() tool."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.package_lines import package_lines
from topo_tools.cli.main import cli


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def _write_synthetic(path, rows: list[dict]) -> None:
    cols = [k for k in rows[0] if k != "wkt"]
    col_list = ", ".join([*cols, "geom"])
    values = ", ".join(
        "("
        + ", ".join(_sql_literal(r[c]) for c in cols)
        + f", ST_GeomFromText('{r['wkt']}'))"
        for r in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t({col_list})"
        )
        conn.execute(f"COPY synth TO '{path}'")


def _fetch_lines(path):
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"""
            SELECT a_code, b_code, adm_lvl, ST_AsText(geometry)
            FROM '{path}'
        """).fetchall()


def test_cli_help():
    result = CliRunner().invoke(cli, ["package-lines", "--help"])
    assert result.exit_code == 0
    assert "line layer of admin boundaries" in result.output
    assert "Examples:" in result.output


def test_two_adjacent_squares_one_shared_row(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    shared = [r for r in rows if r[1] is not None]
    assert len(shared) == 1
    assert {shared[0][0], shared[0][1]} == {"A", "B"}


def test_explicit_schema_carries_finest_siblings_no_metadata(tmp_path):
    """Each side carries the finest level's code/name/siblings, nothing else."""
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {
                "adm0_id": "Z",
                "adm0_name": "Zed",
                "adm1_id": code,
                "adm1_name": f"n{code}",
                "adm1_name1": f"m{code}",
                "adm1_pcode": f"x{code}",
                "valid_on": "2020-01-01",
                "wkt": wkt,
            }
            for code, wkt in [
                ("A", "POLYGON((0 0,0 10,10 10,10 0,0 0))"),
                ("B", "POLYGON((10 0,10 10,20 10,20 0,10 0))"),
            ]
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path, output_path, name_field="adm{n}_name", code_field="adm{n}_id"
    )
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0]
            for r in conn.execute(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
        }
        shared = conn.execute(f"""
            SELECT a_code, a_name, a_name1, a_pcode, b_code, b_name, b_name1, b_pcode
            FROM '{output_path}' WHERE b_code IS NOT NULL
        """).fetchall()
    assert columns == {
        *(f"{side}_{g}" for side in "ab" for g in ("code", "name", "name1", "pcode")),
        "adm_lvl",
        "geometry",
    }
    assert [sorted(r[i] for i in (0, 4)) for r in shared] == [["A", "B"]]
    assert {r[0]: r[1:4] for r in shared} | {r[4]: r[5:] for r in shared} == {
        "A": ("nA", "mA", "xA"),
        "B": ("nB", "mB", "xB"),
    }


def test_depth_column_collision_with_output_column_raises(
    tmp_path,
):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    with pytest.raises(ValueError, match="geom"):
        package_lines(
            input_path,
            output_path,
            name_field="adm{n}_name",
            code_field="adm{n}_pcode",
            depth_column="geom",
            overwrite=True,
        )


def test_every_exterior_row_has_null_right_code(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    exterior = [r for r in rows if r[1] is None]
    assert exterior
    assert all(r[1] is None for r in exterior)


def test_three_squares_middle_produces_two_exterior_rows(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
            {"adm1_pcode": "C", "wkt": "POLYGON((20 0,20 10,30 10,30 0,20 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    exterior_by_code: dict[str, list[str]] = {}
    for left_code, right_code, _adm_lvl, geom in rows:
        if right_code is None:
            exterior_by_code.setdefault(left_code, []).append(geom)

    expected_middle_exterior_count = 2
    middle_geoms = next(
        g for g in exterior_by_code.values() if len(g) == expected_middle_exterior_count
    )
    assert all(g.startswith("LINESTRING") for g in middle_geoms)


def test_corner_touch_produces_zero_shared_rows(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 10,10 20,20 20,20 10,10 10))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    assert not [r for r in rows if r[1] is not None]


def test_multi_part_fid_touching_and_remote(tmp_path):
    """A 2-part fid: one part touches a neighbor, one is a remote island."""
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {
                "adm1_pcode": "A",
                "wkt": (
                    "MULTIPOLYGON(((0 0,0 10,10 10,10 0,0 0)),"
                    "((100 70,100 80,110 80,110 70,100 70)))"
                ),
            },
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    shared = [r for r in rows if r[1] is not None]
    assert len(shared) == 1
    remote_exterior = [r for r in rows if r[1] is None and "100 70" in r[3]]
    assert remote_exterior


def test_every_input_code_appears_at_least_once(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
            {"adm1_pcode": "C", "wkt": "POLYGON((20 0,20 10,30 10,30 0,20 0))"},
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows = _fetch_lines(output_path)
    present = {r[0] for r in rows} | {r[1] for r in rows if r[1] is not None}
    assert present == {"A", "B", "C"}


def test_multi_level_classification(tmp_path):
    """4 admin2 units under 2 admin1 parents: same-parent -> finer, else coarser."""
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {
                "adm1_pcode": "P1",
                "adm2_pcode": "X1",
                "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))",
            },
            {
                "adm1_pcode": "P1",
                "adm2_pcode": "X2",
                "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))",
            },
            {
                "adm1_pcode": "P2",
                "adm2_pcode": "Y1",
                "wkt": "POLYGON((20 0,20 10,30 10,30 0,20 0))",
            },
            {
                "adm1_pcode": "P2",
                "adm2_pcode": "Y2",
                "wkt": "POLYGON((30 0,30 10,40 10,40 0,30 0))",
            },
        ],
    )
    output_path = tmp_path / "lines.parquet"
    package_lines(
        input_path,
        output_path,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        shared = conn.execute(f"""
            SELECT ST_X(ST_PointN(geometry, 1)), ST_X(ST_PointN(geometry, 2)),
                   adm_lvl, a_code, b_code
            FROM '{output_path}' WHERE b_code IS NOT NULL
        """).fetchall()
        exterior = conn.execute(f"""
            SELECT DISTINCT adm_lvl, a_code FROM '{output_path}'
            WHERE b_code IS NULL ORDER BY a_code
        """).fetchall()
    by_x = {frozenset((x1, x2)): row for x1, x2, *row in shared}
    finer_level = 2
    # Each row is labelled with the units at its own level.
    assert by_x[frozenset((10.0, 10.0))][0] == finer_level  # X1-X2, same P1
    assert set(by_x[frozenset((10.0, 10.0))][1:]) == {"X1", "X2"}
    assert by_x[frozenset((30.0, 30.0))][0] == finer_level  # Y1-Y2, same P2
    assert by_x[frozenset((20.0, 20.0))][0] == 1  # X2-Y1, P1 vs P2
    assert set(by_x[frozenset((20.0, 20.0))][1:]) == {"P1", "P2"}
    assert exterior == [(0, "P1"), (0, "P2")]


def test_cli_default_naming(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(
        input_path,
        [
            {"adm1_pcode": "A", "wkt": "POLYGON((0 0,0 10,10 10,10 0,0 0))"},
            {"adm1_pcode": "B", "wkt": "POLYGON((10 0,10 10,20 10,20 0,10 0))"},
        ],
    )
    result = CliRunner().invoke(
        cli,
        [
            "package-lines",
            str(input_path),
            "--name-field",
            "adm{n}_name",
            "--code-field",
            "adm{n}_pcode",
        ],
    )
    assert result.exit_code == 0, result.output
    assert input_path.with_stem(input_path.stem + "_lines").exists()
