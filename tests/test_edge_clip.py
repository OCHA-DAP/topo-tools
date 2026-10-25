"""Portability smoke tests: does clip() run to completion on this machine.

Not a correctness suite: outputs.main already raises RuntimeError on an
empty result, so a clean run is already vetted by the pipeline itself.
"""

import math

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.edge_clip import clip
from topo_tools.cli.main import cli
from topo_tools.core.constants import SUBDIVIDE_MIN_VERTICES

_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))"),
    (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))"),
]

# Overshoots overlay A's extent only, no overlap with overlay B.
_INPUT_ROWS = [(1, "POLYGON((-5 -5, 5 -5, 5 5, -5 5, -5 -5))")]

_OVERLAY_A_AREA = 9.0

_STEPS = ["inputs", "assign", "clip", "outputs"]


def _write_overlays(path, wkt_rows):
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in wkt_rows)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


def _write_inputs(path, wkt_rows):
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in wkt_rows)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def synthetic_overlays(tmp_path):
    path = tmp_path / "parents.parquet"
    _write_overlays(path, _OVERLAY_WKT)
    return path


@pytest.fixture
def synthetic_inputs(tmp_path):
    path = tmp_path / "children.parquet"
    _write_inputs(path, _INPUT_ROWS)
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["edge-clip", "--help"])
    assert result.exit_code == 0
    assert "overlay polygon it overlaps most" in result.output
    assert "Examples:" in result.output


