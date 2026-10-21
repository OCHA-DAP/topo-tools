"""Unit tests for core/coverage.py's width-aware gap and micro-polygon checking."""

import duckdb
import pytest

from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.coverage import (
    check_valid_topology,
    close_notches,
    count_gaps,
    coverage_clean,
    detect_notches,
    gap_issues_sql,
    has_gaps,
    has_invalid_edges,
    has_micro_polygons,
    merge_detached_parts,
    merge_micro_polygons,
)


def _write_polygon_with_hole(conn, hole_width):
    """One polygon: a 3x3 square with a centered hole_width square hole cut out."""
    w = hole_width
    lo, hi = 1.5 - w / 2, 1.5 + w / 2
    wkt = (
        f"POLYGON((0 0, 3 0, 3 3, 0 3, 0 0), "
        f"({lo} {lo}, {hi} {lo}, {hi} {hi}, {lo} {hi}, {lo} {lo}))"
    )
    conn.execute(
        "CREATE OR REPLACE TABLE synth AS "
        f"SELECT 1 AS fid, ST_GeomFromText('{wkt}') AS geom"
    )


def test_has_gaps_tolerates_wide_hole_when_scoped():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=2.0)
        assert has_gaps(conn, "synth", gap_maximum_width=0)
        assert not has_gaps(conn, "synth", gap_maximum_width=SNAP_TOLERANCE)


@pytest.mark.parametrize(("within", "rows"), [((1, 2), 1), ((5, 6), 0)])
def test_gap_issues_keep_only_gaps_inside_within(within, rows):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=0.5)
        lo, hi = within
        box = f"SELECT ST_MakeEnvelope({lo}, {lo}, {hi}, {hi}) AS geom"
        sql = gap_issues_sql(conn, "synth", within=box)
        assert conn.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0] == rows


def test_check_valid_topology_raises_on_micro_gap_even_when_scoped():
    """A gap at/below SNAP_TOLERANCE must still raise: only wider gaps are tolerated."""
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=SNAP_TOLERANCE / 2)
        with pytest.raises(RuntimeError, match="GAPS"):
            check_valid_topology(conn, "synth", gap_maximum_width=SNAP_TOLERANCE)


def test_check_valid_topology_tolerates_wide_gap_when_scoped():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=2.0)
        check_valid_topology(conn, "synth", gap_maximum_width=SNAP_TOLERANCE)


def test_count_gaps_respects_min_width():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=2.0)
        assert count_gaps(conn, "synth") == 1
        assert count_gaps(conn, "synth", min_width=SNAP_TOLERANCE) == 1
        assert count_gaps(conn, "synth", min_width=3.0) == 0


SLIVER = SNAP_TOLERANCE / 10
SQUARE_A = "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
SQUARE_B = "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"


def _strip(x0, width, height=0.5):
    """Return a width-wide, height-tall rectangle at x0 as WKT ring text."""
    x1 = x0 + width
    return f"(({x0} 0, {x1} 0, {x1} {height}, {x0} {height}, {x0} 0))"


