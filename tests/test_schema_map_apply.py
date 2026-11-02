"""Smoke tests for schema-map applying an imported crosswalk (--csv)."""

import csv

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.schema_map import map as schema_map
from topo_tools.cli.main import cli

_STEPS = ["inputs", "apply", "outputs"]


def apply_crosswalk(input_path, crosswalk_path, output_path=None, **kwargs):
    schema_map(input_path, output_path, csv_input=crosswalk_path, **kwargs)


def _write_table(path, col_names, rows):
    def _cell(col, val):
        if col == "geom":
            return f"ST_GeomFromText('{val}')"
        return "NULL" if val is None else f"'{val}'"

    values = ", ".join(
        "(" + ", ".join(_cell(c, v) for c, v in zip(col_names, row, strict=True)) + ")"
        for row in rows
    )
    cols_decl = ", ".join(col_names)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t({cols_decl})"
        )
        conn.execute(f"COPY synth TO '{path}'")


def _unit_square(i):
    return f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))"


_CROSSWALK_FIELDS = ["source_column", "target_column", "confidence", "note"]


def _write_crosswalk(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CROSSWALK_FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def apply_input(tmp_path):
    path = tmp_path / "apply_input.parquet"
    _write_table(
        path,
        ["geom", "Name_Old", "Code_Old", "Extra"],
        [
            (_unit_square(0), "Alpha", "A1", "junk1"),
            (_unit_square(1), "Beta", "A2", "junk2"),
        ],
    )
    return path


@pytest.fixture
def full_crosswalk(tmp_path):
    return _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {
                "source_column": "Name_Old",
                "target_column": "adm1_name",
                "confidence": "exact",
                "note": None,
            },
            {
                "source_column": "Code_Old",
                "target_column": "adm1_pcode",
                "confidence": "exact",
                "note": None,
            },
            {
                "source_column": "Extra",
                "target_column": None,
                "confidence": "unmatched",
                "note": "drop me",
            },
        ],
    )


def test_cli_help():
    result = CliRunner().invoke(cli, ["schema-map", "--help"])
    assert result.exit_code == 0
    assert "--csv" in result.output
    assert "Examples:" in result.output


