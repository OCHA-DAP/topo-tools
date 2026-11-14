"""Tests for the package() composite tool (polygons + points + lines in one call)."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.package import package
from topo_tools.api.package_lines import package_lines
from topo_tools.api.package_points import package_points
from topo_tools.api.package_polygons import package_polygons
from topo_tools.cli.main import cli

_ROWS = [
    {
        "adm1_pcode": "P1",
        "adm2_pcode": "A1",
        "wkt": "POLYGON((0 0, 0 10, 10 10, 10 0, 0 0))",
    },
    {
        "adm1_pcode": "P1",
        "adm2_pcode": "A2",
        "wkt": "POLYGON((10 0, 10 10, 20 10, 20 0, 10 0))",
    },
    {
        "adm1_pcode": "P2",
        "adm2_pcode": "B1",
        "wkt": "POLYGON((0 10, 0 20, 10 20, 10 10, 0 10))",
    },
]


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


@pytest.fixture
def admin2_input(tmp_path):
    path = tmp_path / "admin2.parquet"
    _write_synthetic(path, _ROWS)
    return path


def test_default_output_matches_calling_each_tool_separately(tmp_path):
    combined_dir = tmp_path / "combined"
    combined_dir.mkdir()
    combined_input = combined_dir / "admin2.parquet"
    _write_synthetic(combined_input, _ROWS)
    package(
        combined_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
        block=False,
    )

    separate_dir = tmp_path / "separate"
    separate_dir.mkdir()
    separate_input = separate_dir / "admin2.parquet"
    _write_synthetic(separate_input, _ROWS)
    package_polygons(
        separate_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    package_points(
        separate_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    package_lines(
        separate_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    reports = {f"admin2_{s}_issues.parquet" for s in ("schema", "topo", "code", "name")}
    combined_names = {p.name for p in combined_dir.glob("*.parquet")} - reports
    separate_names = {p.name for p in separate_dir.glob("*.parquet")}
    assert combined_names == separate_names


def test_x_template_substitutes_per_subtool(admin2_input, tmp_path):
    template = str(tmp_path / "web_{x}.parquet")
    package(
        admin2_input,
        template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
        block=False,
    )

    assert (tmp_path / "web_admin1.parquet").exists()
    assert (tmp_path / "web_admin2.parquet").exists()
    assert (tmp_path / "web_points.parquet").exists()
    assert (tmp_path / "web_lines.parquet").exists()


def test_missing_x_raises(admin2_input, tmp_path):
    with pytest.raises(ValueError, match="literal '\\{x\\}'"):
        package(
            admin2_input,
            str(tmp_path / "flat.parquet"),
            name_field="adm{n}_name",
            code_field="adm{n}_pcode",
            overwrite=True,
            block=False,
        )


def test_overwrite_and_name_field_code_field_passed_through(admin2_input, tmp_path):
    template = str(tmp_path / "web_{x}.parquet")
    package(
        admin2_input,
        template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
        block=False,
    )

    with pytest.raises(FileExistsError, match="already exists"):
        package(
            admin2_input,
            template,
            name_field="adm{n}_name",
            code_field="adm{n}_pcode",
            overwrite=False,
            block=False,
        )


def test_min_level_leaves_out_coarser_levels(admin2_input, tmp_path):
    """min_level=2 writes no admin1 polygons, points or lines below level 2."""
    out = tmp_path / "out"
    package(
        admin2_input,
        str(out / "{x}.parquet"),
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        min_level=2,
        block=False,
    )
    assert not (out / "admin1.parquet").exists()
    assert (out / "admin2.parquet").exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        point_levels = conn.execute(
            f"SELECT DISTINCT adm_lvl FROM '{out / 'points.parquet'}'"
        ).fetchall()
        line_levels = conn.execute(
            f"SELECT DISTINCT adm_lvl FROM '{out / 'lines.parquet'}'"
        ).fetchall()
    assert point_levels == [(2,)]
    assert line_levels == [(2,)]


def test_cli_default_naming(admin2_input):
    result = CliRunner().invoke(
        cli,
        [
            "package",
            "--no-block",
            str(admin2_input),
            "--name-field",
            "adm{n}_name",
            "--code-field",
            "adm{n}_pcode",
        ],
    )
    assert result.exit_code == 0, result.output
    assert admin2_input.with_stem(admin2_input.stem + "_admin1").exists()
    assert admin2_input.with_stem(admin2_input.stem + "_points").exists()
    assert admin2_input.with_stem(admin2_input.stem + "_lines").exists()


def test_cli_output_template(admin2_input, tmp_path):
    result = CliRunner().invoke(
        cli,
        [
            "package",
            "--no-block",
            str(admin2_input),
            "--output",
            str(tmp_path / "web_{x}.parquet"),
            "--name-field",
            "adm{n}_name",
            "--code-field",
            "adm{n}_pcode",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "web_points.parquet").exists()


@pytest.mark.parametrize("tool", [package_polygons, package_points, package_lines])
def test_raises_on_overlapping_input(tool, tmp_path):
    path = tmp_path / "admin2.parquet"
    _write_synthetic(path, [*_ROWS, {**_ROWS[0], "adm2_pcode": "A9"}])
    with pytest.raises(ValueError, match=r"pair\(s\) overlap"):
        tool(path, tmp_path / "out.parquet")


def test_unusable_column_name_blocks_before_any_output(tmp_path):
    """A name packaging can't give an output column stops it before polygons."""
    rows = [
        {
            "adm1": r["adm1_pcode"],
            "adm1_pcode": r["adm1_pcode"],
            "adm1_name": f"n{r['adm1_pcode']}",
            "adm2": r["adm2_pcode"],
            "adm2_pcode": r["adm2_pcode"],
            "adm2_name": f"n{r['adm2_pcode']}",
            "wkt": r["wkt"],
        }
        for r in _ROWS
    ]
    input_path = tmp_path / "in.parquet"
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="validation found errors"):
        package(input_path, name_field="adm{n}_name", code_field="adm{n}_pcode")
    assert not list(tmp_path.glob("in_admin*.parquet"))
    assert not list(tmp_path.glob("in_points*")) + list(tmp_path.glob("in_lines*"))