def _merge(rows):
    """Load (fid, wkt) rows, run merge_micro_polygons, return (count, out, issues)."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    values = ", ".join(f"({fid}, '{wkt}')" for fid, wkt in rows)
    conn.execute(
        "CREATE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom "
        f"FROM (VALUES {values}) v(fid, wkt)"
    )
    count = merge_micro_polygons(conn, "synth", "out", issues_table="issues")
    out = dict(
        conn.execute(
            "SELECT fid, ST_NumGeometries(geom) FROM out ORDER BY fid"
        ).fetchall()
    )
    issues = conn.execute(
        "SELECT unit_a, unit_b, reason, fixed FROM issues ORDER BY key"
    ).fetchall()
    assert not has_micro_polygons(conn, "out")
    return count, out, issues


def test_micro_polygon_raises_in_check_valid_topology():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        wkt = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {_strip(5, SLIVER)})"
        conn.execute(f"CREATE TABLE synth AS SELECT 1 AS fid, '{wkt}'::GEOMETRY geom")
        assert has_micro_polygons(conn, "synth")
        with pytest.raises(RuntimeError, match="MICRO_POLYGONS"):
            check_valid_topology(conn, "synth")


def test_overlap_sliver_merges_into_the_feature_it_overlaps():
    wkt_a = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {_strip(1, SLIVER)})"
    count, out, issues = _merge([(1, wkt_a), (2, SQUARE_B)])
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


def test_touching_fragment_merges_into_its_own_large_part():
    wkt_a = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {_strip(1, SLIVER)})"
    count, out, issues = _merge([(1, wkt_a)])
    assert count == 1
    assert out == {1: 1}
    assert issues == [(1, 1, "merged into neighbouring polygon", True)]


def test_isolated_fragment_is_dropped_and_large_part_kept():
    wkt_a = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {_strip(5, SLIVER)})"
    count, out, issues = _merge([(1, wkt_a), (2, SQUARE_B)])
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, None, "dropped: touches no polygon", True)]


def test_whole_micro_feature_row_is_removed():
    wkt_c = "POLYGON" + _strip(2, SLIVER)
    count, out, issues = _merge([(1, SQUARE_A), (2, SQUARE_B), (3, wkt_c)])
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(3, 2, "merged into neighbouring polygon", True)]


def test_micro_polygon_keys_ignore_input_row_order():
    wkt_a = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {_strip(5, SLIVER)})"
    wkt_b = f"MULTIPOLYGON(((2 0, 3 0, 3 1, 2 1, 2 0)), {_strip(7, SLIVER)})"
    for rows in ([(1, wkt_a), (2, wkt_b)], [(2, wkt_b), (1, wkt_a)]):
        conn = duckdb.connect()
        conn.execute("INSTALL spatial; LOAD spatial;")
        values = ", ".join(f"({fid}, '{wkt}')" for fid, wkt in rows)
        conn.execute(
            "CREATE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom "
            f"FROM (VALUES {values}) v(fid, wkt)"
        )
        merge_micro_polygons(conn, "synth", "out", issues_table="issues")
        keys = conn.execute("SELECT key, unit_a FROM issues ORDER BY key").fetchall()
        assert keys == [("micro-polygon-1", 1), ("micro-polygon-2", 2)]


def test_small_real_island_is_kept():
    island = _strip(5, SNAP_TOLERANCE * 10, height=SNAP_TOLERANCE * 10)
    wkt_a = f"MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)), {island})"
    count, out, issues = _merge([(1, wkt_a)])
    assert count == 0
    assert out == {1: 2}
    assert issues == []


def test_coverage_clean_removes_whole_micro_feature_instead_of_emptying_it():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        wkt_c = "POLYGON" + _strip(2, SLIVER, height=1)
        conn.execute(
            "CREATE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom FROM "
            f"(VALUES (1, '{SQUARE_A}'), (2, '{SQUARE_B}'), (3, '{wkt_c}')) v(fid, wkt)"
        )
        coverage_clean(conn, "synth", "out", fids=None, micro_issues_table="micro")
        rows = conn.execute("SELECT fid, ST_IsEmpty(geom) FROM out ORDER BY fid")
        assert rows.fetchall() == [(1, False), (2, False)]
        assert conn.execute("SELECT unit_a, unit_b FROM micro").fetchall() == [(3, 2)]


@pytest.mark.parametrize("order", ["ASC", "DESC"])
def test_coverage_clean_ignores_input_row_order(order):
    """The overlap goes to the same feature whichever row comes first."""
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom FROM (VALUES
                (1, 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))'),
                (2, 'POLYGON((0.9 0, 2 0, 2 1, 0.9 1, 0.9 0))')) v(fid, wkt)
            ORDER BY fid {order}
        """)
        coverage_clean(conn, "synth", "out", fids=None)
        rows = conn.execute("SELECT fid, ST_Area(geom) FROM out ORDER BY fid")
        assert [(f, round(a, 6)) for f, a in rows.fetchall()] == [(1, 0.9), (2, 1.1)]


# Detached-part fixtures use 1e-3 deg units (~111 m), so pieces stay under the
# merge cap in square meters and only the ratio rule decides.
S = 1e-3


def _rect(x0, y0, x1, y1, scale=S):
    """Return an axis-aligned rectangle as WKT ring text."""
    x0, y0, x1, y1 = (v * scale for v in (x0, y0, x1, y1))
    return f"(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def _multi(*rings):
    return "MULTIPOLYGON(" + ", ".join(rings) + ")"


