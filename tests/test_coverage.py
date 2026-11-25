"""Unit tests for core/coverage.py's width-aware gap and micro-polygon checking."""

from pathlib import Path

import duckdb
import pytest

from topo_tools.core import coverage
from topo_tools.core.constants import INVALID_EDGE_BATCH_SECONDS, SNAP_TOLERANCE
from topo_tools.core.coverage import (
    check_no_overlaps,
    check_valid_topology,
    close_notches,
    count_gaps,
    coverage_clean,
    coverage_clean_rows,
    detect_notches,
    finalize_geometry,
    has_gaps,
    has_invalid_edges,
    has_micro_polygons,
    invalid_edge_issues,
    merge_detached_parts,
    merge_micro_polygons,
    merge_sliver_gaps,
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
        coverage_clean(conn, "synth", "out", fids=None, issues_table="micro")
        rows = conn.execute("SELECT fid, ST_IsEmpty(geom) FROM out ORDER BY fid")
        assert rows.fetchall() == [(1, False), (2, False)]
        assert conn.execute("SELECT unit_a, unit_b FROM micro").fetchall() == [(3, 2)]


# A clip sliver just over SNAP_TOLERANCE wide, which the clean's snap narrows under it.
NEAR_MICRO_ROWS = [
    (
        1,
        (
            "POLYGON ((-127.40594181089371 50.01134009011129, "
            "-127.405941787 50.011340097, "
            "-127.40594179356547 50.011340112175425, -127.4059365680061 50.01135, "
            "-127.40593 50.01135, -127.40593 50.011329999999994, "
            "-127.40595 50.011329999999994, -127.40595 50.01133265649202, "
            "-127.40594181089371 50.01134009011129))"
        ),
    ),
    (
        2,
        (
            "POLYGON ((-127.40594181089371 50.01134009011129, -127.4059418 50.0113401, "
            "-127.40594179356547 50.011340112175425, -127.405941787 50.011340097, "
            "-127.40594181089371 50.01134009011129))"
        ),
    ),
]


def test_coverage_clean_merges_a_part_its_snap_narrows_to_micro_width():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        values = ", ".join(f"({fid}, '{wkt}')" for fid, wkt in NEAR_MICRO_ROWS)
        conn.execute(
            "CREATE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom "
            f"FROM (VALUES {values}) v(fid, wkt)"
        )
        assert not has_micro_polygons(conn, "synth")
        coverage_clean(conn, "synth", "out", fids=None, issues_table="micro")
        assert not has_micro_polygons(conn, "out")
        assert conn.execute("SELECT fid FROM out").fetchall() == [(1,)]
        issues = conn.execute("SELECT key, unit_a, unit_b FROM micro").fetchall()
        assert issues == [("micro-polygon-1", 2, 1)]


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


def _detach(clipped, pre, original=None, *, owned=False, source_files=None):
    """Run merge_detached_parts on (fid, wkt) rows; return count/out/issues/conn."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")

    def load(table, rows, cols):
        values = ", ".join(
            "(" + ", ".join("NULL" if v is None else repr(v) for v in r) + ")"
            for r in rows
        )
        conn.execute(
            f"CREATE TABLE {table} AS SELECT * EXCLUDE (wkt), "
            f"ST_GeomFromText(wkt) AS geom FROM (VALUES {values}) v({cols}, wkt)"
        )

    load("clipped", clipped, "fid")
    if source_files:
        conn.execute("ALTER TABLE clipped ADD COLUMN source_file VARCHAR")
        for fid, path in source_files.items():
            conn.execute(f"UPDATE clipped SET source_file = '{path}' WHERE fid = {fid}")
    load("pre", pre, "fid, overlay_fid")
    if original is not None:
        cols = "fid, input_fid, source_file" if owned else "fid"
        load("original", original, cols)
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
        original_owner="input_fid" if owned else None,
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


@pytest.mark.parametrize(
    ("lobe_owner", "reason"),
    [(1, "kept: matches original shape"), (99, "merged into neighbouring polygon")],
)
def test_detached_piece_follows_only_its_own_original(lobe_owner, reason):
    ring = [(0, 0), (10, 0), (10, 8), (4e-4, 8), (4e-4, 9), (0.05, 9), (0.05, 9.1)]
    ring += [(0, 9.1), (0, 0)]
    lobe = "POLYGON((" + ", ".join(f"{x * S} {y * S}" for x, y in ring) + "))"
    _, _, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
        original=[(1, lobe_owner, None, lobe), (2, 2, None, LEFT_2)],
        owned=True,
    )
    assert [r[2] for r in issues] == [reason]


@pytest.mark.parametrize(
    ("lobe_file", "reason"),
    [("a", "kept: matches original shape"), ("b", "merged into neighbouring polygon")],
)
def test_unkeyed_original_follows_only_its_paired_file(lobe_file, reason):
    ring = [(0, 0), (10, 0), (10, 8), (4e-4, 8), (4e-4, 9), (0.05, 9), (0.05, 9.1)]
    ring += [(0, 9.1), (0, 0)]
    lobe = "POLYGON((" + ", ".join(f"{x * S} {y * S}" for x, y in ring) + "))"
    _, _, issues, _ = _detach(
        [(1, _multi(MAIN_1, PIECE_1)), (2, LEFT_2)],
        [(1, 1, PRE_1), (2, 1, LEFT_2)],
        original=[(1, None, lobe_file, lobe), (2, 2, None, LEFT_2)],
        owned=True,
        source_files={1: "a", 2: "b"},
    )
    assert [r[2] for r in issues] == [reason]


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


def test_close_notches_survives_slivers_that_pinch_on_their_own_edge():
    fixture = Path(__file__).parent / "fixtures" / "sxm_maf_notch.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""
            CREATE TABLE units AS
            SELECT row_number() OVER (ORDER BY adm0_id DESC) AS fid, geometry AS geom
            FROM '{fixture}' ORDER BY fid
        """)
        assert close_notches(conn, "units") == 1
        assert detect_notches(conn, "units", "after") == 0
        valid, types = conn.execute(
            "SELECT bool_and(ST_IsValid(geom)), list(DISTINCT ST_GeometryType(geom)) "
            "FROM units"
        ).fetchall()[0]
        assert valid
        assert set(types) <= {"POLYGON", "MULTIPOLYGON"}


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


