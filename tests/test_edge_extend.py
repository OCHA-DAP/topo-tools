"""Portability smoke tests: does extend() run to completion on this machine.

Not a correctness suite: outputs.main already raises RuntimeError on
coverage violations, so a clean run is already vetted by the pipeline itself.
"""

from decimal import Decimal

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.edge_extend import extend
from topo_tools.cli.main import cli
from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.coverage import detect_notches, has_gaps
from topo_tools.core.edge_extend import _01_inputs as inputs
from topo_tools.core.edge_extend import _02_lines as lines
from topo_tools.core.edge_extend import _03_points as points
from topo_tools.core.edge_extend import _04_voronoi as voronoi
from topo_tools.core.edge_extend import attempt

# fid 1/2 touch exactly (valid coverage); fid 3 sits across a deliberate gap
# for extension to fill; fid 4 is a MULTIPOLYGON (two disjoint parts).
_SYNTHETIC_WKT = [
    (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    (2, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
    (3, "POLYGON((3 0, 4 0, 4 1, 3 1, 3 0))"),
    (
        4,
        (
            "MULTIPOLYGON(((0 3, 0.5 3, 0.5 3.5, 0 3.5, 0 3)), "
            "((1 3, 1.5 3, 1.5 3.5, 1 3.5, 1 3)))"
        ),
    ),
]

_STEPS = ["inputs", "lines", "attempt", "merge", "outputs"]


@pytest.fixture
def synthetic_input(tmp_path):
    """Write a small synthetic GeoParquet, no real-world fixture needed."""
    path = tmp_path / "synthetic.parquet"
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in _SYNTHETIC_WKT
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["edge-extend", "--help"])
    assert result.exit_code == 0
    assert "Extend polygons outward" in result.output
    assert "Examples:" in result.output


def test_cli_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "topo-tools" in result.output


def test_extend_full_run(synthetic_input, tmp_path):
    output_path = tmp_path / "out.parquet"
    extend(synthetic_input, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
    assert row_count == len(_SYNTHETIC_WKT)


def test_extend_default_output_path(synthetic_input):
    extend(synthetic_input, overwrite=True)

    expected = synthetic_input.with_stem(synthetic_input.stem + "_extended")
    assert expected.exists()


def test_cli_positional_args(synthetic_input, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli, ["edge-extend", str(synthetic_input), str(output_path)]
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_clean_error_on_existing_output(synthetic_input, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        ["edge-extend", str(synthetic_input), str(output_path), "--overwrite=false"],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_extend_steps(synthetic_input, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        extend(
            synthetic_input, output_path, tmp_dir=work_dir, step=step, overwrite=True
        )
    assert output_path.exists()


# An isolated point's unbounded Voronoi cell, closed by GEOS's own envelope,
# lands outside valid WGS84 range without _04_voronoi.py's clip.
_ISOLATED_EXCLAVE_POINTS = [
    (0, x, y) for x in range(-70, -10, 10) for y in range(-40, 10, 10)
] + [(1, -68, -85)]

# A degenerate near-collinear cluster where GEOS's ST_VoronoiDiagram drops a
# generator point's cell, reproducing the risk _04_voronoi.py's check guards.
_DEGENERATE_POINTS = [
    (0, -0.95793148, 0.0),
    (1, -0.95793148, -8.5829e-08),
    (2, 0.107843045, 0.0),
    (3, -0.985618322, 0.0),
    (4, 0.107843045, 7.6477e-08),
    (5, -0.985618322, 0.0),
    (6, -0.985618322, -6.1918e-08),
    (7, -0.95793148, -2.914e-08),
    (8, -0.985618322, 0.0),
    (9, -0.95793148, 9.6833e-08),
]


def test_inputs_closes_noise_scale_gap(tmp_path):
    """A sub-SNAP_TOLERANCE gap has no overlaps, so has_gaps() must be checked too."""
    path = tmp_path / "noise_gap.parquet"
    w = SNAP_TOLERANCE / 2
    wkt = [
        (1, f"POLYGON((0 -50, 101 -50, 101 0, {50 + w} 0, 50 0, 0 0, 0 -50))"),
        (
            2,
            (f"POLYGON((0 {w}, 50 {w}, {50 + w} {w}, 101 {w}, 101 51, 0 51, 0 {w}))"),
        ),
        (3, f"POLYGON((0 0, 50 0, 50 {w}, 0 {w}, 0 0))"),
        (
            4,
            (f"POLYGON(({50 + w} 0, 101 0, 101 {w}, {50 + w} {w}, {50 + w} 0))"),
        ),
    ]
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt_}'))" for fid, wkt_ in wkt)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")

    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        inputs.main(conn, "synth", path)
        assert not has_gaps(conn, "synth_01", gap_maximum_width=0)


def test_inputs_closes_deep_notch(tmp_path):
    """A 600 m wedge with a 5 m mouth between two units is closed on input."""
    path = tmp_path / "notch.parquet"
    m = 1 / 111320
    ax, bx = 600 * m, 1000 * m
    a = f"POLYGON((-0.02 0, 0 0, {ax} {5 * m}, {ax} 0.02, -0.02 0.02, -0.02 0))"
    b = f"POLYGON((-0.02 0, 0 0, {bx} 0, {bx} -0.02, -0.02 -0.02, -0.02 0))"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""
            COPY (SELECT 1 AS id, ST_GeomFromText('{a}') AS geom
                  UNION ALL SELECT 2, ST_GeomFromText('{b}')) TO '{path}'
        """)

    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        inputs.main(conn, "synth", path)
        assert detect_notches(conn, "synth_01", "notches") == 0


def test_voronoi_raises_on_incomplete_assignment():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        values = ", ".join(
            f"({fid}, ST_Point({x}, {y}))" for fid, x, y in _DEGENERATE_POINTS
        )
        conn.execute(
            f"CREATE TABLE synth_03b AS SELECT * FROM (VALUES {values}) AS t(fid, geom)"
        )
        with pytest.raises(RuntimeError, match="Voronoi assignment incomplete"):
            voronoi.main(conn, "synth", debug=True)


def test_voronoi_clips_isolated_cell_to_world_bounds():
    lon_min, lon_max, lat_min, lat_max = -180, 180, -90, 90
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        values = ", ".join(
            f"({fid}, ST_Point({x}, {y}))" for fid, x, y in _ISOLATED_EXCLAVE_POINTS
        )
        conn.execute(
            f"CREATE TABLE synth_03b AS SELECT * FROM (VALUES {values}) AS t(fid, geom)"
        )
        voronoi.main(conn, "synth", debug=False)
        bounds = conn.execute(
            "SELECT ST_XMin(geom), ST_XMax(geom), ST_YMin(geom), ST_YMax(geom) "
            "FROM synth_04"
        ).fetchall()
    for xmin, xmax, ymin, ymax in bounds:
        assert lon_min <= xmin <= xmax <= lon_max
        assert lat_min <= ymin <= ymax <= lat_max


def test_extend_renames_reserved_source_columns(tmp_path):
    """A source OGC_FID column collides with GDAL's reserved FID handling.

    COPY to .gpkg fails outright on a column literally named OGC_FID;
    inputs.main must rename it on load rather than let it crash the final COPY.
    """
    path = tmp_path / "synthetic.parquet"
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in _SYNTHETIC_WKT
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(OGC_FID, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")

    output_path = tmp_path / "out.gpkg"
    extend(path, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        cols = [
            c[0]
            for c in conn.execute(
                f"DESCRIBE SELECT * FROM ST_Read('{output_path}')"
            ).fetchall()
        ]
        row_count = conn.execute(
            f"SELECT COUNT(*) FROM ST_Read('{output_path}')"
        ).fetchone()[0]
    assert "OGC_FID_orig" in cols
    assert row_count == len(_SYNTHETIC_WKT)


def test_points_bbox_prefilter_matches_global_union(tmp_path):
    """A fine grid's bbox-prefiltered _03b must equal one global-union _03b."""
    n = 15
    path = tmp_path / "grid.parquet"
    values = ", ".join(
        f"({x * n + y}, ST_GeomFromText("
        f"'POLYGON(({x} {y}, {x + 1} {y}, {x + 1} {y + 1}, {x} {y + 1}, {x} {y}))'))"
        for x in range(n)
        for y in range(n)
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")

    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        inputs.main(conn, "synth", path)
        lines.main(conn, "synth")
        points.build_segments(conn, "synth")
        points.main(conn, "synth", Decimal("0.25"), debug=True)

        conn.execute("""--sql
            CREATE TABLE ref_global AS
            SELECT ST_Union_Agg(geom) AS geom FROM synth_03a
        """)
        conn.execute("""--sql
            CREATE TABLE ref_03b AS
            SELECT fid, geom FROM (
                SELECT a.fid,
                       UNNEST(ST_Dump(ST_Difference(a.geom, b.geom))).geom AS geom
                FROM synth_03_tmp4 AS a CROSS JOIN ref_global AS b
                UNION ALL
                SELECT a.fid,
                       UNNEST(ST_Dump(ST_Boundary(
                           ST_Difference(a.geom, b.geom)
                       ))).geom AS geom
                FROM synth_02 AS a CROSS JOIN ref_global AS b
            )
            WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
        """)

        mismatched = conn.execute("""--sql
            SELECT COUNT(*) FROM (
                SELECT fid, ST_Union_Agg(geom) AS geom FROM synth_03b GROUP BY fid
            ) a
            JOIN (
                SELECT fid, ST_Union_Agg(geom) AS geom FROM ref_03b GROUP BY fid
            ) b USING (fid)
            WHERE ST_Area(ST_SymDifference(a.geom, b.geom)) > 1e-20
        """).fetchall()[0][0]
        assert mismatched == 0


def test_extend_runs_on_input_past_antimeridian(tmp_path):
    path = tmp_path / "antimeridian.parquet"
    wkt = [
        "POLYGON((179 0, 179.5 0, 179.5 1, 179 1, 179 0))",
        "POLYGON((179.5 0, 180.0000001 0, 180.0000001 1, 179.5 1, 179.5 0))",
    ]
    values = ", ".join(f"({i}, ST_GeomFromText('{w}'))" for i, w in enumerate(wkt))
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"COPY (SELECT * FROM (VALUES {values}) t(id, geom)) TO '{path}'")
    output_path = tmp_path / "out.parquet"
    extend(path, output_path, overwrite=True)
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        n, xmax = conn.execute(
            f"SELECT count(*), max(ST_XMax(geometry)) FROM '{output_path}'"
        ).fetchone()
    assert (n, xmax) == (2, 180)


def test_attempt_skips_distance_estimated_over_max_points(
    synthetic_input, tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr(attempt, "MAX_POINTS", 1_500)
    output_path = tmp_path / "out.parquet"
    with caplog.at_level("INFO"):
        extend(synthetic_input, output_path)
    assert "skip: synthetic_parquet distance=0.0002," in caplog.text
    assert output_path.exists()


def test_attempt_raises_on_assignment_error_without_retry(
    synthetic_input, tmp_path, monkeypatch
):
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        msg = "Voronoi assignment incomplete: 1/2 points assigned"
        raise voronoi.AssignmentError(msg)

    monkeypatch.setattr(voronoi, "main", fail)
    with pytest.raises(RuntimeError, match=r"distance=.*assignment incomplete"):
        extend(synthetic_input, tmp_path / "out.parquet")
    assert len(calls) == 1