def test_clip_bounds_output_to_overlay(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    clip(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(f"""--sql
            SELECT ST_Area(geometry) FROM '{output_path}' WHERE id = 1
        """).fetchone()[0]
    assert area == pytest.approx(_OVERLAY_A_AREA, abs=1e-6)


def test_clip_crs_less_input_against_tagged_overlay(synthetic_overlays, tmp_path):
    input_path = tmp_path / "children.parquet"
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        conn.execute(f"""
            COPY (SELECT 1 AS id, ST_GeomFromText('{_INPUT_ROWS[0][1]}') AS geom)
            TO '{input_path}' (FORMAT PARQUET, GEOPARQUET_VERSION 'NONE')
        """)
    output_path = tmp_path / "out.parquet"
    clip(input_path, synthetic_overlays, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        geom_type, area = conn.execute(
            f"SELECT typeof(geometry), ST_Area(geometry) FROM '{output_path}'"
        ).fetchone()
    assert geom_type == "GEOMETRY('OGC:CRS84')"
    assert area == pytest.approx(_OVERLAY_A_AREA, abs=1e-6)


def test_clip_majority_vote_drops_outlier(synthetic_overlays, tmp_path):
    """Two input features overshoot overlay A, one overshoots overlay B.

    A wins the file's majority vote; the dissenting input is dropped, not misassigned.
    """
    input_path = tmp_path / "children.parquet"
    _write_inputs(
        input_path,
        [
            (1, "POLYGON((-5 -5, 5 -5, 5 5, -5 5, -5 -5))"),
            (2, "POLYGON((-2 -2, 4 -2, 4 4, -2 4, -2 -2))"),
            (3, "POLYGON((8 -5, 20 -5, 20 20, 8 20, 8 -5))"),
        ],
    )

    output_path = tmp_path / "out.parquet"
    clip(input_path, synthetic_overlays, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(f"""--sql
            SELECT id, ST_Area(geometry) FROM '{output_path}' ORDER BY id
        """).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert all(area == pytest.approx(_OVERLAY_A_AREA, abs=1e-6) for _, area in rows)


def _circle_wkt(cx, cy, r, n):
    pts = [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    pts.append(pts[0])
    coords = ", ".join(f"{x} {y}" for x, y in pts)
    return f"POLYGON(({coords}))"


def test_clip_heavy_overlay_tiling_finds_real_overlap(tmp_path):
    """An overlay part at or above SUBDIVIDE_MIN_VERTICES takes the tiled path.

    Regression test: a heavy tile's bbox columns must survive the join
    that finds its real overlaps.
    """
    n_points = SUBDIVIDE_MIN_VERTICES + 200
    overlays_path = tmp_path / "heavy_parents.parquet"
    _write_overlays(overlays_path, [(1, _circle_wkt(50, 50, 10, n_points))])

    input_path = tmp_path / "heavy_children.parquet"
    _write_inputs(input_path, [(1, "POLYGON((49 49, 51 49, 51 51, 49 51, 49 49))")])

    output_path = tmp_path / "out.parquet"
    clip(input_path, overlays_path, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
    assert count == 1


def test_clip_raises_when_no_input_overlaps_any_overlay(synthetic_overlays, tmp_path):
    input_path = tmp_path / "children.parquet"
    _write_inputs(
        input_path, [(1, "POLYGON((100 100, 101 100, 101 101, 100 101, 100 100))")]
    )

    output_path = tmp_path / "out.parquet"
    with pytest.raises(RuntimeError, match="no input polygon survived clipping"):
        clip(input_path, synthetic_overlays, output_path, overwrite=True)


def test_clip_default_output_path(synthetic_inputs, synthetic_overlays):
    clip(synthetic_inputs, synthetic_overlays, overwrite=True)

    expected = synthetic_inputs.with_stem(synthetic_inputs.stem + "_clipped")
    assert expected.exists()


def test_cli_positional_args(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-clip",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_error_on_existing_output(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        [
            "edge-clip",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert "output already exists" in result.output


def test_clip_steps(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        clip(
            synthetic_inputs,
            synthetic_overlays,
            output_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()


def test_cli_single_file_unchanged(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-clip",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(f"""--sql
            SELECT ST_Area(geometry) FROM '{output_path}' WHERE id = 1
        """).fetchone()[0]
    assert area == pytest.approx(_OVERLAY_A_AREA, abs=1e-6)


def _write_with_code(path, rows):
    """rows: list of (fid, wkt, pcode)."""
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'), '{code}')" for fid, wkt, code in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE synth AS
            SELECT * FROM (VALUES {values}) AS t(id, geom, pcode)
        """)
        conn.execute(f"COPY synth TO '{path}'")


def test_match_overrides_spatial_and_reports_mismatch(tmp_path):
    """An input mostly inside overlay A but coded to overlay B ends up clipped to B."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        # Overlaps overlay A (area 2) far more than overlay B (area 0.5), but
        # its code points to B, which it does overlap, so code wins.
        [(1, "POLYGON((1 0, 10.5 0, 10.5 1, 1 1, 1 0))", "P2")],
    )

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clip(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        match_column="pcode",
        overwrite=True,
    )

    assert output_path.exists()
    assert issues_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(
            f"SELECT ST_Area(geometry) FROM '{output_path}'"
        ).fetchone()[0]
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]
    # Clipped to overlay B (only the input feature's x:10-10.5 sliver survives), not
    # overlay A, where clipping would have kept the much larger x:1-3 slice.
    assert area == pytest.approx(0.5, abs=1e-6)
    assert kinds == ["code-mismatch"]


@pytest.mark.parametrize("with_original", [True, False])
def test_clip_merges_detached_sliver_only_with_original(tmp_path, with_original):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlays(
        overlays_path,
        [
            (
                1,
                (
                    "POLYGON((-0.005 0, 0.01 0, 0.01 0.008, 0 0.008, 0 0.009, "
                    "0.00005 0.009, 0.00005 0.0091, 0 0.0091, 0 0.01, -0.005 0.01, "
                    "-0.005 0))"
                ),
            )
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_inputs(
        input_path,
        [
            (1, "POLYGON((0 0, 0.01 0, 0.01 0.01, 0 0.01, 0 0))"),
            (2, "POLYGON((-0.005 0, 0 0, 0 0.01, -0.005 0.01, -0.005 0))"),
        ],
    )
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clip(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        overwrite=True,
        original_path=input_path if with_original else None,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT id, ST_NumGeometries(geometry), ST_Area(geometry) "
            f"FROM '{output_path}' ORDER BY id"
        ).fetchall()
        issues = conn.execute(
            f"SELECT kind, unit_a, unit_b, fixed FROM '{issues_path}'"
        ).fetchall()
    parts = 1 if with_original else 2
    assert [(i, n) for i, n, _ in rows] == [(1, parts), (2, 1)]
    if with_original:
        assert rows[1][2] == pytest.approx(50.005e-6, abs=1e-15)
    assert issues == [("detached-part", 1, 2, with_original)]


def test_match_falls_back_when_code_unmatched(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")],
    )
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "NOPE")],
    )

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clip(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        match_column="pcode",
        overwrite=True,
    )

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]
    assert kinds == ["code-fallback"]


def test_match_mutually_exclusive_with_pair(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="mutually exclusive"):
        clip(
            synthetic_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            match_column="pcode",
            overlay_match_column="pcode",
        )


def test_cli_match_help():
    result = CliRunner().invoke(cli, ["edge-clip", "--help"])
    assert result.exit_code == 0
    assert "--match-column" in result.output
    assert "--overlay-match-column" in result.output
    assert "--input-match-column" in result.output


def test_clip_carry_columns_populates_output(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_inputs(input_path, [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))")])

    output_path = tmp_path / "out.parquet"
    clip(
        input_path,
        overlays_path,
        output_path,
        carry_columns=["pcode"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        pcode = conn.execute(f"SELECT pcode FROM '{output_path}'").fetchone()[0]
    assert pcode == "P1"


def test_cli_carry_column_help():
    result = CliRunner().invoke(cli, ["edge-clip", "--help"])
    assert result.exit_code == 0
    assert "--carry-column" in result.output