def test_full_run_renames_and_drops(apply_input, full_crosswalk, tmp_path):
    out = tmp_path / "mapped.parquet"
    apply_crosswalk(apply_input, full_crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
        names = conn.execute(
            f"SELECT adm1_name FROM '{out}' ORDER BY adm1_name"
        ).fetchall()
    assert columns == {"adm1_name", "adm1_pcode", "geometry"}
    assert names == [("Alpha",), ("Beta",)]


def test_bom_prefixed_crosswalk_applies(apply_input, full_crosswalk, tmp_path):
    full_crosswalk.write_bytes(b"\xef\xbb\xbf" + full_crosswalk.read_bytes())
    out = tmp_path / "mapped.parquet"
    apply_crosswalk(apply_input, full_crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert columns == {"adm1_name", "adm1_pcode", "geometry"}


def test_default_output_path(apply_input, full_crosswalk):
    apply_crosswalk(apply_input, full_crosswalk, overwrite=True)

    expected = apply_input.with_stem(apply_input.stem + "_mapped")
    assert expected.exists()


def test_crosswalk_missing_column_raises(apply_input, tmp_path):
    """The crosswalk never decided about "Extra": refactor must not guess."""
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name"},
            {"source_column": "Code_Old", "target_column": "adm1_pcode"},
        ],
    )
    with pytest.raises(ValueError, match="not decided in the crosswalk"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_crosswalk_extra_column_raises(apply_input, tmp_path):
    """A crosswalk referencing a column absent from the file: stale or wrong file."""
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name"},
            {"source_column": "Code_Old", "target_column": "adm1_pcode"},
            {"source_column": "Extra", "target_column": None},
            {"source_column": "Nonexistent", "target_column": "adm1_notes"},
        ],
    )
    with pytest.raises(ValueError, match="not in the file"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_crosswalk_duplicate_source_column_raises(apply_input, tmp_path):
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name"},
            {"source_column": "Name_Old", "target_column": "adm1_name2"},
            {"source_column": "Code_Old", "target_column": "adm1_pcode"},
            {"source_column": "Extra", "target_column": None},
        ],
    )
    with pytest.raises(ValueError, match="same source_column more than once"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_crosswalk_duplicate_target_raises(apply_input, tmp_path):
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name"},
            {"source_column": "Code_Old", "target_column": "adm1_name"},
            {"source_column": "Extra", "target_column": None},
        ],
    )
    with pytest.raises(ValueError, match="used more than once"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_crosswalk_reserved_target_raises(apply_input, tmp_path):
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "geometry"},
            {"source_column": "Code_Old", "target_column": "adm1_pcode"},
            {"source_column": "Extra", "target_column": None},
        ],
    )
    with pytest.raises(ValueError, match="reserved names"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_crosswalk_missing_source_column_header_raises(apply_input, tmp_path):
    crosswalk = tmp_path / "crosswalk.csv"
    crosswalk.write_text("target_column\nadm1_name\n")
    with pytest.raises(ValueError, match="must be a CSV with a source_column column"):
        apply_crosswalk(
            apply_input, crosswalk, tmp_path / "out.parquet", overwrite=True
        )


def test_blank_source_column_row_is_skipped(apply_input, tmp_path):
    """A gap-row placeholder from map (blank source_column) is inert, not an error."""
    crosswalk = tmp_path / "crosswalk.csv"
    crosswalk.write_text(
        "source_column,target_column\n"
        ",adm1_name\n"
        "Name_Old,adm1_name\n"
        "Code_Old,adm1_pcode\n"
        "Extra,\n"
    )
    out = tmp_path / "out.parquet"
    apply_crosswalk(apply_input, crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert columns == {"adm1_name", "adm1_pcode", "geometry"}


def test_noise_columns_excluded_from_coverage_check(tmp_path):
    """OBJECTID/Shape_Length/etc. don't need a crosswalk entry to pass validation."""
    path = tmp_path / "noisy_input.parquet"
    _write_table(
        path,
        ["geom", "Name_Old", "OBJECTID", "Shape_Length", "Shape_Area"],
        [
            (_unit_square(0), "Alpha", "1", "2.5", "3.5"),
            (_unit_square(1), "Beta", "2", "2.5", "3.5"),
        ],
    )
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [{"source_column": "Name_Old", "target_column": "adm1_name"}],
    )
    out = tmp_path / "out.parquet"
    apply_crosswalk(path, crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert columns == {"adm1_name", "geometry"}


def test_target_column_with_quote_round_trips(apply_input, tmp_path):
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": 'adm1"name'},
            {"source_column": "Code_Old", "target_column": "adm1_pcode"},
            {"source_column": "Extra", "target_column": None},
        ],
    )
    out = tmp_path / "mapped.parquet"
    apply_crosswalk(apply_input, crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert 'adm1"name' in columns


def test_debug_step_apply_exports_crosswalk_table(
    apply_input, full_crosswalk, tmp_path
):
    work_dir = tmp_path / "work"
    for step in ("inputs", "apply"):
        apply_crosswalk(
            apply_input,
            full_crosswalk,
            tmp_path / "out.parquet",
            tmp_dir=work_dir,
            step=step,
            debug=True,
            overwrite=True,
        )
    exported = {p.name for p in work_dir.glob("*.parquet")}
    assert any(name.endswith("_crosswalk.parquet") for name in exported)


def test_cli_error_on_existing_output(apply_input, full_crosswalk, tmp_path):
    out = tmp_path / "exists.parquet"
    out.touch()
    result = CliRunner().invoke(
        cli,
        [
            "schema-map",
            str(apply_input),
            str(out),
            "--csv",
            str(full_crosswalk),
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert "output already exists" in result.output


def test_refactor_steps(apply_input, full_crosswalk, tmp_path):
    out = tmp_path / "steps_mapped.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        apply_crosswalk(
            apply_input,
            full_crosswalk,
            out,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert out.exists()


def test_columns_follow_crosswalk_rows_and_rows_sort_by_code(tmp_path):
    src = tmp_path / "admin2.parquet"
    _write_table(
        src,
        ["geom", "N2", "C2", "N2b", "N1", "C1"],
        [
            (_unit_square(0), "Alpha", "XY02001", "Alt Alpha", "Two", "XY02"),
            (_unit_square(1), "Bravo", "XY01002", "Alt Bravo", "One", "XY01"),
            (_unit_square(2), "Charlie", "XY01001", "Alt Charlie", "One", "XY01"),
        ],
    )
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "C1", "target_column": "adm1_code"},
            {"source_column": "N2b", "target_column": "adm2_name"},
            {"source_column": "C2", "target_column": "adm2_code"},
            {"source_column": "N1", "target_column": "adm1_name"},
            {"source_column": "N2", "target_column": "adm2_name1"},
        ],
    )
    out = tmp_path / "out.parquet"
    apply_crosswalk(src, crosswalk, out)

    with duckdb.connect() as conn:
        result = conn.execute(f"SELECT * EXCLUDE (geometry) FROM '{out}'")
        columns = [d[0] for d in result.description]
        rows = result.fetchall()
        first = conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchone()[0]
    assert first == "geometry"
    assert columns == ["adm1_code", "adm2_name", "adm2_code", "adm1_name", "adm2_name1"]
    assert [r[2] for r in rows] == ["XY01001", "XY01002", "XY02001"]


