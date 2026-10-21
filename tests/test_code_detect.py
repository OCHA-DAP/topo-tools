"""Tests for code-detect: a clean layer, then one mutation per kind."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.code_detect import detect
from topo_tools.cli.main import cli
from topo_tools.core.code_detect._03_checks import _CHECKS

_FIELDS = {"name_field": "adm{n}_name", "code_field": "adm{n}_pcode"}


def _base_sql() -> str:
    """Three levels: 3 regions, 12 districts, 48 communes, one feature each."""
    return """--sql
        SELECT 'XY' || a AS adm1_pcode, 'Region ' || a AS adm1_name,
               'XY' || a || b AS adm2_pcode, 'District ' || a || b AS adm2_name,
               'XY' || a || b || c AS adm3_pcode,
               'Commune ' || a || b || c AS adm3_name,
               ST_Point(a * 10 + b, c) AS geom
        FROM range(1, 4) r1(a), range(1, 5) r2(b), range(1, 5) r3(c)
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


def _report(tmp_path, select):
    out = tmp_path / "issues.csv"
    detect(_write(tmp_path / "in.parquet", select), out, **_FIELDS)
    return _read(out)


def _kinds(tmp_path, select):
    return {
        (r["kind"], r["severity"], r["level"], r["code_a"])
        for r in _report(tmp_path, select)
    }


def test_cli_help():
    result = CliRunner().invoke(cli, ["code-detect", "--help"])
    assert result.exit_code == 0
    assert "Find problems in the unit codes" in result.output


@pytest.mark.parametrize("fields", [_FIELDS, {}], ids=["explicit", "structural"])
def test_clean_layer_writes_empty_report(tmp_path, fields):
    out = tmp_path / "issues.csv"
    detect(_write(tmp_path / "in.parquet"), out, **fields)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _read(out) == []


def test_blank_code_reported_once_per_parent(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode IN ('XY111', 'XY112') THEN NULL
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    report = _report(tmp_path, select)
    assert [r["kind"] for r in report] == ["blank-code"]
    assert report[0]["reason"].startswith("2 units under XY11 have no code")


def test_name_conflict(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY111' THEN 'Other'
                               ELSE adm2_name END AS adm2_name)
        FROM base
    """
    assert _kinds(tmp_path, select) == {("name-conflict", "error", 2, "XY11")}


def test_duplicate_code(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY112' THEN 'XY111'
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    assert ("duplicate-code", "error", 3, "XY111") in _kinds(tmp_path, select)


def test_split_unit(tmp_path):
    select = (
        "SELECT * FROM base UNION ALL SELECT * FROM base WHERE adm3_pcode = 'XY111'"
    )
    assert _kinds(tmp_path, select) == {("split-unit", "warn", 3, "XY111")}


def test_prefix_mismatch(tmp_path):
    select = """--sql
        SELECT * REPLACE (
            CASE WHEN adm2_pcode = 'XY11' THEN 'XY2' ELSE adm1_pcode END AS adm1_pcode,
            CASE WHEN adm2_pcode = 'XY11' THEN 'Region 2' ELSE adm1_name END
                AS adm1_name
        )
        FROM base
    """
    assert _kinds(tmp_path, select) == {("prefix-mismatch", "error", 2, "XY11")}


def test_format_outlier(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY111' THEN 'XY11-1'
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    assert _kinds(tmp_path, select) == {("format-outlier", "warn", 3, "XY11-1")}


def test_format_undetected(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN right(adm3_pcode, 1) IN ('3', '4')
                               THEN adm2_pcode || '0' || right(adm3_pcode, 1)
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    assert _kinds(tmp_path, select) == {("format-undetected", "warn", 3, None)}


def test_few_codes_skip_format_checks(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm1_pcode = 'XY1' THEN 'XY-1'
                               ELSE adm1_pcode END AS adm1_pcode)
        FROM base
    """
    kinds = {k for k, *_ in _kinds(tmp_path, select)}
    assert not kinds & {"format-outlier", "format-undetected"}


def test_failing_check_reported_others_run(tmp_path, monkeypatch):
    def broken(_source):
        return "SELECT * FROM missing_table"

    monkeypatch.setitem(_CHECKS, "format-outlier", broken)
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY112' THEN 'XY111'
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    kinds = _kinds(tmp_path, select)
    assert ("duplicate-code", "error", 3, "XY111") in kinds
    assert ("check-failed", "error", None, None) in kinds


def test_parquet_report(tmp_path):
    out = tmp_path / "issues.parquet"
    detect(_write(tmp_path / "in.parquet"), out, **_FIELDS)
    assert duckdb.sql(f"SELECT count(*) FROM '{out}'").fetchall() == [(0,)]


def test_parquet_report_has_unit_geometry(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY112' THEN 'XY111'
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    out = tmp_path / "issues.parquet"
    detect(_write(tmp_path / "in.parquet", select), out, **_FIELDS)
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(f"""
            SELECT code_a, ST_NumGeometries(geometry) FROM '{out}'
            WHERE kind = 'duplicate-code'
        """).fetchall()
    assert rows == [("XY111", 2)]


def test_default_issues_path(tmp_path):
    detect(_write(tmp_path / "layer.parquet"), **_FIELDS)
    assert (tmp_path / "layer_code_issues.parquet").exists()


def test_bad_issues_suffix_raises(tmp_path):
    with pytest.raises(ValueError, match="issues_path must end in"):
        detect(_write(tmp_path / "in.parquet"), tmp_path / "issues.gpkg", **_FIELDS)


def test_steps(tmp_path):
    select = """--sql
        SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY112' THEN 'XY111'
                               ELSE adm3_pcode END AS adm3_pcode)
        FROM base
    """
    path = _write(tmp_path / "in.parquet", select)
    out = tmp_path / "issues.csv"
    for step in ["inputs", "levels", "checks", "outputs"]:
        detect(path, out, **_FIELDS, tmp_dir=tmp_path / "work", step=step)
    assert "duplicate-code" in {r["kind"] for r in _read(out)}
