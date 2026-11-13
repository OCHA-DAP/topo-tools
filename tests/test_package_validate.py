"""Tests for package's clean and validation: reports, summary, blocking, --no-clean."""

import sys

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.package import package
from topo_tools.cli.main import cli
from topo_tools.core.code_detect._03_checks import _CHECKS as _CODE_CHECKS

_FIELDS = {"name_field": "adm{n}_name", "code_field": "adm{n}_pcode"}


def _write(path, select: str = "SELECT * FROM base"):
    """Three levels: 3 regions, 9 districts, 27 communes, as adjacent squares."""
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute("""--sql
            CREATE TABLE base AS
            SELECT 'XY' || a AS adm1_pcode, 'Region ' || a AS adm1_name,
                   'XY' || a || b AS adm2_pcode, 'District ' || a || b AS adm2_name,
                   'XY' || a || b || c AS adm3_pcode,
                   'Commune ' || a || b || c AS adm3_name,
                   ST_MakeEnvelope((a - 1) * 3 + b - 1, c - 1, (a - 1) * 3 + b, c)
                       AS geom
            FROM range(1, 4) r1(a), range(1, 4) r2(b), range(1, 4) r3(c)
        """)
        conn.execute(f"COPY ({select}) TO '{path}'")
    return path


def _summary(path):
    rel = duckdb.sql(f"SELECT * FROM read_csv('{path}', all_varchar = true)")
    return [dict(zip(rel.columns, row, strict=True)) for row in rel.fetchall()]


_DUPLICATE = """--sql
    SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY112' THEN 'XY111'
                           ELSE adm3_pcode END AS adm3_pcode)
    FROM base
"""
_OVERLAP = """--sql
    SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY111'
                           THEN ST_Translate(geom, 0.1, 0) ELSE geom END AS geom)
    FROM base
"""
_OUTLIER = """--sql
    SELECT * REPLACE (CASE WHEN adm3_pcode = 'XY111' THEN 'XY11-1'
                           ELSE adm3_pcode END AS adm3_pcode)
    FROM base
"""


def _out(tmp_path):
    return str(tmp_path / "out" / "{x}.parquet")


def test_clean_layer(tmp_path):
    package(_write(tmp_path / "in.parquet"), _out(tmp_path))
    out = tmp_path / "out"
    for suffix in [
        "schema_issues.parquet",
        "code_issues.parquet",
        "name_issues.parquet",
    ]:
        assert (out / f"in_{suffix}").exists()
    assert (out / "admin1.parquet").exists()
    summary = out / "in_validate_summary.csv"
    assert summary.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _summary(summary)
    assert [r["stage"] for r in rows] == ["schema", "topo", "code", "name"]
    assert {r["reason"] for r in rows} == {"no issues"}


def test_reports_default_beside_input(tmp_path):
    package(_write(tmp_path / "layer.parquet"))
    assert (tmp_path / "layer_validate_summary.csv").exists()
    assert (tmp_path / "layer_code_issues.parquet").exists()
    assert (tmp_path / "layer_admin1.parquet").exists()


def test_error_blocks_outputs(tmp_path):
    with pytest.raises(ValueError, match="validation found errors"):
        package(_write(tmp_path / "in.parquet", _DUPLICATE), _out(tmp_path), **_FIELDS)
    out = tmp_path / "out"
    rows = _summary(out / "in_validate_summary.csv")
    assert {
        "stage": "code",
        "kind": "duplicate-code",
        "severity": "error",
        "count": "1",
        "report": str(out / "in_code_issues.parquet"),
        "reason": None,
    } in rows
    assert not (out / "admin1.parquet").exists()


def test_no_block_reports_and_packages(tmp_path):
    package(
        _write(tmp_path / "in.parquet", _DUPLICATE),
        _out(tmp_path),
        block=False,
        **_FIELDS,
    )
    out = tmp_path / "out"
    assert (out / "admin1.parquet").exists()
    rows = _summary(out / "in_validate_summary.csv")
    assert any(r["severity"] == "error" for r in rows)


def test_undetected_levels_skip_code_and_name(tmp_path):
    select = "SELECT adm3_name AS label, geom FROM base"
    with pytest.raises(ValueError, match="validation found errors"):
        package(_write(tmp_path / "in.parquet", select), _out(tmp_path))
    rows = {
        (r["stage"], r["kind"], r["severity"])
        for r in _summary(tmp_path / "out" / "in_validate_summary.csv")
    }
    assert ("schema", "levels-undetected", "error") in rows
    assert {("code", "skipped", "warn"), ("name", "skipped", "warn")} <= rows
    assert not (tmp_path / "out" / "in_code_issues.parquet").exists()