def test_name_field_requires_code_field(apply_input, full_crosswalk, tmp_path):
    with pytest.raises(ValueError, match="given together"):
        apply_crosswalk(
            apply_input, full_crosswalk, tmp_path / "out.parquet", name_field="n{n}"
        )


def test_warns_when_no_code_template_column(
    apply_input, full_crosswalk, tmp_path, caplog
):
    apply_crosswalk(apply_input, full_crosswalk, tmp_path / "out.parquet")
    assert "no 'adm{n}_code' target column" in caplog.text


def test_warns_when_numbered_sibling_precedes_its_base(apply_input, tmp_path, caplog):
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name1"},
            {"source_column": "Code_Old", "target_column": "adm1_name"},
            {"source_column": "Extra", "target_column": None},
        ],
    )
    out = tmp_path / "out.parquet"
    apply_crosswalk(apply_input, crosswalk, out)

    with duckdb.connect() as conn:
        columns = [d[0] for d in conn.execute(f"SELECT * FROM '{out}'").description]
    assert columns == ["geometry", "adm1_name1", "adm1_name"]
    assert "numbered siblings of ['adm1_name'] are out of order" in caplog.text


def test_noise_columns_listed_in_crosswalk_accepted(tmp_path):
    """A crosswalk naming OBJECTID/GlobalID (as schema-map writes it) still applies."""
    path = tmp_path / "noisy_input.parquet"
    _write_table(
        path,
        ["geom", "Name_Old", "OBJECTID", "GlobalID"],
        [
            (_unit_square(0), "Alpha", "1", "a"),
            (_unit_square(1), "Beta", "2", "b"),
        ],
    )
    crosswalk = _write_crosswalk(
        tmp_path / "crosswalk.csv",
        [
            {"source_column": "Name_Old", "target_column": "adm1_name"},
            {"source_column": "OBJECTID", "target_column": None},
            {"source_column": "GlobalID", "target_column": "global_id"},
        ],
    )
    out = tmp_path / "out.parquet"
    apply_crosswalk(path, crosswalk, out, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert columns == {"adm1_name", "global_id", "geometry"}
