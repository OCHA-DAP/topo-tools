"""Portability smoke tests: does clean() run to completion on this machine.

Not a correctness suite for the general pipeline, but gap/overlap
*classification* is non-obvious enough that a few tests assert specific outcomes.
"""

import duckdb
import pytest
from click.testing import CliRunner

import topo_tools.core.topo_clean._03_clean as clean_stage
import topo_tools.core.topo_clean._04_outputs as outputs_stage
from topo_tools.api.topo_clean import clean
from topo_tools.api.topo_detect import detect
from topo_tools.cli.main import cli
from topo_tools.core.constants import SNAP_TOLERANCE

# Four independent groups, spatially separated, each exercising one defect.
_SYNTHETIC_WKT = [
    # Donut of 4 polygons noded at their shared corners, enclosing a real
    # 1x1 gap at (1,1)-(2,2); an unenclosed inlet wouldn't count as a gap.
    (1, "POLYGON((0 0, 3 0, 3 1, 2 1, 1 1, 0 1, 0 0))"),
    (2, "POLYGON((0 2, 1 2, 2 2, 3 2, 3 3, 0 3, 0 2))"),
    (3, "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))"),
    (4, "POLYGON((2 1, 3 1, 3 2, 2 2, 2 1))"),
    (5, "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    (6, "POLYGON((10.95 0, 12 0, 12 1, 10.95 1, 10.95 0))"),  # overlaps fid 5 by 0.05
    # fid 8 sits fully inside fid 7; ST_Overlaps alone is false here by OGC
    # definition, so this pair is only caught via the join's ST_Contains half.
    (7, "POLYGON((30 0, 32 0, 32 2, 30 2, 30 0))"),
    (8, "POLYGON((30.5 0.5, 31.5 0.5, 31.5 1.5, 30.5 1.5, 30.5 0.5))"),
    # Second donut: same hole area (1.0) as fid 1-4's, but a thinness ratio
    # (~0.03) far below DEFAULT_THINNESS_RATIO (vs fid 1-4's ~0.785 square).
    (9, "POLYGON((50 0, 62 0, 62 1, 50 1, 50 0))"),
    (10, "POLYGON((50 1.1, 62 1.1, 62 2.1, 50 2.1, 50 1.1))"),
    (11, "POLYGON((50 1, 51 1, 51 1.1, 50 1.1, 50 1))"),
    (12, "POLYGON((61 1, 62 1, 62 1.1, 61 1.1, 61 1))"),
]

# Units sharing y=0 up to x=0, then A's edge leaves B's at a 5 m mouth over 600 m.
_A, _B, _MOUTH = 600 / 111320, 1000 / 111320, 5 / 111320
_WEDGE_WKT = [
    (1, f"POLYGON((-.02 0, 0 0, {_A} {_MOUTH}, {_A} .02, -.02 .02, -.02 0))"),
    (2, f"POLYGON((-.02 0, 0 0, {_B} 0, {_B} -.02, -.02 -.02, -.02 0))"),
]

_STEPS = ["inputs", "issues", "clean", "outputs"]


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


def _real_hole_area(path):
    """Area of any fully-enclosed hole in the union of an output file's polygons."""
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"""
            WITH u AS (SELECT ST_Union_Agg(geometry) AS g FROM '{path}'),
            parts AS (SELECT (UNNEST(ST_Dump(g))).geom AS geom FROM u)
            SELECT COALESCE(
                SUM(
                    ST_Area(ST_Difference(ST_MakePolygon(ST_ExteriorRing(geom)), geom))
                ),
                0
            )
            FROM parts WHERE ST_NumInteriorRings(geom) > 0
        """).fetchone()[0]


def test_cli_help():
    result = CliRunner().invoke(cli, ["topo-clean", "--help"])
    assert result.exit_code == 0
    assert "Find and fix gaps and overlaps" in result.output
    assert "Examples:" in result.output


def test_clean_full_run(synthetic_input, tmp_path):
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clean(synthetic_input, output_path, issues_path, overwrite=True)

    assert output_path.exists()
    assert issues_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
        kinds = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT kind FROM '{issues_path}'"
            ).fetchall()
        }
    assert row_count == len(_SYNTHETIC_WKT)
    assert kinds == {"gap", "overlap"}


