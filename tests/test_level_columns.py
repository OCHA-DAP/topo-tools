"""Unit tests for schema_map's structural level/column detection."""

import duckdb
import pytest

from topo_tools.core.schema_map._level_columns import (
    LevelColumns,
    detect_leaf_level,
    detect_level_columns,
    detect_root_level,
    group_families_by_level,
    is_level_identity_column,
    level_code_columns,
    verify_functional_cluster,
)


@pytest.mark.parametrize(
    ("name", "prefix", "anchor", "suffix", "expected"),
    [
        ("adm3_pcode", "adm", "3", "_pcode", True),
        ("adm3_name2", "adm", "3", "_pcode", True),
        ("adm4_pcode", "adm", "3", "_pcode", False),
        ("area_sqkm", "adm", "3", "_pcode", False),
        ("state_name", "", "state", "_code", True),
        ("code_state", "code_", "state", "", True),
        ("code_county", "code_", "state", "", False),
    ],
)
def test_is_level_identity_column(name, prefix, anchor, suffix, expected):
    assert is_level_identity_column(name, prefix, anchor, suffix) is expected


@pytest.fixture
def conn():
    with duckdb.connect() as connection:
        connection.execute("INSTALL spatial; LOAD spatial;")
        yield connection


def _load_two_level_table(conn) -> None:
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'A1', 'Province1', 'Alpha',
             ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('P1', 'A2', 'Province1', 'Beta',
             ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('P2', 'B1', 'Province2', 'Gamma',
             ST_GeomFromText('POLYGON((0 1,1 1,1 2,0 2,0 1))'))
        ) AS v(adm1_pcode, adm2_pcode, adm1_name, adm2_name, geom)
    """)


def test_detect_level_columns_finds_coarser_and_finer_level(conn):
    _load_two_level_table(conn)
    result = detect_level_columns(conn, "t_01")
    coarser_level, finer_level = sorted(result)
    assert set(result[coarser_level].group_by) == {"adm1_pcode", "adm1_name"}
    assert set(result[finer_level].group_by) == {"adm2_pcode", "adm2_name"}
    assert set(result[coarser_level].identity_columns) == {"adm1_pcode", "adm1_name"}
    assert set(result[finer_level].identity_columns) == {"adm2_pcode", "adm2_name"}


def test_detect_level_columns_excludes_bijective_lookalike_from_identity(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'A1', 1.5, ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('P1', 'A2', 2.5, ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('P2', 'B1', 3.5, ST_GeomFromText('POLYGON((0 1,1 1,1 2,0 2,0 1))'))
        ) AS v(adm1_pcode, adm2_pcode, area_sqkm, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    finer_level = max(result)
    assert "area_sqkm" not in result[finer_level].identity_columns


def test_name_column_ignores_digit_shaped_numeric_attributes(conn):
    """name_column must resolve to the real name, not a digit-containing DOUBLE."""
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'Alphaland', 12.5, 1.1, 2.1,
             ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('P2', 'Betaland', 34.5, 3.3, 4.4,
             ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('P3', 'Gammaland', 56.5, 5.5, 6.6,
             ST_GeomFromText('POLYGON((2 0,3 0,3 1,2 1,2 0))'))
        ) AS v(adm1_pcode, adm1_name, area_sqkm, center_lat, center_lon, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    (level,) = result
    assert result[level].name_column == "adm1_name"


def test_detect_level_columns_ignores_floating_point_embed_coincidence(conn):
    """A low-cardinality flag must not masquerade as a level via a float's digits."""
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('C0', 'C0A', 'Alpha', 1, 100.0,
             ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('C0', 'C0B', 'Beta', 1, 210.0,
             ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('C0', 'C0C', 'Gamma', 1, 310.0,
             ST_GeomFromText('POLYGON((0 1,1 1,1 2,0 2,0 1))')),
            ('C0', 'C0D', 'Delta', 2, 420.0,
             ST_GeomFromText('POLYGON((1 1,2 1,2 2,1 2,1 1))'))
        ) AS v(adm0_pcode, adm1_pcode, adm1_name, multipart, area_sqkm, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    assert len(result) == 1
    (level,) = result
    assert set(result[level].group_by) == {"adm1_pcode", "adm1_name", "area_sqkm"}


def test_detect_level_columns_finds_flat_non_embedding_finest_level(conn):
    """A flat, non-compound finest-level code is still detected, via containment."""
    adm2_children = {
        "C0A1": ["5001", "5002", "5003"],
        "C0A2": ["5004", "5005", "5006"],
        "C0B1": ["5007", "5008", "5009"],
        "C0B2": ["5010", "5011", "5012"],
    }
    adm1_of = {"C0A1": "C0A", "C0A2": "C0A", "C0B1": "C0B", "C0B2": "C0B"}
    rows = []
    i = 0
    for adm2, children in adm2_children.items():
        for adm3 in children:
            square = f"POLYGON(({i} 0,{i + 1} 0,{i + 1} 1,{i} 1,{i} 0))"
            rows.append((adm1_of[adm2], adm2, adm3, square))
            i += 1
    values = ", ".join(
        f"('{a1}', '{a2}', '{a3}', ST_GeomFromText('{g}'))" for a1, a2, a3, g in rows
    )
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode, *
        FROM (VALUES {values}) AS v(adm1_pcode, adm2_pcode, adm3_pcode, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    finest_level = max(result)
    assert result[finest_level].group_by == ["adm3_pcode"]


def test_detect_level_columns_finds_zero_padded_middle_level(conn):
    """Zero-padded codes embed only the root's, and lang1/lang2 join the root group."""
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'XY' AS adm0_pcode,
               'Country' AS adm0_name, 'en' AS lang1, 'fr' AS lang2,
               'XY' || a || '0000' AS adm1_pcode, 'Region ' || a AS adm1_name,
               'XY' || a || b || '000' AS adm2_pcode,
               'District ' || a || b AS adm2_name,
               'XY' || a || b || c || d AS adm3_pcode,
               'Commune ' || a || b || c || d AS adm3_name,
               ST_MakeEnvelope(a * 10 + b, c * 10 + d, a * 10 + b + 1, c * 10 + d + 1)
                   AS geom
        FROM range(1, 4) r1(a), range(1, 4) r2(b), range(1, 4) r3(c),
             range(1, 4) r4(d)
    """)
    result = detect_level_columns(conn, "t_01")
    group_bys = [set(result[level].group_by) for level in sorted(result)]
    assert {"adm2_pcode", "adm2_name"} in group_bys


def test_detect_level_columns_ignores_coincidentally_embedding_sparse_columns(conn):
    """Sparse, single-row audit columns must not out-chain the real hierarchy."""
    adm2_children = {
        "C0A1": ["5001", "5002", "5003"],
        "C0A2": ["5004", "5005", "5006"],
        "C0B1": ["5007", "5008", "5009"],
        "C0B2": ["5010", "5011", "5012"],
    }
    adm1_of = {"C0A1": "C0A", "C0A2": "C0A", "C0B1": "C0B", "C0B2": "C0B"}
    rows = []
    i = 0
    for adm2, children in adm2_children.items():
        for adm3 in children:
            square = f"POLYGON(({i} 0,{i + 1} 0,{i + 1} 1,{i} 1,{i} 0))"
            audit = "' '" if i == 0 else "NULL"
            rows.append((adm1_of[adm2], adm2, adm3, audit, square))
            i += 1
    values = ", ".join(
        f"('{a1}', '{a2}', '{a3}', {audit}, ST_GeomFromText('{g}'))"
        for a1, a2, a3, audit, g in rows
    )
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode, *
        FROM (VALUES {values})
            AS v(adm1_pcode, adm2_pcode, adm3_pcode, comments, geom)
    """)
    conn.execute("""--sql
        ALTER TABLE t_01 ADD COLUMN focus_id VARCHAR;
        ALTER TABLE t_01 ADD COLUMN mod_by VARCHAR;
        ALTER TABLE t_01 ADD COLUMN progres_id VARCHAR;
        UPDATE t_01 SET focus_id = comments, mod_by = comments, progres_id = comments;
    """)
    result = detect_level_columns(conn, "t_01")
    assert any("adm1_pcode" in lc.group_by for lc in result.values())
    finest_level = max(result)
    assert result[finest_level].group_by == ["adm3_pcode"]


_LAST_ROW = 11


def test_detect_level_columns_ignores_low_cardinality_date_chains(conn):
    """A fully-populated pair of date audit columns must not out-chain a real level."""
    adm1_children = {
        "C0A": ["5001", "5002", "5003", "5004", "5005", "5006"],
        "C0B": ["5007", "5008", "5009", "5010", "5011", "5012"],
    }
    rows = []
    i = 0
    for adm1, children in adm1_children.items():
        for finest in children:
            square = f"POLYGON(({i} 0,{i + 1} 0,{i + 1} 1,{i} 1,{i} 0))"
            created = "2023-04-15" if i % 2 == 0 else "2023-06-01"
            # The last row was later re-edited, breaking the otherwise-clean
            # created_date/update_date bijection into a genuine embeds edge.
            updated = "2023-07-01" if i == _LAST_ROW else created
            rows.append((adm1, f"{adm1}{finest}", created, updated, square))
            i += 1
    values = ", ".join(
        f"('{a1}', '{f}', DATE '{c}', DATE '{d}', ST_GeomFromText('{g}'))"
        for a1, f, c, d, g in rows
    )
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode, *
        FROM (VALUES {values})
            AS v(adm1_pcode, finest_pcode, created_date, update_date, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    assert any(lc.group_by == ["adm1_pcode"] for lc in result.values())
    finest_level = max(result)
    assert "finest_pcode" in result[finest_level].group_by


def _build_scattered_audit_rows(update_by_fn):
    adm1_children = {
        "C0A": ["5001", "5002", "5003", "5004", "5005", "5006"],
        "C0B": ["5007", "5008", "5009", "5010", "5011", "5012"],
    }
    rows = []
    i = 0
    for adm1, children in adm1_children.items():
        for finest in children:
            square = f"POLYGON(({i} 0,{i + 1} 0,{i + 1} 1,{i} 1,{i} 0))"
            rows.append((adm1, f"{adm1}{finest}", update_by_fn(i), square))
            i += 1
    return rows


def test_detect_level_columns_ignores_spatially_scattered_tolerance_embed(conn):
    """A tolerance-embedding but spatially scattered audit column must not chain."""
    rows = _build_scattered_audit_rows(lambda i: "ADMIN" if i % 2 == 0 else "WRITER")
    values = ", ".join(
        f"('{a1}', '{f}', '{u}', ST_GeomFromText('{g}'))" for a1, f, u, g in rows
    )
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode,
            'WRITER' AS created_user, *
        FROM (VALUES {values}) AS v(adm1_pcode, finest_pcode, update_by, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    assert not any("update_by" in lc.group_by for lc in result.values())
    assert any(lc.group_by == ["adm1_pcode"] for lc in result.values())
    finest_level = max(result)
    assert "finest_pcode" in result[finest_level].group_by


def test_detect_level_columns_ignores_spatially_scattered_root_freebie(conn):
    """An audit column with no embed at all must not chain via the root freebie."""
    rows = _build_scattered_audit_rows(lambda i: "ADMIN" if i % 2 == 0 else "WRITER")
    values = ", ".join(
        f"('{a1}', '{f}', '{u}', ST_GeomFromText('{g}'))" for a1, f, u, g in rows
    )
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode, *
        FROM (VALUES {values}) AS v(adm1_pcode, finest_pcode, update_by, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    assert not any("update_by" in lc.group_by for lc in result.values())
    assert any(lc.group_by == ["adm1_pcode"] for lc in result.values())
    finest_level = max(result)
    assert "finest_pcode" in result[finest_level].group_by


def test_detect_level_columns_word_based_anchor_both_positions(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'A1', NULL, NULL,
             ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('P1', 'A2', NULL, NULL,
             ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('P2', 'B1', NULL, NULL,
             ST_GeomFromText('POLYGON((0 1,1 1,1 2,0 2,0 1))'))
        ) AS v(state_code, county_code, state_name_alt, county_name_alt, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    state_level, county_level = sorted(result)
    assert "state_name_alt" in result[state_level].identity_columns
    assert "county_name_alt" in result[county_level].identity_columns


def test_detect_level_columns_raises_when_no_level_found(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid,
               ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))') AS geom
    """)
    with pytest.raises(ValueError, match="no admin hierarchy level detected"):
        detect_level_columns(conn, "t_01")


def test_verify_functional_cluster_raises_on_fragmenting_member(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT * FROM (VALUES
            ('P1', 'x'), ('P1', 'y'), ('P2', 'z')
        ) AS v(code, extra)
    """)
    with pytest.raises(ValueError, match="grouping by"):
        verify_functional_cluster(conn, "t_01", "code", ["code", "extra"])


def test_verify_functional_cluster_passes_on_matching_cardinality(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT * FROM (VALUES
            ('P1', 'x'), ('P1', 'x'), ('P2', 'y')
        ) AS v(code, extra)
    """)
    verify_functional_cluster(conn, "t_01", "code", ["code", "extra"])


def test_detect_root_level_digit_anchored(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0_pcode, 'Country0' AS adm0_name,
               *
        FROM (VALUES
            ('P1', 'A1'), ('P1', 'A2'), ('P2', 'B1')
        ) AS v(adm1_pcode, adm2_pcode)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    root = detect_root_level(conn, "t_01", level_columns)
    assert root is not None
    level, cols = root
    assert level == 0
    assert set(cols.identity_columns) == {"adm0_pcode", "adm0_name"}
    assert cols.group_by == []
    assert cols.is_root


def test_detect_root_level_ignores_constant_depth_column(conn):
    """A bare-named root is still found beside a constant adm_lvl column."""
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS adm0, 2 AS adm_lvl, *
        FROM (VALUES
            ('C01', 'C0101'), ('C01', 'C0102'), ('C02', 'C0201'), ('C02', 'C0202')
        ) AS v(adm1, adm2)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    root = detect_root_level(conn, "t_01", level_columns)
    assert root is not None
    assert root[0] == 0
    assert root[1].identity_columns == ["adm0"]


@pytest.mark.parametrize("with_adm0", [False, True])
def test_detect_root_level_skips_empty_finer_level(conn, with_adm0):
    """An all-NULL adm3 never becomes the root, nor hides a real adm0 root."""
    adm0 = "'C0' AS adm0_pcode, " if with_adm0 else ""
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, {adm0}*,
               NULL::VARCHAR AS adm3_pcode, NULL::VARCHAR AS adm3_name
        FROM (VALUES
            ('C01', 'C0101'), ('C01', 'C0102'), ('C02', 'C0201'), ('C02', 'C0202')
        ) AS v(adm1_pcode, adm2_pcode)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    root = detect_root_level(conn, "t_01", level_columns)
    if with_adm0:
        assert root is not None
        assert root[0] == 0
        assert root[1].identity_columns == ["adm0_pcode"]
    else:
        assert root is None


def test_level_code_columns_root_skips_next_level_name():
    """A root's code follows the next level's code, even when its name sorts first."""
    level_columns = {
        0: LevelColumns(
            group_by=[], identity_columns=["adm0", "adm0_name"], is_root=True
        ),
        1: LevelColumns(
            group_by=["adm1_name", "adm1"],
            identity_columns=["adm1_name", "adm1"],
            name_column="adm1_name",
        ),
    }
    families = {
        "": {0: "adm0", 1: "adm1"},
        "_name": {0: "adm0_name", 1: "adm1_name"},
    }
    assert level_code_columns(level_columns, families, [0, 1])[0] == "adm0"


@pytest.mark.parametrize(("root", "expected"), [("adm1", 1), ("adm0", 0)])
def test_detect_root_level_numbered_by_own_digit(conn, root, expected):
    """A constant root takes its own level number, not always 0."""
    conn.execute(f"""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, 'C0' AS {root}_pcode, *
        FROM (VALUES
            ('P1', 'A1'), ('P1', 'A2'), ('P2', 'B1')
        ) AS v(adm2_pcode, adm3_pcode)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    root_level = detect_root_level(conn, "t_01", level_columns)
    assert root_level is not None
    assert root_level[0] == expected


def test_detect_root_level_word_anchored(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid,
               'US' AS country_code, 'USA' AS country_name, *
        FROM (VALUES
            ('P1', 'A1'), ('P1', 'A2'), ('P2', 'B1')
        ) AS v(state_code, county_code)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    root = detect_root_level(conn, "t_01", level_columns)
    assert root is not None
    level, cols = root
    assert level == min(level_columns) - 1
    assert set(cols.identity_columns) == {"country_code", "country_name"}


def test_detect_level_columns_finds_trailing_name_only_leaf(conn):
    """A name repeated under different parents can't be checked unique globally."""
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'Province1', 'A1', 'AlphaCounty', 'Ward1',
             ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))')),
            ('P1', 'Province1', 'A2', 'BetaCounty', 'Ward2',
             ST_GeomFromText('POLYGON((1 0,2 0,2 1,1 1,1 0))')),
            ('P2', 'Province2', 'B1', 'GammaCounty', 'Ward1',
             ST_GeomFromText('POLYGON((0 1,1 1,1 2,0 2,0 1))')),
            ('P2', 'Province2', 'B2', 'DeltaCounty', 'Ward2',
             ST_GeomFromText('POLYGON((1 1,2 1,2 2,1 2,1 1))')),
            ('P3', 'Province3', 'C1', 'EpsilonCounty', 'Ward3',
             ST_GeomFromText('POLYGON((2 0,3 0,3 1,2 1,2 0))'))
        ) AS v(adm1_pcode, adm1_name, adm2_pcode, adm2_name, adm3_name, geom)
    """)
    result = detect_level_columns(conn, "t_01")
    leaf_level = max(result)
    assert result[leaf_level].group_by == ["adm3_name"]
    assert result[leaf_level].has_code is False
    assert "adm3_name" not in result[leaf_level - 1].group_by


def test_detect_leaf_level_returns_none_without_a_naming_anchor(conn):
    _load_two_level_table(conn)
    level_columns = detect_level_columns(conn, "t_01")
    assert detect_leaf_level(conn, "t_01", level_columns) is None


def test_group_families_by_level_word_based_anchor(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT row_number() OVER () AS fid, *
        FROM (VALUES
            ('P1', 'A1', 'Province1', 'Alpha'),
            ('P1', 'A2', 'Province1', 'Beta'),
            ('P2', 'B1', 'Province2', 'Gamma')
        ) AS v(state_code, county_code, state_name, county_name)
    """)
    level_columns = detect_level_columns(conn, "t_01")
    families = group_families_by_level(conn, "t_01", level_columns)
    code_family = next(f for f in families.values() if set(f.values()) & {"state_code"})
    assert set(code_family.values()) == {"state_code", "county_code"}


def test_detect_level_columns_digit_only_name_breaks_tie_by_schema_marker(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT * FROM (VALUES
            ('51', 'North', '0101', '101'), ('51', 'North', '0102', '102'),
            ('51', 'North', '0103', '103'), ('52', 'South', '0201', '201'),
            ('52', 'South', '0202', '202'), ('52', 'South', '0203', '203')
        ) AS v(adm1_code, adm1_name, adm2_code, adm2_name)
    """)
    level = detect_level_columns(conn, "t_01")[2]
    assert level.group_by[0] == "adm2_code"
    assert level.name_column == "adm2_name"


def test_verify_functional_cluster_raises_on_coarser_member_under_parent(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT * FROM (VALUES
            ('P1', 'c1', 'g1'), ('P1', 'c2', 'g1'),
            ('P1', 'c3', 'g2'), ('P1', 'c4', 'g2')
        ) AS v(parent, code, extra)
    """)
    with pytest.raises(ValueError, match="coarser level merged"):
        verify_functional_cluster(
            conn, "t_01", "code", ["code", "extra"], parent_column="parent"
        )


def test_verify_functional_cluster_passes_on_duplicate_name_under_parent(conn):
    conn.execute("""--sql
        CREATE TABLE t_01 AS
        SELECT * FROM (VALUES
            ('P1', 'c1', 'n1'), ('P1', 'c2', 'n2'),
            ('P1', 'c3', 'n3'), ('P1', 'c4', 'n3')
        ) AS v(parent, code, extra)
    """)
    verify_functional_cluster(
        conn, "t_01", "code", ["code", "extra"], parent_column="parent"
    )
