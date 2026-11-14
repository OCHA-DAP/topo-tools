"""Tests for schema-detect: a clean layer, then one mutation per kind."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.schema_detect import detect
from topo_tools.cli.main import cli
from topo_tools.core.schema_detect._03_checks import _CHECKS

_FIELDS = {"name_field": "adm{n}_name", "code_field": "adm{n}_pcode"}


def _base_sql() -> str:
    """Three levels under one constant root: 3 x 3 x 3 units, two name columns."""
    return """--sql
        SELECT 'XY' AS adm0_pcode, 'Country' AS adm0_name, 'Pays' AS adm0_name1,
               'XY' || a AS adm1_pcode, 'Region ' || x AS adm1_name,
               'Région ' || x AS adm1_name1,
               'XY' || a || b AS adm2_pcode, 'District ' || x || y AS adm2_name,
               'District FR ' || x || y AS adm2_name1,
               'XY' || a || b || c AS adm3_pcode,
               'Commune ' || x || y || z AS adm3_name,
               'Commune FR ' || x || y || z AS adm3_name1,
               ST_Point(a * 10 + b, c) AS geom
        FROM (
            SELECT a, b, c, chr(64 + a::INTEGER) AS x, chr(64 + b::INTEGER) AS y,
                   chr(64 + c::INTEGER) AS z
            FROM range(1, 4) r1(a), range(1, 4) r2(b), range(1, 4) r3(c)
        )
    """


def _write(path, select: str = "SELECT * FROM base"):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"CREATE TABLE base AS {_base_sql()}")
        conn.execute(f"COPY ({select}) TO '{path}'")
    return path


def _read(path):
    rel = duckdb.sql(f"SELECT * FROM read_csv('{path}')")
    return [dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()]


def _kinds(tmp_path, select, **fields):
    out = tmp_path / "issues.csv"
    detect(_write(tmp_path / "in.parquet", select), out, **fields)
    return {(r["kind"], r["severity"], r["level"], r["column"]) for r in _read(out)}


def test_cli_help():
    result = CliRunner().invoke(cli, ["schema-detect", "--help"])
    assert result.exit_code == 0
    assert "Find problems in the column schema" in result.output


@pytest.mark.parametrize("fields", [_FIELDS, {}], ids=["explicit", "structural"])
def test_clean_layer_writes_empty_report(tmp_path, fields):
    out = tmp_path / "issues.csv"
    detect(_write(tmp_path / "in.parquet"), out, **fields)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _read(out) == []


@pytest.mark.parametrize("fields", [_FIELDS, {}], ids=["explicit", "structural"])
def test_column_naming(tmp_path, fields):
    select = "SELECT * RENAME (adm2_name1 AS ADM2_NAME1) FROM base"
    assert _kinds(tmp_path, select, **fields) == {
        ("column-naming", "warn", 2, "ADM2_NAME1")
    }


def test_column_set_mismatch(tmp_path):
    select = "SELECT * EXCLUDE (adm2_name1) FROM base"
    assert _kinds(tmp_path, select, **_FIELDS) == {
        ("column-set-mismatch", "warn", 2, None)
    }


def test_family_at_one_level_is_not_a_mismatch(tmp_path):
    select = "SELECT *, adm3_name AS adm3_ref_name FROM base"
    assert _kinds(tmp_path, select, **_FIELDS) == set()


def test_level_skipped(tmp_path):
    select = "SELECT * EXCLUDE (adm2_pcode, adm2_name, adm2_name1) FROM base"
    assert ("level-skipped", "error", 2, None) in _kinds(tmp_path, select, **_FIELDS)


def test_multiple_parents(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY111' THEN 'XY2'
                               ELSE adm1_pcode END AS adm1_pcode)
        FROM base
    """
    assert _kinds(tmp_path, select, **_FIELDS) == {
        ("multiple-parents", "error", 2, "adm2_pcode")
    }


def test_orphan_child(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm2_pcode = 'XY11' THEN NULL
                               ELSE adm1_pcode END AS adm1_pcode)
        FROM base
    """
    assert _kinds(tmp_path, select, **_FIELDS) == {
        ("orphan-child", "error", 2, "adm2_pcode")
    }


@pytest.mark.parametrize("fields", [_FIELDS, {}], ids=["explicit", "structural"])
def test_varying_column(tmp_path, fields):
    """Text varying within a coarsest unit is an error; constant or numeric is not."""
    select = """--sql
        SELECT *, 'en' AS lang, adm3_pcode AS valid_on, hash(adm3_pcode) AS n
        FROM base
    """
    assert _kinds(tmp_path, select, **fields) == {
        ("varying-column", "error", 0, "valid_on")
    }


def test_varying_column_skips_blank_coarsest_code(tmp_path):
    """Rows with no coarsest code are not one unit; odd names stay quoted."""
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm2_pcode = 'XY11' THEN NULL
                               ELSE adm0_pcode END AS adm0_pcode),
               CASE WHEN adm2_pcode = 'XY11' THEN adm3_pcode ELSE 'x' END
                   AS "it's 100%"
        FROM base
    """
    kinds = _kinds(tmp_path, select, **_FIELDS)
    assert not [k for k in kinds if k[0] in {"varying-column", "check-failed"}]