MAIN_1 = _rect(0, 0, 10, 8)
PRE_1 = "POLYGON" + _rect(0, 0, 10, 10)
PIECE_1 = _rect(0, 9, 0.05, 9.1)
LEFT_2 = "POLYGON" + _rect(-5, 0, 0, 10)


def _detach(clipped, pre, original=None):
    """Run merge_detached_parts on (fid, wkt) rows; return count/out/issues/conn."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")

    def load(table, rows, cols):
        values = ", ".join(str(r) for r in rows)
        conn.execute(
            f"CREATE TABLE {table} AS SELECT * EXCLUDE (wkt), "
            f"ST_GeomFromText(wkt) AS geom FROM (VALUES {values}) v({cols}, wkt)"
        )

    load("clipped", clipped, "fid")
    load("pre", pre, "fid, overlay_fid")
    if original is not None:
        load("original", original, "fid")
    conn.execute("""
        CREATE TABLE overlay AS
        SELECT p.overlay_fid AS fid, ST_Union_Agg(c.geom) AS geom
        FROM clipped c JOIN pre p USING (fid) GROUP BY p.overlay_fid
    """)
    count = merge_detached_parts(
        conn,
        "clipped",
        "out",
        pre_clip_table="pre",
        overlay_source="overlay",
        original_table="original" if original is not None else None,
        issues_table="issues",
    )
    out = dict(
        conn.execute(
            "SELECT fid, ST_NumGeometries(geom) FROM out ORDER BY fid"
        ).fetchall()
    )
    issues = conn.execute(
        "SELECT unit_a, unit_b, reason, fixed FROM issues ORDER BY key"
    ).fetchall()
    return count, out, issues, conn


def test_detached_sliver_merges_into_edge_neighbour():
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
        original=[(8, PRE_1), (9, LEFT_2)],
    )
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


def test_detached_sliver_without_original_is_only_reported():
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
    )
    assert count == 0
    assert out == {1: 2, 2: 1}
    assert issues == [(1, 2, "kept: no original layer", False)]


def test_detached_piece_drawn_as_original_lobe_is_kept():
    ring = [(0, 0), (10, 0), (10, 8), (4e-4, 8), (4e-4, 9), (0.05, 9), (0.05, 9.1)]
    ring += [(0, 9.1), (0, 0)]
    lobe = "POLYGON((" + ", ".join(f"{x * S} {y * S}" for x, y in ring) + "))"
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
        original=[(1, lobe), (2, LEFT_2)],
    )
    assert count == 0
    assert out == {1: 2, 2: 1}
    assert issues == [(1, 2, "kept: matches original shape", False)]


def test_detached_piece_over_ratio_is_kept_and_reported():
    count, out, issues, conn = _detach(
        [(1, _multi(MAIN_1, _rect(0, 9, 1, 10))), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
    )
    assert count == 0
    assert out == {1: 2, 2: 1}
    assert issues == [(1, 2, "kept: too large to merge", False)]
    width, thinness = conn.execute(
        "SELECT max_width_m, thinness_ratio FROM issues"
    ).fetchone()
    assert width > 0
    assert 0 < thinness <= 1


def test_detached_ratio_is_per_original_part_not_per_feature():
    island_pre = _rect(20, 0, 21, 1)
    clipped = _multi(MAIN_1, _rect(20, 0, 20.6, 1), _rect(20.7, 0, 21, 1))
    right = "POLYGON" + _rect(21, 0, 22, 1)
    count, out, issues, _ = _detach(
        [(1, clipped), (2, right)],
        [(1, 1, _multi(_rect(0, 0, 10, 10), island_pre)), (2, 1, right)],
    )
    assert count == 0
    assert out == {1: 3, 2: 1}
    assert issues == [(1, 2, "kept: too large to merge", False)]


def test_isolated_detached_piece_is_kept_unreported():
    count, out, issues, _ = _detach([(1, _multi(MAIN_1, PIECE_1))], [(1, 1, PRE_1)])
    assert (count, out, issues) == (0, {1: 2}, [])


def test_point_contact_is_not_a_destination():
    corner = "POLYGON" + _rect(-1, 9.1, 0, 10)
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, corner)],
        [(1, 1, PRE_1), (2, 1, corner)],
    )
    assert (count, out, issues) == (0, {1: 2, 2: 1}, [])


def test_neighbour_under_another_overlay_is_not_a_destination():
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 2, LEFT_2)],
    )
    assert (count, out, issues) == (0, {1: 2, 2: 1}, [])


def test_detached_pieces_swap_between_two_features():
    ring = [(10, 0), (20, 0), (20, 10), (10, 10), (10, 5.1), (5, 5.1), (5, 5), (10, 5)]
    pre_2 = "POLYGON((" + ", ".join(f"{x * S} {y * S}" for x, y in [*ring, ring[0]])
    pre_2 += "))"
    notch = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 5.1), (10, 5.1), (10, 10), (0, 10)]
    orig_1 = "POLYGON((" + ", ".join(f"{x * S} {y * S}" for x, y in [*notch, notch[0]])
    orig_1 += "))"
    count, out, issues, conn = _detach(
        [
            (1, _multi(_rect(0, 0, 10, 5), _rect(9.9, 9, 10, 9.1))),
            (2, _multi(_rect(10, 0, 20, 10), _rect(5, 5, 5.1, 5.1))),
        ],
        [(1, 1, PRE_1), (2, 1, pre_2)],
        original=[(1, orig_1), (2, pre_2)],
    )
    assert count == len(issues)
    assert out == {1: 1, 2: 1}
    assert [(a, b) for a, b, _, _ in issues] == [(1, 2), (2, 1)]
    areas = conn.execute("SELECT fid, ST_Area(geom) FROM out ORDER BY fid").fetchall()
    assert [(f, round(a / S**2, 6)) for f, a in areas] == [(1, 50.01), (2, 100.01)]


def test_footprint_piece_is_kept_over_larger_extension_piece():
    footprint = _rect(0, 0, 1, 1)
    right = "POLYGON" + _rect(20, 0, 21, 10)
    count, out, issues, conn = _detach(
        [(1, _multi(footprint, _rect(5, 0, 20, 10))), (2, right)],
        [(1, 1, "POLYGON" + _rect(0, 0, 20, 10)), (2, 1, right)],
        original=[(1, "POLYGON" + footprint), (2, right)],
    )
    assert count == 0
    assert out == {1: 2, 2: 1}
    assert issues == [(1, 2, "kept: too large to merge", False)]
    extension_area = pytest.approx(150 * S**2)
    assert (
        conn.execute("SELECT ST_Area(geom) FROM issues").fetchone()[0] == extension_area
    )


def test_piece_mostly_on_footprint_is_kept_when_its_point_hits_a_hole():
    big, small = _rect(5, 0, 20, 10), _rect(0, 0, 1, 1)
    holed = f"POLYGON({_rect(0, 0, 20, 10)[1:-1]}, {_rect(12, 4.5, 13, 5.5)[1:-1]})"
    count, out, issues, _ = _detach(
        [(1, _multi(big, small)), (2, LEFT_2)],
        [(1, 1, "POLYGON" + _rect(0, 0, 20, 10)), (2, 1, LEFT_2)],
        original=[(1, holed), (2, LEFT_2)],
    )
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


def test_only_a_too_large_detached_piece_is_a_destination():
    count, out, issues, _ = _detach(
        [
            (1, _multi(_rect(0, 0, 10, 5), _rect(20, 0, 20.1, 0.1))),
            (2, _multi(_rect(40, 0, 50, 10), _rect(20.1, 0, 22, 2))),
        ],
        [
            (1, 1, "POLYGON" + _rect(0, 0, 30, 10)),
            (2, 1, "POLYGON" + _rect(20, 0, 50, 10)),
        ],
        original=[
            (1, "POLYGON" + _rect(0, 0, 10, 5)),
            (2, "POLYGON" + _rect(40, 0, 50, 10)),
        ],
    )
    assert count == 1
    assert out == {1: 1, 2: 2}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


def test_unattached_merge_is_cancelled_and_reported():
    gap = -SNAP_TOLERANCE / 2
    near = (
        f"POLYGON(({-5 * S} 0, {gap} 0, {gap} {10 * S}, {-5 * S} {10 * S}, {-5 * S} 0))"
    )
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, near)],
        [(1, 1, PRE_1), (2, 1, near)],
    )
    assert count == 0
    assert out == {1: 2, 2: 1}
    assert issues == [(1, 2, "kept: merge did not attach", False)]


def test_genuine_multipart_input_is_left_unchanged():
    two = _multi(_rect(0, 0, 1, 1), _rect(5, 0, 6, 1))
    count, out, issues, _ = _detach([(1, two)], [(1, 1, two)])
    assert (count, out, issues) == (0, {1: 2}, [])


def test_source_island_fused_by_extension_is_kept_and_reported():
    island = _rect(20.5, 0, 21, 0.5)
    right = "POLYGON" + _rect(21, 0, 22, 1)
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, island)), (2, right)],
        [(1, 1, "POLYGON" + _rect(0, 0, 21, 10)), (2, 1, right)],
        original=[(1, _multi(MAIN_1, island)), (2, right)],
    )
    assert (count, out) == (0, {1: 2, 2: 1})
    assert issues == [(1, 2, "kept: matches original shape", False)]


def test_extension_only_sliver_merges_into_edge_neighbour():
    count, out, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
        original=[(1, "POLYGON" + MAIN_1), (2, LEFT_2)],
    )
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


def test_large_area_detached_piece_under_ratio_merges():
    main, piece = _rect(0, 0, 10, 8, scale=1), _rect(0, 9, 0.05, 9.1, scale=1)
    left = "POLYGON" + _rect(-5, 0, 0, 10, scale=1)
    count, out, issues, _ = _detach(
        [(1, _multi(main, piece)), (2, left)],
        [(1, 1, "POLYGON" + _rect(0, 0, 10, 10, scale=1)), (2, 1, left)],
        original=[(1, "POLYGON" + _rect(0, 0, 10, 10, scale=1)), (2, left)],
    )
    assert count == 1
    assert out == {1: 1, 2: 1}
    assert issues == [(1, 2, "merged into neighbouring polygon", True)]


M = 1 / 111320  # one metre in degrees, near the equator


def _wedge(conn, a_arm, *, b_len_m=1000, far=0.02):
    """Write units A (fid 1) and B sharing y=0 up to x=0, then A follows `a_arm` (m)."""
    arm = ", ".join(f"{x * M} {y * M}" for x, y in a_arm)
    end = a_arm[-1][0] * M
    a = f"POLYGON((-{far} 0, 0 0, {arm}, {end} {far}, -{far} {far}, -{far} 0))"
    b_end = b_len_m * M
    b = f"POLYGON((-{far} 0, 0 0, {b_end} 0, {b_end} -{far}, -{far} -{far}, -{far} 0))"
    conn.execute(f"""
        CREATE OR REPLACE TABLE units AS
        SELECT 1 AS fid, ST_GeomFromText('{a}') AS geom
        UNION ALL SELECT 2, ST_GeomFromText('{b}')
    """)


@pytest.mark.parametrize("mouth_m", [0.1, 5.0])
def test_deep_wedge_is_detected_and_closed(mouth_m):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(600, mouth_m)])
        assert detect_notches(conn, "units", "notches") == 1
        assert conn.execute("SELECT unit_a, unit_b FROM notches").fetchall() == [(1, 2)]
        assert close_notches(conn, "units") == 1
        assert detect_notches(conn, "units", "after") == 0
        assert not has_invalid_edges(conn, "units")


def test_vertex_near_segment_midpoint_is_closed():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(500, 0.085), (1000, 0)], b_len_m=1000)
        assert detect_notches(conn, "units", "notches") == 1
        close_notches(conn, "units")
        assert detect_notches(conn, "units", "after") == 0
        assert not has_invalid_edges(conn, "units")


def test_gap_enclosed_by_closing_the_tip_is_filled():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(300, 30), (600, 0.1)])
        assert not has_gaps(conn, "units", gap_maximum_width=1)
        assert close_notches(conn, "units") == 1
        assert not has_gaps(conn, "units", gap_maximum_width=1)
        assert not has_invalid_edges(conn, "units")


def test_close_notches_leaves_geometry_outside_the_window_unchanged():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(600, 0.1)])
        conn.execute("CREATE TABLE before AS SELECT * FROM units")
        close_notches(conn, "units")
        box = "ST_MakeEnvelope(-0.003, -0.003, 0.009, 0.003)"
        same = conn.execute(f"""
            SELECT bool_and(ST_Equals(ST_Difference(u.geom, {box}),
                                      ST_Difference(b.geom, {box})))
            FROM units u JOIN before b USING (fid)
        """).fetchone()[0]
        assert same


def test_close_notches_keeps_a_vertex_shared_with_a_third_unit():
    """A's notch with B starts at a vertex A shares with C, which must stay put."""

    def poly(pts):
        return "POLYGON((" + ", ".join(f"{x * M} {y * M}" for x, y in pts) + "))"

    a = [(-300, 300), (0, 5), (600, 0.1), (600, 2000), (-300, 2000), (-300, 300)]
    b = [(-2000, 0), (0, 0), (1000, 0), (1000, -2000), (-2000, -2000), (-2000, 0)]
    c = [(-2000, 0), (0, 0), (0, 5), (-300, 300), (-300, 2000), (-2000, 2000)]
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""
            CREATE TABLE units AS
            SELECT 1 AS fid, ST_GeomFromText('{poly(a)}') AS geom
            UNION ALL SELECT 2, ST_GeomFromText('{poly(b)}')
            UNION ALL SELECT 3, ST_GeomFromText('{poly([*c, c[0]])}')
        """)
        assert close_notches(conn, "units") == 1
        assert not has_gaps(conn, "units", gap_maximum_width=0)
        overlap = conn.execute("""
            SELECT ST_Area(ST_Intersection(a.geom, c.geom))
            FROM units a, units c WHERE a.fid = 1 AND c.fid = 3
        """).fetchone()[0]
        assert overlap == pytest.approx(0, abs=1e-12)


# A clipped Myanmar admin4 pair whose closed union leaves a line spur on unit 2.
_SPUR_A = [
    (98.0083609620001, 23.47019279700004),
    (98.00708554200008, 23.47631481100018),
    (98.00494909200751, 23.47960850512211),
    (98.01165975643754, 23.47960850512211),
    (98.01165975643754, 23.466874621385763),
    (98.009052248369, 23.466874621385763),
    (98.0083609620001, 23.47019279700004),
]
_SPUR_B = [
    (98.00708554200008, 23.47631481100018),
    (98.00836099500003, 23.47019278400012),
    (98.0083609620001, 23.47019279700004),
    (98.00617347200011, 23.47104705700002),
    (98.00424826600016, 23.472747559000027),
    (98.00379184787815, 23.47302665583293),
    (98.00379184787815, 23.47960850512211),
    (98.00494909200751, 23.47960850512211),
    (98.00708554200008, 23.47631481100018),
]


def test_close_notches_keeps_only_polygons():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        wkt = [
            "POLYGON((" + ", ".join(f"{x} {y}" for x, y in r) + "))"
            for r in (_SPUR_A, _SPUR_B)
        ]
        conn.execute(
            "CREATE TABLE units AS SELECT * FROM (VALUES (1, ST_GeomFromText(?)), "
            "(2, ST_GeomFromText(?))) t(fid, geom)",
            wkt,
        )
        assert close_notches(conn, "units") == 1
        types = conn.execute(
            "SELECT DISTINCT ST_GeometryType(geom) FROM units"
        ).fetchall()
        assert {t for (t,) in types} <= {"POLYGON", "MULTIPOLYGON"}


@pytest.mark.parametrize(("rise_m", "notches"), [(15, 1), (22, 0)])
def test_open_wedge_is_a_notch_only_within_the_move_limit(rise_m, notches):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(200, rise_m)], b_len_m=150)
        assert detect_notches(conn, "units", "notches") == notches
        close_notches(conn, "units")
        assert detect_notches(conn, "units", "after") == 0


def test_wide_wedge_and_t_junction_are_not_notches():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _wedge(conn, [(600, 100)])
        assert detect_notches(conn, "units", "notches") == 0
        conn.execute("""
            CREATE OR REPLACE TABLE units AS
            SELECT 1 AS fid,
                   ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))') AS geom
            UNION ALL SELECT 2, ST_GeomFromText('POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))')
            UNION ALL SELECT 3, ST_GeomFromText('POLYGON((0 1, 2 1, 2 2, 0 2, 0 1))')
        """)
        assert detect_notches(conn, "units", "notches") == 0
        assert close_notches(conn, "units") == 0