def test_clean_closes_notch(tmp_path):
    input_path = tmp_path / "notch.parquet"
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in _WEDGE_WKT)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{input_path}'")
    issues_path = tmp_path / "issues.parquet"
    clean(input_path, tmp_path / "out.parquet", issues_path, overwrite=True)
    with duckdb.connect() as conn:
        rows = conn.execute(f"SELECT kind, fixed FROM '{issues_path}'").fetchall()
    assert rows == [("notch", True)]


def test_clean_issues_report_overlap_outcome(synthetic_input, tmp_path):
    """An overlap row reports each unit's own real area change, not just the defect.

    fid 8 sits inside fid 7, so one side's change is a big loss, the other
    untouched (unit_a/b order isn't guaranteed, so check the pair unordered).
    """
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    clean(synthetic_input, output_path, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        a_change, b_change, fixed = conn.execute(f"""
            SELECT unit_a_area_change_m2, unit_b_area_change_m2, fixed
            FROM '{issues_path}'
            WHERE kind = 'overlap'
              AND ST_Within(geometry, ST_MakeEnvelope(30, 0, 32, 2))
        """).fetchall()[0]

    clearly_lost_a_lot_m2 = -1e9
    swallowed, untouched = sorted([a_change, b_change])
    assert swallowed < clearly_lost_a_lot_m2
    assert untouched == pytest.approx(0.0)
    assert fixed is True


@pytest.mark.parametrize("with_overlap", [False, True])
def test_clean_reports_merged_micro_polygon(with_overlap, tmp_path):
    """A micro feature is merged and reported, with or without a coverage_clean."""
    sliver, micro_id = SNAP_TOLERANCE / 10, 3
    rows = [
        (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
        (2, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
        (3, f"POLYGON((2 0, {2 + sliver} 0, {2 + sliver} 1, 2 1, 2 0))"),
    ]
    if with_overlap:
        rows += [r for r in _SYNTHETIC_WKT if r[0] in (5, 6)]
    input_path = tmp_path / "micro.parquet"
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in rows)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {values}) t(id, geom)) TO '{input_path}'"
        )
    clean(input_path, output_path, issues_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        out_ids = conn.execute(
            f"SELECT id FROM '{output_path}' WHERE NOT ST_IsEmpty(geometry) ORDER BY id"
        ).fetchall()
        micro = conn.execute(f"""
            SELECT unit_b IS NOT NULL, reason, fixed, ST_Area(geometry) > 0
            FROM '{issues_path}' WHERE kind = 'micro-polygon'
        """).fetchall()
    assert [r[0] for r in out_ids] == [r[0] for r in rows if r[0] != micro_id]
    assert micro == [(True, "merged into neighbouring polygon", True, True)]


def test_clean_default_output_paths(synthetic_input):
    clean(synthetic_input, overwrite=True)

    expected_output = synthetic_input.with_stem(synthetic_input.stem + "_cleaned")
    expected_issues = expected_output.with_stem(expected_output.stem + "_issues")
    assert expected_output.exists()
    assert expected_issues.exists()


@pytest.fixture
def scaled_gap_input(tmp_path):
    """Write a single compact 1x1 hole inside a ~10,200-area ring, not ~8.

    `_SYNTHETIC_WKT`'s fid 1-4 hole is unrepresentative of real admin-boundary
    scale; same shape/width (1x1, ~0.785 thinness), realistically-scaled ring.
    """
    path = tmp_path / "scaled_gap.parquet"
    wkt = [
        (1, "POLYGON((0 -50, 101 -50, 101 0, 0 0, 0 -50))"),
        (2, "POLYGON((0 1, 101 1, 101 51, 0 51, 0 1))"),
        (3, "POLYGON((0 0, 50 0, 50 1, 0 1, 0 0))"),
        (4, "POLYGON((51 0, 101 0, 101 1, 51 1, 51 0))"),
    ]
    values = ", ".join(f"({fid}, ST_GeomFromText('{w}'))" for fid, w in wkt)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")
    return path


@pytest.fixture
def gap_only_input(tmp_path):
    """Write a ring of 4 polygons enclosing a 1x1 hole, with no overlaps anywhere.

    Fully noded and realistically-scaled, so `has_invalid_edges()` is False;
    regression fixture for the old has_invalid_edges()-only fix-stage gate.
    """
    path = tmp_path / "gap_only.parquet"
    wkt = [
        (1, "POLYGON((0 -50, 101 -50, 101 0, 51 0, 50 0, 0 0, 0 -50))"),
        (2, "POLYGON((0 1, 50 1, 51 1, 101 1, 101 51, 0 51, 0 1))"),
        (3, "POLYGON((0 0, 50 0, 50 1, 0 1, 0 0))"),
        (4, "POLYGON((51 0, 101 0, 101 1, 51 1, 51 0))"),
    ]
    values = ", ".join(f"({fid}, ST_GeomFromText('{w}'))" for fid, w in wkt)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")
    return path


@pytest.fixture
def noise_scale_gap_input(tmp_path):
    """Reuse gap_only_input's ring/gap topology, gap narrowed below SNAP_TOLERANCE.

    Only the gap's width shrinks, ring stays at realistic 101-wide scale;
    shrinking the whole ring instead collapsed the output at GEOS's own floor.
    """
    path = tmp_path / "noise_scale_gap.parquet"
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
    return path


def test_clean_fills_gap_with_no_coverage_violations(gap_only_input, tmp_path):
    """`--maximum-gap-width all` must still fill a gap when nothing overlaps.

    Regression for the old has_invalid_edges()-only fix-stage gate, False
    here despite a real fully-enclosed gap that must still get filled.
    """
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        violations = conn.execute(f"""
            SELECT ST_CoverageInvalidEdges_Agg(geom) IS NOT NULL
            FROM (SELECT UNNEST(ST_Dump(geom)).geom AS geom FROM '{gap_only_input}')
        """).fetchone()[0]
    assert violations is False

    output_path = tmp_path / "gap_only_cleaned.parquet"
    issues_path = tmp_path / "gap_only_issues.parquet"
    clean(
        gap_only_input,
        output_path,
        issues_path,
        maximum_gap_width="all",
        overwrite=True,
    )

    assert _real_hole_area(output_path) == pytest.approx(0.0, abs=1e-9)


def test_clean_maximum_gap_width_all_fills_gap(scaled_gap_input, tmp_path):
    output_path = tmp_path / "all.parquet"
    issues_path = tmp_path / "all_issues.parquet"
    clean(
        scaled_gap_input,
        output_path,
        issues_path,
        maximum_gap_width="all",
        overwrite=True,
    )

    assert _real_hole_area(output_path) == pytest.approx(0.0, abs=1e-9)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        filled = conn.execute(
            f"SELECT filled_area_m2 FROM '{issues_path}' WHERE kind = 'gap'"
        ).fetchall()[0][0]
    # The gap is fully closed, so its whole area counts as filled.
    assert filled > 0


def test_clean_maximum_gap_width_thin_fills_only_thin_gap(synthetic_input, tmp_path):
    """Thin fills the fid 9-12 sliver but leaves the fid 1-4 compact square alone."""
    output_path = tmp_path / "thin.parquet"
    issues_path = tmp_path / "thin_issues.parquet"
    clean(
        synthetic_input,
        output_path,
        issues_path,
        maximum_gap_width="thin",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        compact_filled, thin_filled = conn.execute(f"""
            WITH u AS (SELECT ST_Union_Agg(geometry) AS g FROM '{output_path}')
            SELECT ST_Contains(u.g, ST_Point(1.5, 1.5)),
                   ST_Contains(u.g, ST_Point(55, 1.05))
            FROM u
        """).fetchone()
        compact_fixed, thin_fixed = (
            r[0]
            for r in conn.execute(f"""
                SELECT fixed FROM '{issues_path}' WHERE kind = 'gap'
                ORDER BY thinness_ratio DESC
            """).fetchall()
        )
    assert compact_filled is False
    assert thin_filled is True
    assert compact_fixed is False
    assert thin_fixed is True


@pytest.mark.parametrize("mode", [None, "thin", "all"])
def test_clean_reports_every_gap_topo_detect_finds(mode, synthetic_input, tmp_path):
    output_path = tmp_path / "clean.parquet"
    issues_path = tmp_path / "clean_issues.parquet"
    clean(
        synthetic_input,
        output_path,
        issues_path,
        maximum_gap_width=mode,
        overwrite=True,
    )
    detect_path = tmp_path / "detect_issues.parquet"
    detect(output_path, detect_path, overwrite=True)

    with duckdb.connect() as conn:
        unfilled = conn.execute(
            f"SELECT count(*) FROM '{issues_path}' WHERE kind = 'gap' AND NOT fixed"
        ).fetchone()[0]
        found = (
            conn.execute(
                f"SELECT count(*) FROM '{detect_path}' WHERE kind = 'gap'"
            ).fetchone()[0]
            if detect_path.exists()
            else 0
        )
    assert unfilled == found


def test_clean_maximum_gap_width_default_leaves_real_gap_unfilled(
    scaled_gap_input, tmp_path
):
    """Omitting --maximum-gap-width must not fill a real-scale gap."""
    output_path = tmp_path / "default.parquet"
    issues_path = tmp_path / "default_issues.parquet"
    clean(scaled_gap_input, output_path, issues_path, overwrite=True)

    assert _real_hole_area(output_path) == pytest.approx(1.0, rel=1e-6)


def test_clean_maximum_gap_width_default_fills_noise_scale_gap(
    noise_scale_gap_input, tmp_path
):
    """Omitting --maximum-gap-width must still fill a sub-SNAP_TOLERANCE gap."""
    output_path = tmp_path / "default_noise.parquet"
    issues_path = tmp_path / "default_noise_issues.parquet"
    clean(noise_scale_gap_input, output_path, issues_path, overwrite=True)

    assert _real_hole_area(output_path) == pytest.approx(0.0, abs=1e-20)


def test_clean_maximum_gap_width_explicit_degrees(scaled_gap_input, tmp_path):
    narrow_output = tmp_path / "narrow.parquet"
    narrow_issues = tmp_path / "narrow_issues.parquet"
    clean(
        scaled_gap_input,
        narrow_output,
        narrow_issues,
        maximum_gap_width="0.5",
        overwrite=True,
    )
    # 0.5 degrees doesn't clear the compact square's ~1 degree MIC width,
    # so the hole (area 1.0) remains.
    assert _real_hole_area(narrow_output) == pytest.approx(1.0, rel=1e-6)

    wide_output = tmp_path / "wide.parquet"
    wide_issues = tmp_path / "wide_issues.parquet"
    clean(
        scaled_gap_input,
        wide_output,
        wide_issues,
        maximum_gap_width="2",
        overwrite=True,
    )
    assert _real_hole_area(wide_output) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("value", ["potato", "auto"])
def test_clean_invalid_maximum_gap_width_value(value, synthetic_input, tmp_path):
    """'auto' is rejected: the noise-floor default is only reachable by omission."""
    with pytest.raises(ValueError, match="maximum-gap-width"):
        clean(
            synthetic_input,
            tmp_path / "bad.parquet",
            maximum_gap_width=value,
            overwrite=True,
        )


@pytest.mark.parametrize("value", ["potato", "auto"])
def test_clean_invalid_snapping_distance_value(value, synthetic_input, tmp_path):
    """'auto' is rejected: the noise-floor default is only reachable by omission."""
    with pytest.raises(ValueError, match="snapping-distance"):
        clean(
            synthetic_input,
            tmp_path / "bad.parquet",
            snapping_distance=value,
            overwrite=True,
        )


def _empty_issues_table_sql(name: str) -> str:
    return f"""
        CREATE TABLE {name} AS
        SELECT NULL::VARCHAR AS key, NULL::VARCHAR AS kind,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               NULL::BIGINT AS unit_a, NULL::BIGINT AS unit_b,
               NULL::GEOMETRY AS geom
        WHERE FALSE
    """


def test_partly_filled_gap_is_not_fixed(tmp_path):
    """The fill covers the gap's interior point but leaves 40% of it open."""
    left = "POLYGON((0 0, 1 0, 1 1, 1 1.6, 1 2, 1 3, 0 3, 0 0))"
    right = "POLYGON((2 0, 3 0, 3 3, 2 3, 2 2, 2 1.6, 2 1, 2 0))"
    top = "POLYGON((1 2, 2 2, 2 3, 1 3, 1 2))"
    bottoms = {
        "stage_01": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        "stage_03": "POLYGON((1 0, 2 0, 2 1, 2 1.6, 1 1.6, 1 1, 1 0))",
    }
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        for table, bottom in bottoms.items():
            values = ", ".join(
                f"({fid}, ST_GeomFromText('{wkt}'))"
                for fid, wkt in enumerate((left, right, top, bottom), start=1)
            )
            conn.execute(f"CREATE TABLE {table} AS FROM (VALUES {values}) t(fid, geom)")
        conn.execute(_empty_issues_table_sql("stage_02"))
        conn.execute("""
            INSERT INTO stage_02 (key, kind, geom)
            VALUES ('gap-1', 'gap', ST_MakeEnvelope(1, 1, 2, 2))
        """)
        conn.execute("CREATE TABLE stage_03_micro AS FROM stage_02 WHERE FALSE")
        issues_path = tmp_path / "issues.parquet"
        outputs_stage.main(conn, "stage", tmp_path / "out.parquet", issues_path)
        rows = conn.execute(f"SELECT key, fixed FROM '{issues_path}'").fetchall()
    assert rows == [("gap-1", False)]


def test_notch_fixed_matches_location_not_just_unit_pair(tmp_path):
    """Two notches on one unit pair: only the one still detected stays unfixed."""
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in _WEDGE_WKT)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"CREATE TABLE stage_01 AS FROM (VALUES {values}) AS t(fid, geom)")
        conn.execute("CREATE TABLE stage_03 AS FROM stage_01")
        conn.execute(_empty_issues_table_sql("stage_02"))
        conn.execute(f"""
            INSERT INTO stage_02 (key, kind, unit_a, unit_b, geom) VALUES
            ('notch-1', 'notch', 1, 2, ST_MakeEnvelope(0, 0, {_A}, {_MOUTH})),
            ('notch-2', 'notch', 1, 2, ST_MakeEnvelope(0.01, -0.01, 0.011, -0.009))
        """)
        conn.execute("CREATE TABLE stage_03_micro AS FROM stage_02 WHERE FALSE")
        issues_path = tmp_path / "issues.parquet"
        outputs_stage.main(conn, "stage", tmp_path / "out.parquet", issues_path)
        rows = conn.execute(
            f"SELECT key, fixed FROM '{issues_path}' ORDER BY key"
        ).fetchall()
    assert rows == [("notch-1", False), ("notch-2", True)]