def test_varying_column_single_level(tmp_path):
    select = """--sql
        SELECT adm2_pcode AS adm1_pcode, adm2_name AS adm1_name,
               adm3_pcode AS note, geom
        FROM base
    """
    assert _kinds(tmp_path, select, **_FIELDS) == {
        ("varying-column", "error", 1, "note")
    }


def test_levels_undetected_is_reported_not_raised(tmp_path):
    fields = {"name_field": "lvl{n}_name", "code_field": "lvl{n}_code"}
    assert _kinds(tmp_path, "SELECT * FROM base", **fields) == {
        ("levels-undetected", "error", None, None)
    }


def test_grouping_column_is_a_warning_not_a_refusal(tmp_path):
    select = (
        "SELECT *, CASE WHEN adm1_pcode = 'XY3' THEN 'S' ELSE 'N' END AS zone FROM base"
    )
    assert _kinds(tmp_path, select) == {("supplemental-column", "warn", None, "zone")}


@pytest.mark.parametrize("fields", [_FIELDS, {}], ids=["explicit", "structural"])
def test_nameless_layer_reports_levels_undetected(tmp_path, fields):
    select = "SELECT COLUMNS('pcode'), geom FROM base"
    out = tmp_path / "issues.csv"
    detect(_write(tmp_path / "in.parquet", select), out, **fields)
    (row,) = [r for r in _read(out) if r["kind"] == "levels-undetected"]
    assert "no admin level with both a code and a name" in row["reason"]


def test_parquet_report(tmp_path):
    out = tmp_path / "issues.parquet"
    detect(_write(tmp_path / "in.parquet"), out, **_FIELDS)
    assert duckdb.sql(f"SELECT count(*) FROM '{out}'").fetchall() == [(0,)]


def test_default_issues_path(tmp_path):
    path = _write(tmp_path / "layer.parquet")
    detect(path, **_FIELDS)
    assert (tmp_path / "layer_schema_issues.parquet").exists()


def test_bad_issues_suffix_raises(tmp_path):
    with pytest.raises(ValueError, match="issues_path must end in"):
        detect(_write(tmp_path / "in.parquet"), tmp_path / "issues.gpkg", **_FIELDS)


def test_failing_check_reported_others_run(tmp_path, monkeypatch):
    def broken(_name, _parents):
        return "SELECT * FROM missing_table"

    monkeypatch.setitem(_CHECKS, "column-naming", broken)
    select = "SELECT * EXCLUDE (adm2_name1) FROM base"
    assert _kinds(tmp_path, select, **_FIELDS) == {
        ("check-failed", "error", None, None),
        ("column-set-mismatch", "warn", 2, None),
    }


def test_steps(tmp_path):
    select = "SELECT * EXCLUDE (adm2_name1) FROM base"
    path = _write(tmp_path / "in.parquet", select)
    out = tmp_path / "issues.csv"
    for step in ["inputs", "levels", "checks", "outputs"]:
        detect(path, out, **_FIELDS, tmp_dir=tmp_path / "work", step=step)
    assert {r["kind"] for r in _read(out)} == {"column-set-mismatch"}


def test_unusable_column_name_explicit(tmp_path):
    """A bare level-numbered family leaves packaging no name for its output column."""
    select = """--sql
        SELECT *, adm0_pcode AS adm0, adm1_pcode AS adm1, adm2_pcode AS adm2,
               adm3_pcode AS adm3
        FROM base
    """
    assert ("unusable-column-name", "error", None, None) in _kinds(
        tmp_path, select, **_FIELDS
    )


def test_unusable_column_name_structural(tmp_path):
    """Two auto-detected families reducing to one output name are reported."""
    select = """--sql
        SELECT *, adm0_name AS adm0name, adm1_name AS adm1name,
               adm2_name AS adm2name, adm3_name AS adm3name
        FROM base
    """
    assert ("unusable-column-name", "error", None, None) in _kinds(tmp_path, select)
