"""Portability smoke tests for the standalone detect() tool.

Same fixtures as tests/test_topo_clean.py, since topo_clean composes
core.topo_detect's issue-detection stage directly.
"""

import duckdb
import pytest
from click.testing import CliRunner

import topo_tools.core.topo_detect._02_issues as issues_stage
from topo_tools.api.topo_detect import detect
from topo_tools.cli.main import cli

# Same layout as test_clean.py's _SYNTHETIC_WKT: fid 1-4 is a compact-hole
# donut, fid 5-6 a partial overlap, fid 7-8 a full-containment overlap.
_SYNTHETIC_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 1, 2 1, 1 1, 0 1, 0 0))"),
    (2, "POLYGON((0 2, 1 2, 2 2, 3 2, 3 3, 0 3, 0 2))"),
    (3, "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))"),
    (4, "POLYGON((2 1, 3 1, 3 2, 2 2, 2 1))"),
    (5, "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    (6, "POLYGON((10.95 0, 12 0, 12 1, 10.95 1, 10.95 0))"),
    (7, "POLYGON((30 0, 32 0, 32 2, 30 2, 30 0))"),
    (8, "POLYGON((30.5 0.5, 31.5 0.5, 31.5 1.5, 30.5 1.5, 30.5 0.5))"),
]

# Units sharing y=0 up to x=0, then A's edge leaves B's at a 5 m mouth over 600 m.
_A, _B, _MOUTH = 600 / 111320, 1000 / 111320, 5 / 111320
_WEDGE_WKT = [
    (1, f"POLYGON((-.02 0, 0 0, {_A} {_MOUTH}, {_A} .02, -.02 .02, -.02 0))"),
    (2, f"POLYGON((-.02 0, 0 0, {_B} 0, {_B} -.02, -.02 -.02, -.02 0))"),
]

_STEPS = ["inputs", "issues", "outputs"]


def _write_wkt(path, wkt):
    values = ", ".join(f"({fid}, ST_GeomFromText('{w}'))" for fid, w in wkt)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def synthetic_input(tmp_path):
    path = tmp_path / "synthetic.parquet"
    _write_wkt(path, _SYNTHETIC_WKT)
    return path


@pytest.fixture
def no_defects_input(tmp_path):
    """Two squares sharing only a boundary edge: no gap, no overlap."""
    path = tmp_path / "no_defects.parquet"
    _write_wkt(
        path,
        [
            (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
            (2, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
        ],
    )
    return path


@pytest.fixture
def notch_input(tmp_path):
    path = tmp_path / "notch.parquet"
    _write_wkt(path, _WEDGE_WKT)
    return path


def test_detect_reports_notch(notch_input, tmp_path):
    issues_path = tmp_path / "issues.parquet"
    detect(notch_input, issues_path, overwrite=True)
    with duckdb.connect() as conn:
        rows = conn.execute(
            f"SELECT kind, near_length_m FROM '{issues_path}'"
        ).fetchall()
    assert [kind for kind, _ in rows] == ["notch"]
    assert rows[0][1] > 0


@pytest.mark.parametrize(("dy", "kind"), [(0.0005, "gap"), (-0.0005, "overlap")])
def test_detect_skips_notch_on_reported_gap_or_overlap(tmp_path, dy, kind):
    """A thin lens between two units is one gap or overlap, not also notches."""
    input_path, issues_path = tmp_path / "lens.parquet", tmp_path / "issues.parquet"
    _write_wkt(
        input_path,
        [
            (1, f"POLYGON((0 0, 0.2 0, 0.5 {dy}, 0.8 0, 1 0, 1 1, 0 1, 0 0))"),
            (2, "POLYGON((0 0, 0 -1, 1 -1, 1 0, 0.8 0, 0.2 0, 0 0))"),
        ],
    )
    detect(input_path, issues_path, overwrite=True)
    with duckdb.connect() as conn:
        kinds = conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
    assert kinds == [(kind,)]


def test_cli_help():
    result = CliRunner().invoke(cli, ["topo-detect", "--help"])
    assert result.exit_code == 0
    assert "Find gaps and overlaps between the polygons" in (result.output)
    assert "Examples:" in result.output


def test_detect_full_run(synthetic_input, tmp_path):
    issues_path = tmp_path / "issues.parquet"
    detect(synthetic_input, issues_path, overwrite=True)

    assert issues_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0]
            for r in conn.execute(f"DESCRIBE SELECT * FROM '{issues_path}'").fetchall()
        }
        kinds = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT kind FROM '{issues_path}'"
            ).fetchall()
        }
    assert kinds == {"gap", "overlap"}
    # No outcome columns: nothing was fixed, only clean's own issues output
    # gets those.
    assert "fixed" not in columns
    assert "filled_area_m2" not in columns