def _overlapping_pair_conn():
    """Open a connection with a minimal 2-fid overlapping `stage_01`/`stage_02` pair."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""
        CREATE TABLE stage_01 AS
        SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))')),
            (2, ST_GeomFromText('POLYGON((1 1, 3 1, 3 3, 1 3, 1 1))'))
        ) AS t(fid, geom)
    """)
    # The two squares above overlap on (1 1, 2 2), area 1, so both fids
    # are exempt from the defect-unrelated collapse/drift check.
    conn.execute("""
        CREATE TABLE stage_02 AS
        SELECT 'overlap-1' AS key, 'overlap' AS kind,
               NULL::DOUBLE AS area_m2, NULL::DOUBLE AS max_width_m,
               NULL::DOUBLE AS thinness_ratio,
               NULL::DOUBLE AS near_length_m,
               1 AS unit_a, 2 AS unit_b,
               ST_Intersection(a.geom, b.geom) AS geom
        FROM stage_01 a, stage_01 b WHERE a.fid = 1 AND b.fid = 2
    """)
    return conn


def test_clean_raises_immediately_on_coverage_clean_failure(monkeypatch):
    """A coverage_clean() failure propagates directly: there is no retry."""

    def always_fails(*_args, **_kwargs):
        msg = "simulated GEOS failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(clean_stage, "coverage_clean", always_fails)

    with (
        _overlapping_pair_conn() as conn,
        pytest.raises(RuntimeError, match="simulated GEOS failure"),
    ):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )


