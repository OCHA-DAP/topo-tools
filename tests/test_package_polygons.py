"""Portability + naming tests for the package_polygons() tool."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.package_polygons import package_polygons
from topo_tools.cli.main import cli

_BASE_ROWS = [
    {
        "adm2_pcode": "A1",
        "adm1_pcode": "P1",
        "adm2_name": "Alpha",
        "adm1_name": "Province1",
        "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    },
    {
        "adm2_pcode": "A2",
        "adm1_pcode": "P1",
        "adm2_name": "Beta",
        "adm1_name": "Province1",
        "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
    },
    {
        "adm2_pcode": "B1",
        "adm1_pcode": "P2",
        "adm2_name": "Gamma",
        "adm1_name": "Province2",
        "wkt": "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))",
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


def _describe_columns(path) -> set[str]:
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return {
            row[0]
            for row in conn.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()
        }


@pytest.fixture
def admin2_input(tmp_path):
    path = tmp_path / "admin2.parquet"
    _write_synthetic(path, _BASE_ROWS)
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["package-polygons", "--help"])
    assert result.exit_code == 0
    assert "Merge a polygon layer" in result.output
    assert "Examples:" in result.output


def test_default_naming_produces_one_file_per_level(admin2_input):
    package_polygons(
        admin2_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    admin1_out = admin2_input.with_stem(admin2_input.stem + "_admin1")
    admin2_out = admin2_input.with_stem(admin2_input.stem + "_admin2")
    assert admin1_out.exists()
    assert admin2_out.exists()

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        admin1_count = conn.execute(f"SELECT COUNT(*) FROM '{admin1_out}'").fetchone()[
            0
        ]
    expected_admin1_count = 2
    assert admin1_count == expected_admin1_count


def test_coarser_level_keeps_input_column_order(admin2_input):
    package_polygons(
        admin2_input,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    admin1_out = admin2_input.with_stem(admin2_input.stem + "_admin1")
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{admin1_out}'").fetchall()
    ]
    assert columns == ["geometry", "adm1_pcode", "adm1_name"]


def test_finest_level_skipped_when_path_equals_input(tmp_path):
    input_path = tmp_path / "admin2.parquet"
    _write_synthetic(input_path, _BASE_ROWS)
    template = str(tmp_path / "admin{n}.parquet")

    package_polygons(
        input_path,
        template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    assert (tmp_path / "admin1.parquet").exists()
    assert input_path.exists()  # untouched, not overwritten by its own dissolve


def test_output_path_template_used_per_level(admin2_input, tmp_path):
    template = str(tmp_path / "level_{n}.parquet")
    package_polygons(
        admin2_input,
        template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    assert (tmp_path / "level_1.parquet").exists()
    assert (tmp_path / "level_2.parquet").exists()


def test_output_path_missing_n_raises(admin2_input, tmp_path):
    with pytest.raises(ValueError, match="literal '\\{n\\}'"):
        package_polygons(
            admin2_input,
            str(tmp_path / "flat.parquet"),
            name_field="adm{n}_name",
            code_field="adm{n}_pcode",
            overwrite=True,
        )


def test_finest_level_written_when_path_resolves_elsewhere(admin2_input, tmp_path):
    template = str(tmp_path / "out_{n}.parquet")
    package_polygons(
        admin2_input,
        template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    finest_out = tmp_path / "out_2.parquet"
    assert finest_out.exists()
    assert _describe_columns(finest_out) & {"adm2_pcode", "adm1_pcode"}


def test_overwrite_false_raises_on_existing_coarser_output(admin2_input, tmp_path):
    template = str(tmp_path / "out_{n}.parquet")
    (tmp_path / "out_1.parquet").touch()
    with pytest.raises(FileExistsError, match="already exists"):
        package_polygons(
            admin2_input,
            template,
            name_field="adm{n}_name",
            code_field="adm{n}_pcode",
            overwrite=False,
        )


def test_issues_path_follows_same_n_template(admin2_input, tmp_path):
    output_template = str(tmp_path / "out_{n}.parquet")
    issues_template = str(tmp_path / "issues_{n}.parquet")
    package_polygons(
        admin2_input,
        output_template,
        issues_template,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )
    assert (tmp_path / "out_1.parquet").exists()
    # Level 1's issues file may be skipped entirely if empty (shared
    # issues-table convention), so only assert the output side here.


def test_cli_default_naming(admin2_input):
    result = CliRunner().invoke(
        cli,
        [
            "package-polygons",
            str(admin2_input),
            "--name-field",
            "adm{n}_name",
            "--code-field",
            "adm{n}_pcode",
        ],
    )
    assert result.exit_code == 0, result.output
    assert admin2_input.with_stem(admin2_input.stem + "_admin1").exists()
    assert admin2_input.with_stem(admin2_input.stem + "_admin2").exists()


def test_mismatched_name_code_prefixes_drop_finer_names(tmp_path):
    rename = {
        "adm2_pcode": "GID_2",
        "adm1_pcode": "GID_1",
        "adm2_name": "NAME_2",
        "adm1_name": "NAME_1",
    }
    path = tmp_path / "gadm.parquet"
    _write_synthetic(
        path, [{rename.get(k, k): v for k, v in r.items()} for r in _BASE_ROWS]
    )
    package_polygons(path, name_field="NAME_{n}", code_field="GID_{n}", overwrite=True)

    admin1_columns = _describe_columns(path.with_stem(path.stem + "_admin1"))
    assert {"GID_1", "NAME_1"} <= admin1_columns
    assert not {"GID_2", "NAME_2"} & admin1_columns


def test_output_templates_rename_written_columns(admin2_input, tmp_path):
    package_polygons(
        admin2_input,
        str(tmp_path / "level_{n}.parquet"),
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        output_name_field="adm{n}_label",
        output_code_field="adm{n}_code",
    )
    assert {"adm1_label", "adm1_code"} <= _describe_columns(
        tmp_path / "level_1.parquet"
    )
    admin2_columns = _describe_columns(tmp_path / "level_2.parquet")
    assert {"adm2_label", "adm2_code", "adm1_label", "adm1_code"} <= admin2_columns
    assert not {"adm2_name", "adm2_pcode"} & admin2_columns


def test_output_templates_require_input_templates(admin2_input):
    with pytest.raises(ValueError, match="require name_field/code_field"):
        package_polygons(admin2_input, output_code_field="adm{n}_code")


def test_cli_package_output_code_field(admin2_input, tmp_path):
    result = CliRunner().invoke(
        cli,
        [
            "package",
            "--no-validate",
            str(admin2_input),
            "--output",
            str(tmp_path / "{x}.parquet"),
            "--name-field",
            "adm{n}_name",
            "--code-field",
            "adm{n}_pcode",
            "--output-code-field",
            "adm{n}_code",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "adm1_code" in _describe_columns(tmp_path / "admin1.parquet")
