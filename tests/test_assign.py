"""Direct core.assign tests: code-join precedence, fallback, and default schema."""

import duckdb
import pytest

from topo_tools.core.assign import (
    assign_many,
    assign_one,
    fill_unmatched_overlays,
    input_bbox_extent,
    prepare_overlay_tiles,
    resolve_column_selection,
)

# Two disjoint overlays, far enough apart that an input spans the empty gap
# between them without touching anything else.
_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
    (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
]


def _connect_with_overlays():
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'), '{code}')"
        for fid, wkt, code in _OVERLAY_WKT
    )
    conn.execute(f"""--sql
        CREATE TABLE t_overlay_01 AS
        SELECT * FROM (VALUES {values}) AS v(fid, geom, pcode)
    """)
    return conn


def test_assign_many_default_schema_unchanged():
    """No match columns: `_02_assign` stays exactly (input_fid, overlay_fid)."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText(
                'POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'
            ), 'fileA')
        ) AS v(fid, geom, source_file)
    """)
    assign_many(conn, "t")
    cols = [d[0] for d in conn.execute('SELECT * FROM "t_02_assign"').description]
    assert cols == ["input_fid", "overlay_fid"]


def test_assign_many_code_join_precedence_and_fallback():
    """Code wins on disagreement, falls back to spatial when unmatched/unrelated."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            -- agrees: entirely inside P1, code also says P1
            (1, ST_GeomFromText(
                'POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'
            ), 'P1'),
            -- mismatch: mostly overlaps P1 (area 2) but touches P2 too (area
            -- 0.5); code says P2, which it does overlap, so code wins anyway
            (2, ST_GeomFromText(
                'POLYGON((1 0, 10.5 0, 10.5 1, 1 1, 1 0))'
            ), 'P2'),
            -- fallback: no parent has this code at all
            (3, ST_GeomFromText(
                'POLYGON((1.5 1.5, 2 1.5, 2 2, 1.5 2, 1.5 1.5))'
            ), 'ZZZ'),
            -- fallback: code says P2, but this child doesn't overlap P2 at all
            (4, ST_GeomFromText(
                'POLYGON((1.5 0.2, 2 0.2, 2 0.6, 1.5 0.6, 1.5 0.2))'
            ), 'P2')
        ) AS v(fid, geom, pcode)
    """)
    conn.execute("""--sql
        ALTER TABLE t_input_01 ADD COLUMN source_file VARCHAR DEFAULT 'fileA'
    """)

    assign_many(conn, "t", overlay_match_column="pcode", input_match_column="pcode")

    rows = {
        row[0]: row[1:]
        for row in conn.execute("""--sql
            SELECT input_fid, overlay_fid, assignment_method, spatial_agrees
            FROM "t_02_assign" ORDER BY input_fid
        """).fetchall()
    }
    assert rows[1] == (1, "code", True)
    assert rows[2] == (2, "code", False)
    assert rows[3] == (1, "spatial_fallback", None)
    assert rows[4] == (1, "spatial_fallback", None)

    unassigned = conn.execute('SELECT COUNT(*) FROM "t_02_unassigned"').fetchone()[0]
    assert unassigned == 0


def test_assign_many_carry_columns_populates_overlay_attributes():
    """carry_columns copies named overlay columns onto every matched input row."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText(
                'POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'
            ), 'fileA')
        ) AS v(fid, geom, source_file)
    """)
    assign_many(conn, "t", carry_columns=["pcode"])
    row = conn.execute("""--sql
        SELECT input_fid, overlay_fid, pcode FROM "t_02_assign"
    """).fetchone()
    assert row == (1, 1, "P1")


def test_input_bbox_extent_combines_input_rows():
    """Returns the combined (xmin, ymin, xmax, ymax) across every input feature row."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText('POLYGON((5 5, 6 5, 6 6, 5 6, 5 5))'))
        ) AS v(fid, geom)
    """)
    assert input_bbox_extent(conn, "t") == (0.0, 0.0, 6.0, 6.0)