def test_clean_rejects_eroded_output(monkeypatch):
    """A totally empty coverage_clean() result must not be accepted as success.

    Regression: has_invalid_edges() alone passes an empty result as
    "no violations", the area-sanity check must catch it too.
    """

    def empties_output(conn, table_in, table_out, **_kwargs):
        query = (
            f'CREATE OR REPLACE TABLE "{table_out}" AS '
            f'SELECT * FROM "{table_in}" WHERE FALSE'
        )
        conn.execute(query)

    monkeypatch.setattr(clean_stage, "coverage_clean", empties_output)

    with (
        _overlapping_pair_conn() as conn,
        pytest.raises(RuntimeError, match="invalid, collapsed, or under-filled"),
    ):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )


def test_clean_rejects_collapsed_fid(monkeypatch):
    """A fid collapsing to zero area must not be accepted, even if the total survives.

    Regression: a summed-total-only floor misses a small feature vanishing
    inside a much larger dataset; neither fid here touches a gap or overlap.
    """

    def collapses_one_fid(conn, table_in, table_out, **_kwargs):
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{table_out}" AS
            SELECT fid,
                   CASE WHEN fid = 1 THEN ST_GeomFromText('POLYGON EMPTY')
                        ELSE geom END AS geom
            FROM "{table_in}"
        """)

    monkeypatch.setattr(clean_stage, "coverage_clean", collapses_one_fid)

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""
        CREATE TABLE stage_01 AS
        SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText('POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))'))
        ) AS t(fid, geom)
    """)
    conn.execute(_empty_issues_table_sql("stage_02"))

    with conn, pytest.raises(RuntimeError, match="invalid, collapsed, or under-filled"):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )


def test_clean_rejects_bad_geometry_type(monkeypatch):
    """A fid degenerating into a mixed-type GeometryCollection must be rejected.

    Regression: ST_Area() only sums polygonal members, so a fix fusing a
    stray line in can preserve the measured area while the output is invalid.
    """

    def degenerates_one_fid(conn, table_in, table_out, **_kwargs):
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{table_out}" AS
            SELECT fid,
                   CASE WHEN fid = 1
                        THEN ST_GeomFromText(
                            'GEOMETRYCOLLECTION('
                            'LINESTRING(0 0, 1 1), '
                            'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0)))'
                        )
                        ELSE geom END AS geom
            FROM "{table_in}"
        """)

    monkeypatch.setattr(clean_stage, "coverage_clean", degenerates_one_fid)

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""
        CREATE TABLE stage_01 AS
        SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText('POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))'))
        ) AS t(fid, geom)
    """)
    conn.execute(_empty_issues_table_sql("stage_02"))

    with conn, pytest.raises(RuntimeError, match="invalid, collapsed, or under-filled"):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )


def test_clean_logs_area_change_on_success(caplog):
    """A successful fix reports the overall area change, not just pass/fail."""
    with _overlapping_pair_conn() as conn, caplog.at_level("INFO"):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )

    messages = [r.message for r in caplog.records if "total area" in r.message]
    assert len(messages) == 1
    assert "lost" in messages[0]