def _write_rows(conn, rows):
    """Table synth (fid, grp, geom) from (fid, grp, wkt) rows."""
    values = ", ".join(f"({f}, '{g}', ST_GeomFromText('{w}'))" for f, g, w in rows)
    conn.execute(
        "CREATE OR REPLACE TABLE synth AS "
        f"SELECT * FROM (VALUES {values}) t(fid, grp, geom)"
    )


_SQUARE = "POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))"
_X = 2 - SNAP_TOLERANCE / 2
_SLIVER = f"POLYGON(({_X} 0, 4 0, 4 2, {_X} 2, {_X} 0))"


@pytest.mark.parametrize(
    ("other", "overlaps"),
    [
        ("POLYGON((1 0, 3 0, 3 2, 1 2, 1 0))", True),
        ("POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", True),
        ("POLYGON((2 0, 4 0, 4 2, 2 2, 2 0))", False),
        (_SLIVER, False),
    ],
    ids=["partial", "contained", "touching", "sub-tolerance-sliver"],
)
def test_check_no_overlaps(other, overlaps):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_rows(conn, [(1, "a", _SQUARE), (2, "b", other)])
        if overlaps:
            with pytest.raises(ValueError, match="1 fid pair"):
                check_no_overlaps(conn, "synth")
        else:
            check_no_overlaps(conn, "synth")
        assert not conn.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'synth_tmp1'"
        ).fetchone()[0]