def test_input_bbox_extent_none_when_empty():
    """Returns None rather than a row of NULLs for a table with zero rows."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("CREATE TABLE t_input_01 (fid BIGINT, geom GEOMETRY)")
    assert input_bbox_extent(conn, "t") is None


def test_prepare_overlay_tiles_input_bbox_filters_distant_parts():
    """A input_bbox drops overlay parts whose bbox can't overlap it."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    conn.execute("""--sql
        CREATE TABLE t_overlay_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
            (2, ST_GeomFromText(
                'POLYGON((100 100, 101 100, 101 101, 100 101, 100 100))'
            )),
            (3, ST_GeomFromText(
                'POLYGON((200 200, 201 200, 201 201, 200 201, 200 200))'
            ))
        ) AS v(fid, geom)
    """)

    prepare_overlay_tiles(conn, "t", input_bbox=(0.0, 0.0, 1.0, 1.0))
    filtered_fids = {
        row[0]
        for row in conn.execute('SELECT fid FROM "t_02_overlay_parts"').fetchall()
    }
    assert filtered_fids == {1}

    prepare_overlay_tiles(conn, "t")
    all_fids = {
        row[0]
        for row in conn.execute('SELECT fid FROM "t_02_overlay_parts"').fetchall()
    }
    assert all_fids == {1, 2, 3}


def test_assign_one_carry_columns_populates_overlay_attributes():
    """carry_columns copies named overlay columns onto every matched input row."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (10, ST_GeomFromText(
                'POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'
            ), 'fileA')
        ) AS v(fid, geom, source_file)
    """)
    assign_one(conn, "t", carry_columns=["pcode"])
    row = conn.execute("""--sql
        SELECT input_fid, overlay_fid, pcode FROM "t_02_assign"
    """).fetchone()
    assert row == (10, 1, "P1")


def test_assign_carry_columns_collision_raises():
    """A carry_columns name reserved by `_02_assign` itself raises ValueError."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'))
        ) AS v(fid, geom)
    """)
    with pytest.raises(ValueError, match="reserved assign column"):
        assign_many(conn, "t", carry_columns=["overlay_fid"])


def test_assign_carry_columns_input_schema_collision_raises():
    """A carry_columns name already present on the input layer raises ValueError."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (1, ST_GeomFromText('POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'), 'X')
        ) AS v(fid, geom, pcode)
    """)
    with pytest.raises(ValueError, match="input layer's own column"):
        assign_many(conn, "t", carry_columns=["pcode"])


def test_assign_one_code_join_precedence_and_fallback():
    """Same precedence/fallback rules, applied per source_file, one overlay each."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            -- fileA: P1 wins the spatial vote 3 to 2 but code agrees on P2,
            -- which two children also overlap, so the whole file moves to P2
            (10, ST_GeomFromText(
                'POLYGON((1 0, 10.5 0, 10.5 1, 1 1, 1 0))'
            ), 'P2', 'fileA'),
            (11, ST_GeomFromText(
                'POLYGON((1 1, 10.5 1, 10.5 2, 1 2, 1 1))'
            ), 'P2', 'fileA'),
            (12, ST_GeomFromText(
                'POLYGON((0.2 2.2, 0.8 2.2, 0.8 2.8, 0.2 2.8, 0.2 2.2))'
            ), 'P2', 'fileA'),
            -- fileC: P1 and P2 tie the spatial vote, code picks P2, so they agree
            (30, ST_GeomFromText(
                'POLYGON((1 0, 10.5 0, 10.5 1, 1 1, 1 0))'
            ), 'P2', 'fileC'),
            -- fileB: no code match at all, falls back to its spatial winner (P1)
            (20, ST_GeomFromText(
                'POLYGON((1.5 0.2, 2 0.2, 2 0.6, 1.5 0.6, 1.5 0.2))'
            ), 'ZZZZ', 'fileB')
        ) AS v(fid, geom, pcode, source_file)
    """)

    assign_one(conn, "t", overlay_match_column="pcode", input_match_column="pcode")

    rows = {
        row[0]: row[1:]
        for row in conn.execute("""--sql
            SELECT input_fid, overlay_fid, assignment_method, spatial_agrees
            FROM "t_02_assign" ORDER BY input_fid
        """).fetchall()
    }
    assert rows[10] == (2, "code", False)
    assert rows[11] == (2, "code", False)
    assert rows[12] == (2, "code", False)
    assert rows[30] == (2, "code", True)
    assert rows[20] == (1, "spatial_fallback", None)

    unassigned = conn.execute('SELECT COUNT(*) FROM "t_02_unassigned"').fetchone()[0]
    assert unassigned == 0


def test_resolve_column_selection_default_returns_all_but_always_exclude():
    conn = _connect_with_overlays()
    selected = resolve_column_selection(
        conn, "t_overlay_01", include=None, exclude=None, always_exclude=("fid", "geom")
    )
    assert selected == ["pcode"]


def test_resolve_column_selection_include_gains_always_include():
    conn = _connect_with_overlays()
    selected = resolve_column_selection(
        conn,
        "t_overlay_01",
        include=["pcode"],
        exclude=None,
        always_include=("fid", "geom"),
    )
    assert set(selected) == {"pcode", "fid", "geom"}


def test_resolve_column_selection_exclude_drops_columns():
    conn = _connect_with_overlays()
    selected = resolve_column_selection(
        conn, "t_overlay_01", include=None, exclude=["pcode"]
    )
    assert set(selected) == {"fid", "geom"}


def test_resolve_column_selection_exclude_collides_with_always_include_raises():
    conn = _connect_with_overlays()
    with pytest.raises(ValueError, match="always-included column"):
        resolve_column_selection(
            conn,
            "t_overlay_01",
            include=None,
            exclude=["fid"],
            always_include=("fid",),
        )


def test_fill_unmatched_overlays_appends_zero_input_overlay():
    """An overlay with no matched inputs carries through via its own geometry."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_02_assign AS SELECT 10 AS input_fid, 1 AS overlay_fid
    """)
    conn.execute("""--sql
        CREATE TABLE t_result AS
        SELECT 1 AS overlay_fid,
               ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))') AS geom
    """)
    fill_unmatched_overlays(
        conn,
        "t",
        carry_columns=["pcode"],
        result_table="t_result",
        overlay_snapshot_table="t_overlay_01",
    )
    overlay_fids = {
        row[0] for row in conn.execute('SELECT overlay_fid FROM "t_result"').fetchall()
    }
    assert overlay_fids == {1, 2}
    pcode = conn.execute(
        'SELECT pcode FROM "t_result" WHERE overlay_fid = 2'
    ).fetchone()[0]
    assert pcode == "P2"


def test_carry_forward_columns_no_false_positive_when_input_columns_narrowed():
    """A carried column already excluded from input_columns doesn't collide."""
    conn = _connect_with_overlays()
    conn.execute("""--sql
        CREATE TABLE t_input_01 AS SELECT * FROM (VALUES
            (10, ST_GeomFromText(
                'POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))'
            ), 'fileA', 'stale')
        ) AS v(fid, geom, source_file, pcode)
    """)
    assign_one(conn, "t", carry_columns=["pcode"], input_columns=["fid", "geom"])
    row = conn.execute('SELECT input_fid, pcode FROM "t_02_assign"').fetchone()
    assert row == (10, "P1")