def test_grouping_column_still_runs_code_and_name(tmp_path):
    select = (
        "SELECT *, CASE WHEN adm1_pcode = 'XY3' THEN 'S' ELSE 'N' END AS zone FROM base"
    )
    package(_write(tmp_path / "in.parquet", select), _out(tmp_path))
    rows = {
        (r["stage"], r["kind"])
        for r in _summary(tmp_path / "out" / "in_validate_summary.csv")
    }
    assert ("schema", "supplemental-column") in rows
    assert {("code", None), ("name", None)} <= rows


def test_failing_stage_recorded_others_run(tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(sys.modules["topo_tools.api._validate"], "topo_detect", broken)
    with pytest.raises(ValueError, match="validation found errors"):
        package(_write(tmp_path / "in.parquet"), _out(tmp_path))
    rows = _summary(tmp_path / "out" / "in_validate_summary.csv")
    topo = [r for r in rows if r["stage"] == "topo"]
    assert topo == [
        {
            "stage": "topo",
            "kind": "failed",
            "severity": "error",
            "count": None,
            "report": None,
            "reason": "boom",
        }
    ]
    assert {r["stage"] for r in rows} == {"schema", "topo", "code", "name"}


def test_failing_check_is_an_error(tmp_path, monkeypatch):
    def broken(_source):
        return "SELECT * FROM missing_table"

    monkeypatch.setitem(_CODE_CHECKS, "format-outlier", broken)
    with pytest.raises(ValueError, match="validation found errors"):
        package(_write(tmp_path / "in.parquet"), _out(tmp_path), **_FIELDS)
    rows = _summary(tmp_path / "out" / "in_validate_summary.csv")
    code = [
        (r["kind"], r["severity"], r["count"]) for r in rows if r["stage"] == "code"
    ]
    assert code == [("check-failed", "error", "1")]


def test_existing_report_without_overwrite_raises(tmp_path):
    path = _write(tmp_path / "in.parquet")
    (tmp_path / "in_name_issues.parquet").touch()
    with pytest.raises(FileExistsError):
        package(path, overwrite=False)
    assert not (tmp_path / "in_schema_issues.parquet").exists()


def test_overlap_cleaned_before_packaging(tmp_path):
    work = tmp_path / "work"
    package(_write(tmp_path / "in.parquet", _OVERLAP), _out(tmp_path), tmp_dir=work)
    assert (tmp_path / "out" / "admin1.parquet").exists()
    assert not list(work.rglob("*.parquet"))


def test_no_clean_reports_invalid_edges(tmp_path):
    package(_write(tmp_path / "in.parquet", _OVERLAP), _out(tmp_path), clean=False)
    out = tmp_path / "out"
    rows = {(r["stage"], r["kind"]) for r in _summary(out / "in_validate_summary.csv")}
    assert ("topo", "skipped") in rows
    kinds = {
        r[0]
        for r in duckdb.sql(
            f"SELECT DISTINCT kind FROM '{out / 'admin3_issues.parquet'}'"
        ).fetchall()
    }
    assert "invalid-edge" in kinds


@pytest.mark.parametrize(
    ("select", "exit_code"),
    [("SELECT * FROM base", 0), (_OUTLIER, 0), (_DUPLICATE, 1)],
    ids=["clean", "warnings", "errors"],
)
def test_cli_exit_code(tmp_path, select, exit_code):
    path = _write(tmp_path / "in.parquet", select)
    fields = ["--name-field", "adm{n}_name", "--code-field", "adm{n}_pcode"]
    result = CliRunner().invoke(
        cli, ["package", str(path), "--output", _out(tmp_path), *fields]
    )
    assert result.exit_code == exit_code


def test_clean_out_of_memory_points_to_no_clean(tmp_path, monkeypatch):
    def out_of_memory(*_args, **_kwargs):
        msg = "simulated out of memory"
        raise duckdb.OutOfMemoryException(msg)

    module = sys.modules["topo_tools.api.package"]
    monkeypatch.setattr(module, "read_reproject_and_clean", out_of_memory)
    with pytest.raises(duckdb.OutOfMemoryException, match="--no-clean"):
        package(_write(tmp_path / "in.parquet"), _out(tmp_path), **_FIELDS)