def test_clean_overlap_exemption_is_unconditional(monkeypatch):
    """A fid party to a detected overlap may shrink by more than the overlap's own area.

    Regression: an earlier version bounded the exemption to the overlap's
    own footprint (area 1); fid 1 here shrinks to area 2.5, past that bound.
    """

    def shrinks_beyond_overlap_budget(conn, table_in, table_out, **_kwargs):
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{table_out}" AS
            SELECT fid,
                   CASE WHEN fid = 1
                        THEN ST_GeomFromText(
                            'POLYGON((0 -2, 2 -2, 2 -0.75, 0 -0.75, 0 -2))'
                        )
                        ELSE geom END AS geom
            FROM "{table_in}"
        """)

    monkeypatch.setattr(clean_stage, "coverage_clean", shrinks_beyond_overlap_budget)

    expected_row_count = 2
    with _overlapping_pair_conn() as conn:
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )
        row_count = conn.execute("SELECT COUNT(*) FROM stage_03").fetchone()[0]

    assert row_count == expected_row_count


def test_clean_logs_defect_unrelated_drift_without_rejecting(monkeypatch, caplog):
    """A fid untouched by any defect may drift slightly without failing the rung.

    Regression: whole-table renoding measurably shifts unrelated boundaries
    on real data; this must log, not reject. Neither fid here has an issue row.
    """

    def drifts_one_fid(conn, table_in, table_out, **_kwargs):
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{table_out}" AS
            SELECT fid,
                   CASE WHEN fid = 2
                        THEN ST_GeomFromText(
                            'POLYGON((10 10, 19.9 10, 19.9 20, 10 20, 10 10))'
                        )
                        ELSE geom END AS geom
            FROM "{table_in}"
        """)

    monkeypatch.setattr(clean_stage, "coverage_clean", drifts_one_fid)

    expected_row_count = 2
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""
        CREATE TABLE stage_01 AS
        SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText('POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))'))
        ) AS t(fid, geom)
    """)
    conn.execute(_empty_issues_table_sql("stage_02"))

    with conn, caplog.at_level("WARNING"):
        clean_stage.main(
            conn,
            "stage",
            gap_maximum_width=("value", 0.5),
            snapping_distance=("default", None),
        )
        row_count = conn.execute("SELECT COUNT(*) FROM stage_03").fetchone()[0]

    assert row_count == expected_row_count
    messages = [r.message for r in caplog.records if "shifted area anyway" in r.message]
    assert len(messages) == 1


def test_cli_positional_args(synthetic_input, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli, ["topo-clean", str(synthetic_input), str(output_path)]
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_clean_error_on_existing_output(synthetic_input, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli, ["topo-clean", str(synthetic_input), str(output_path), "--overwrite=false"]
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_clean_steps(synthetic_input, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    issues_path = tmp_path / "steps_issues.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        clean(
            synthetic_input,
            output_path,
            issues_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()
    assert issues_path.exists()