def test_detect_full_containment_overlap(synthetic_input, tmp_path):
    """A fully-nested duplicate polygon (id 8 inside id 7) is an overlap.

    ST_Overlaps alone is false for full containment (OGC definition), so
    this is only caught via the join's ST_Contains half.
    """
    issues_path = tmp_path / "issues.parquet"
    detect(synthetic_input, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(f"""
            SELECT ST_Area(geometry) FROM '{issues_path}'
            WHERE kind = 'overlap'
              AND ST_Within(geometry, ST_MakeEnvelope(30, 0, 32, 2))
        """).fetchall()
    # Full containment: the overlap area equals fid 8's entire 1x1 extent.
    assert area == [(1.0,)]


def test_detect_default_output_path(synthetic_input):
    detect(synthetic_input, overwrite=True)

    expected_issues = synthetic_input.with_stem(synthetic_input.stem + "_issues")
    assert expected_issues.exists()


def test_detect_no_defects(no_defects_input, tmp_path):
    issues_path = tmp_path / "issues.parquet"
    detect(no_defects_input, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        count = conn.execute(f"SELECT COUNT(*) FROM '{issues_path}'").fetchone()[0]
    assert count == 0


def test_detect_gap_detection_failure_falls_back_to_empty(
    monkeypatch, synthetic_input, tmp_path, caplog
):
    def always_fails(*_args, **_kwargs):
        msg = "simulated GEOS failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(issues_stage, "_build_gaps", always_fails)

    issues_path = tmp_path / "issues.parquet"
    with caplog.at_level("WARNING"):
        detect(synthetic_input, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        kinds = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT kind FROM '{issues_path}'"
            ).fetchall()
        }
    assert kinds == {"overlap"}
    assert any("gap detection failed" in r.message for r in caplog.records)


def test_detect_overlap_detection_failure_falls_back_to_empty(
    monkeypatch, synthetic_input, tmp_path, caplog
):
    def always_fails(*_args, **_kwargs):
        msg = "simulated GEOS failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(issues_stage, "_build_overlaps", always_fails)

    issues_path = tmp_path / "issues.parquet"
    with caplog.at_level("WARNING"):
        detect(synthetic_input, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        kinds = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT kind FROM '{issues_path}'"
            ).fetchall()
        }
    assert kinds == {"gap"}
    assert any("overlap detection failed" in r.message for r in caplog.records)


def test_detect_out_of_memory_raises(monkeypatch, synthetic_input, tmp_path):
    def out_of_memory(*_args, **_kwargs):
        msg = "simulated out of memory"
        raise duckdb.OutOfMemoryException(msg)

    monkeypatch.setattr(issues_stage, "_build_overlaps", out_of_memory)

    with pytest.raises(duckdb.OutOfMemoryException):
        detect(synthetic_input, tmp_path / "issues.parquet", overwrite=True)


def test_cli_positional_args(synthetic_input, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli, ["topo-detect", str(synthetic_input), str(output_path)]
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_detect_error_on_existing_output(synthetic_input, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        ["topo-detect", str(synthetic_input), str(output_path), "--overwrite=false"],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_detect_steps(synthetic_input, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    issues_path = tmp_path / "steps_issues.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        detect(
            synthetic_input,
            issues_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert issues_path.exists()