def test_check_no_overlaps_ignores_an_empty_intersection():
    # GEOS calls these interiors intersecting, yet their intersection has no polygon.
    left = (
        "POLYGON((1.5741966 9.1399968, 1.5629185 9.1723015, "
        "1.5825720526271088 9.1289799834375, 1.5741966 9.1399968))"
    )
    right = (
        "POLYGON((1.598633 9.1078539, 1.5214677 9.1953353, "
        "1.5741966 9.1399968, 1.598633 9.1078539))"
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_rows(conn, [(1, "a", left), (2, "b", right)])
        check_no_overlaps(conn, "synth")


def test_check_no_overlaps_ignores_overlap_within_one_group():
    inner = "POLYGON((1 0, 3 0, 3 2, 1 2, 1 0))"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_rows(conn, [(1, "a", _SQUARE), (2, "a", inner)])
        check_no_overlaps(conn, "synth", group_column="grp")
        _write_rows(conn, [(1, "a", _SQUARE), (2, "b", inner)])
        with pytest.raises(ValueError, match="a / b"):
            check_no_overlaps(conn, "synth", group_column="grp")


def _stacked(conn, units):
    """Write `units` (fid, WKT) pairs to table `units`."""
    rows = " UNION ALL ".join(
        f"SELECT {fid} AS fid, ST_GeomFromText('{wkt}') AS geom" for fid, wkt in units
    )
    conn.execute(f"CREATE OR REPLACE TABLE units AS {rows}")


def test_sliver_between_two_units_is_not_an_invalid_edge():
    d = SNAP_TOLERANCE / 2
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _stacked(
            conn,
            [
                (1, f"POLYGON((0 {d}, 1 {d}, 1 1, 0 1, 0 {d}))"),
                (2, f"POLYGON((0 0, 1 0, 1 {d}, 0 {d}, 0 0))"),
                (3, "POLYGON((0 -1, 1 -1, 1 0, 0 0, 0 -1))"),
            ],
        )
        assert not has_invalid_edges(conn, "units")
        assert invalid_edge_issues(conn, "units", "issues") == 0


def test_overlap_beside_a_matched_unit_is_an_invalid_edge():
    d = SNAP_TOLERANCE / 2
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _stacked(
            conn,
            [
                (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
                (2, f"POLYGON((0 -1, 1 -1, 1 {d}, 0 {d}, 0 -1))"),
                (3, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
            ],
        )
        assert has_invalid_edges(conn, "units")
        pairs = [(1, 2), (2, 3)]
        assert invalid_edge_issues(conn, "units", "issues") == len(pairs)
        rows = conn.execute(
            "SELECT unit_a, unit_b, ST_Length(geom) FROM issues ORDER BY 1, 2"
        ).fetchall()
        assert [(a, b) for a, b, _ in rows] == pairs
        # Each row holds only its own pair's edges: 1's edge shared with 3 is left out.
        assert [n for _, _, n in rows] == pytest.approx([5, 3])


def _wkt_ring(points):
    s = 1e-3
    return "(" + ", ".join(f"{x * s} {y * s}" for x, y in [*points, points[0]]) + ")"


# Unit 1's hole touches its shell at (0, 0); unit 3 touches that vertex from outside.
_PINCH_SHELL = [(0, 0), (15, 190), (90, 190), (300, 150), (300, -800), (-700, -800)]
_PINCH_HOLE = [(0, 0), (130, -65), (220, -5), (205, 20), (140, -13)]
_PINCH_COAST = [(0, 0), (-50, 100), (-20, 600), (100, 600), (90, 190), (15, 190)]


def _pinch_units(conn, outside, fids=(1, 2, 3)):
    _stacked(
        conn,
        [
            (fids[0], f"POLYGON({_wkt_ring(_PINCH_SHELL)}, {_wkt_ring(_PINCH_HOLE)})"),
            (fids[1], f"POLYGON({_wkt_ring(_PINCH_HOLE)})"),
            (fids[2], f"POLYGON({_wkt_ring(outside)})"),
        ],
    )


@pytest.mark.parametrize("fids", [(1, 2, 3), (3, 2, 1)])
def test_edge_from_hole_touching_shell_vertex_is_not_an_invalid_edge(fids):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _pinch_units(conn, _PINCH_COAST, fids)
        flagged = conn.execute(
            "SELECT ST_Length(ST_CoverageInvalidEdges_Agg(geom, 0)) FROM units"
        ).fetchall()[0][0]
        assert flagged > 0, "GEOS no longer misflags the coast edge"
        assert not has_invalid_edges(conn, "units")
        assert invalid_edge_issues(conn, "units", "issues") == 0


@pytest.mark.parametrize(
    "outside",
    [
        # Runs back along unit 1's coast, 5e-9 degrees off at the far end.
        [
            (0, 0),
            (-700, -799.999995),
            (-900, -800),
            (-900, 600),
            (100, 600),
            (90, 190),
            (15, 190),
        ],
        # Chord from the pinch vertex across unit 1's interior.
        [(0, 0), (300, 150), (15, 190)],
    ],
    ids=["near-parallel", "chord-overlap"],
)
def test_real_defect_at_hole_touching_shell_vertex_is_an_invalid_edge(outside):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _pinch_units(conn, outside)
        assert has_invalid_edges(conn, "units")
        assert invalid_edge_issues(conn, "units", "issues") == 1


def test_hole_within_snap_tolerance_of_a_matched_edge_is_an_invalid_edge():
    d = SNAP_TOLERANCE / 2
    hole = f"(0.2 {d}, 0.2 0.5, 0.8 0.5, 0.8 {d}, 0.2 {d})"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _stacked(
            conn,
            [
                (1, "POLYGON((0 -1, 1 -1, 1 0, 0 0, 0 -1))"),
                (2, f"POLYGON((0 0, 1 0, 1 1, 0 1, 0 0), {hole})"),
            ],
        )
        assert has_invalid_edges(conn, "units")
        assert invalid_edge_issues(conn, "units", "issues") == 1


def _overlap_beside_matched_unit(conn):
    d = SNAP_TOLERANCE / 2
    _stacked(
        conn,
        [
            (1, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
            (2, f"POLYGON((0 -1, 1 -1, 1 {d}, 0 {d}, 0 -1))"),
            (3, "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))"),
        ],
    )


def test_stopped_invalid_edge_check_reports_pairs_unchecked(monkeypatch):
    monkeypatch.setattr(coverage, "_run_bounded", lambda *_: False)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _overlap_beside_matched_unit(conn)
        invalid_edge_issues(conn, "units", "issues")
        reasons = conn.execute("SELECT DISTINCT reason FROM issues").fetchall()
        assert [r for (r,) in reasons] == [
            f"check stopped after {INVALID_EDGE_BATCH_SECONDS:g}s; edges left unchecked"
        ]


def test_stopped_batch_keeps_its_pairs_beside_checked_ones(monkeypatch):
    calls = []
    run_bounded = coverage._run_bounded  # noqa: SLF001

    def stop_first(conn, sql, seconds):
        calls.append(sql)
        return len(calls) > 1 and run_bounded(conn, sql, seconds)

    monkeypatch.setattr(coverage, "CLIP_PAIR_VERTEX_BUDGET", 1)
    monkeypatch.setattr(coverage, "_run_bounded", stop_first)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _overlap_beside_matched_unit(conn)
        invalid_edge_issues(conn, "units", "issues")
        rows = conn.execute(
            "SELECT unit_a, unit_b, reason LIKE 'check stopped%' "
            "FROM issues ORDER BY 1, 2"
        ).fetchall()
        assert len(calls) > 1
        assert [stopped for *_, stopped in rows] == [True] + [False] * (len(rows) - 1)
        assert {(a, b) for a, b, _ in rows} >= {(1, 2), (2, 3)}


LEFT = "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"
# The right unit's west edge bends 0.02 east, leaving a needle gap against LEFT.
RIGHT_NEEDLE = "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0.8, 1.02 0.5, 1 0.2, 1 0))"


def _strip_hole(points_per_side):
    """Return a unit with a 0.6 x 0.02 hole, each long side drawn with n points."""
    xs = [0.2 + 0.6 * i / (points_per_side - 1) for i in range(points_per_side)]
    ring = [f"{x} 0.49" for x in xs] + [f"{x} 0.51" for x in reversed(xs)]
    return f"POLYGON((0 0, 1 0, 1 1, 0 1, 0 0), ({', '.join([*ring, ring[0]])}))"


def _sliver_table(conn, rows):
    conn.execute("INSTALL spatial; LOAD spatial;")
    values = ", ".join(f"({fid}, '{wkt}')" for fid, wkt in rows)
    conn.execute(
        "CREATE OR REPLACE TABLE synth AS SELECT fid, ST_GeomFromText(wkt) AS geom "
        f"FROM (VALUES {values}) v(fid, wkt)"
    )


def test_merge_sliver_gaps_merges_needle_into_longest_border_neighbour():
    with duckdb.connect() as conn:
        _sliver_table(conn, [(1, LEFT), (2, RIGHT_NEEDLE)])
        assert merge_sliver_gaps(conn, "synth", issues_table="iss", coverage=False)
        assert not has_gaps(conn, "synth", gap_maximum_width=0)
        area = conn.execute("SELECT ST_Area(geom) FROM synth WHERE fid = 2")
        assert area.fetchall()[0][0] == pytest.approx(1.0)
        issues = conn.execute("SELECT key, kind, unit_a, fixed FROM iss")
        assert issues.fetchall() == [("sliver-gap-1", "sliver-gap", 2, True)]


@pytest.mark.parametrize(("points_per_side", "merged"), [(2, 1), (121, 0)])
def test_merge_sliver_gaps_compares_width_to_vertex_spacing(points_per_side, merged):
    """The same thin hole is a sliver drawn with long edges, a feature drawn finely."""
    with duckdb.connect() as conn:
        _sliver_table(conn, [(1, _strip_hole(points_per_side))])
        count = merge_sliver_gaps(conn, "synth", issues_table="iss", coverage=False)
        assert count == merged
        assert has_gaps(conn, "synth", gap_maximum_width=0) == (not merged)


def test_merge_sliver_gaps_leaves_enclave_inside_hole():
    island = "POLYGON((0.25 0.495, 0.75 0.495, 0.75 0.505, 0.25 0.505, 0.25 0.495))"
    with duckdb.connect() as conn:
        _sliver_table(conn, [(1, _strip_hole(2)), (2, island)])
        assert merge_sliver_gaps(conn, "synth", issues_table="iss", coverage=False)
        assert not has_gaps(conn, "synth", gap_maximum_width=0)
        rows = conn.execute("""
            SELECT ST_Area(ST_Intersection(a.geom, b.geom)), ST_Area(b.geom)
            FROM synth a, synth b WHERE a.fid = 1 AND b.fid = 2
        """).fetchall()
        assert rows[0][0] == pytest.approx(0, abs=1e-12)
        assert rows[0][1] == pytest.approx(0.005)


def test_gap_geometries_counts_hole_inside_enclave_once():
    with duckdb.connect() as conn:
        _sliver_table(
            conn,
            [
                (
                    1,
                    "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 8 2, 8 8, 2 8, 2 2))",
                ),
                (2, "POLYGON((3 3, 7 3, 7 7, 3 7, 3 3), (4 4, 6 4, 6 6, 4 6, 4 4))"),
            ],
        )
        gaps = coverage.gap_geometries_sql("synth")
        areas = conn.execute(f"SELECT ST_Area(geom) FROM {gaps} ORDER BY 1").fetchall()
        assert [a for (a,) in areas] == pytest.approx([4, 20])


def test_merge_sliver_gaps_counts_only_merged_gaps():
    with duckdb.connect() as conn:
        _sliver_table(conn, [(1, LEFT), (2, _strip_hole(2))])
        conn.execute("UPDATE synth SET geom = ST_Translate(geom, 5, 0) WHERE fid = 2")
        count = merge_sliver_gaps(
            conn, "synth", issues_table="iss", coverage=False, where="fid = 1"
        )
        assert count == 0
        assert has_gaps(conn, "synth", gap_maximum_width=0)


def test_merge_sliver_gaps_keeps_compact_hole():
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _write_polygon_with_hole(conn, hole_width=0.02)
        assert not merge_sliver_gaps(conn, "synth", issues_table="iss", coverage=False)
        assert conn.execute("SELECT count(*) FROM iss").fetchall()[0][0] == 0


@pytest.mark.parametrize("merge_slivers", [True, False])
def test_coverage_clean_merges_sliver_gaps_unless_disabled(merge_slivers):
    with duckdb.connect() as conn:
        _sliver_table(conn, [(1, LEFT), (2, RIGHT_NEEDLE)])
        coverage_clean(
            conn,
            "synth",
            "out",
            fids=None,
            issues_table="iss",
            merge_slivers=merge_slivers,
        )
        assert has_gaps(conn, "out", gap_maximum_width=0) != merge_slivers
        kinds = conn.execute("SELECT kind FROM iss").fetchall()
        assert kinds == ([("sliver-gap",)] if merge_slivers else [])


@pytest.mark.parametrize(
    ("rows", "slivers", "held_area"),
    [
        # A held row filling the hole leaves no gap to merge.
        (
            [
                (1, _strip_hole(2)),
                (2, "POLYGON((0.2 0.49, 0.8 0.49, 0.8 0.51, 0.2 0.51, 0.2 0.49))"),
            ],
            0,
            0.012,
        ),
        # A held row elsewhere still lets the needle merge into a cleaned row.
        (
            [(1, LEFT), (3, RIGHT_NEEDLE), (2, "POLYGON((5 5, 6 5, 6 6, 5 6, 5 5))")],
            1,
            1.0,
        ),
    ],
)
def test_coverage_clean_rows_counts_held_rows_as_filled(rows, slivers, held_area):
    with duckdb.connect() as conn:
        _sliver_table(conn, rows)
        coverage_clean_rows(conn, "synth", "fid != 2", issues_table="iss")
        kinds = conn.execute("SELECT kind FROM iss WHERE kind = 'sliver-gap'")
        assert len(kinds.fetchall()) == slivers
        held = conn.execute("SELECT ST_Area(geom) FROM synth WHERE fid = 2")
        assert held.fetchall()[0][0] == pytest.approx(held_area)


_OVERLAPPING = "POLYGON((0.5 0, 2 0, 2 1, 0.5 1, 0.5 0))"


def _off_grid_square():
    conn = duckdb.connect()
    conn.execute("LOAD spatial")
    conn.execute("""
        CREATE TABLE t AS SELECT 1 AS fid,
        ST_GeomFromText('POLYGON((0 0, 1.00000000033 0, 1 1, 0 1, 0 0))') AS geom
    """)
    return conn


def test_finalize_geometry_coverage_raises_on_overlap():
    conn = _off_grid_square()
    conn.execute(f"INSERT INTO t VALUES (2, ST_GeomFromText('{_OVERLAPPING}'))")
    with pytest.raises(RuntimeError, match="INVALID_EDGES"):
        finalize_geometry(conn, "t", validate="coverage")


def test_finalize_geometry_issues_writes_rows_without_raising():
    conn = _off_grid_square()
    conn.execute(f"INSERT INTO t VALUES (2, ST_GeomFromText('{_OVERLAPPING}'))")
    finalize_geometry(conn, "t", validate="issues", issues_table="t_invalid")
    assert conn.execute("SELECT count(*) FROM t_invalid").fetchall()[0][0] > 0


@pytest.mark.parametrize("validate", ["coverage", "issues"])
def test_finalize_geometry_raises_on_invalid_geometry(validate):
    conn = _off_grid_square()
    bowtie = "POLYGON((3 0, 4 1, 4 0, 3 1, 3 0))"
    conn.execute(f"INSERT INTO t VALUES (2, ST_GeomFromText('{bowtie}'))")
    with pytest.raises(RuntimeError, match="INVALID_GEOMETRY"):
        finalize_geometry(conn, "t", validate=validate, issues_table="t_invalid")


def test_finalize_geometry_skips_gap_check_without_width():
    conn = duckdb.connect()
    conn.execute("LOAD spatial")
    _write_polygon_with_hole(conn, hole_width=0.5)
    finalize_geometry(conn, "synth", validate="coverage", gap_maximum_width=None)
    with pytest.raises(RuntimeError, match="GAPS"):
        finalize_geometry(conn, "synth", validate="coverage", gap_maximum_width=1)


def test_finalize_geometry_drops_empty_rows():
    conn = _off_grid_square()
    thin = "POLYGON((5 5, 6 5, 6 5.0000000004, 5 5.0000000004, 5 5))"
    conn.execute(f"""
        INSERT INTO t VALUES (2, ST_GeomFromText('{thin}')), (3, NULL),
        (4, ST_GeomFromText('POLYGON EMPTY'))
    """)
    finalize_geometry(conn, "t", validate="none")
    kept = conn.execute("SELECT fid FROM t ORDER BY fid").fetchall()
    assert kept == [(1,), (2,), (3,)]
    issues = conn.execute(
        "SELECT kind, unit_a, reason, fixed FROM t_empty ORDER BY unit_a"
    ).fetchall()
    assert issues == [("empty-geometry", 4, "empty geometry", False)]


def test_finalize_geometry_labels_dropped_rows():
    conn = _off_grid_square()
    conn.execute("ALTER TABLE t ADD COLUMN code VARCHAR")
    conn.execute("INSERT INTO t VALUES (2, ST_GeomFromText('POLYGON EMPTY'), 'A1')")
    finalize_geometry(conn, "t", validate="none", label_columns=("code",))
    reason = conn.execute("SELECT reason FROM t_empty").fetchall()[0][0]
    assert reason == "empty geometry (code=A1)"


def test_has_invalid_edges_skips_geometry_already_checked_valid(monkeypatch):
    right = "(2, 'POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))')"
    rows = f"(1, 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))'), {right}"
    bad = f"(1, 'POLYGON((0 0, 1.5 0, 1.5 1, 0 1, 0 0))'), {right}"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        for table, values in (("a", rows), ("b", rows), ("c", bad), ("d", bad)):
            conn.execute(f"""
                CREATE TABLE {table} AS SELECT fid, ST_GeomFromText(w) AS geom
                FROM (VALUES {values}) t(fid, w) ORDER BY fid DESC
            """)
        assert not has_invalid_edges(conn, "a")
        assert has_invalid_edges(conn, "c")
        calls = []
        real = coverage._invalid_part_pairs  # noqa: SLF001
        monkeypatch.setattr(
            coverage,
            "_invalid_part_pairs",
            lambda *a, **k: calls.append(1) or real(*a, **k),
        )
        assert not has_invalid_edges(conn, "b")
        assert has_invalid_edges(conn, "d")
    assert len(calls) == 1
