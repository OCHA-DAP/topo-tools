"""Retention-policy tests for the code_update() tool."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.code_create import code_create
from topo_tools.api.code_update import code_update
from topo_tools.cli.main import cli

_CHANGELOG_COLUMNS = [
    "level",
    "old_code",
    "old_name",
    "new_code",
    "new_name",
    "relationship_class",
    "cluster_id",
    "match_method",
    "code_outcome",
    "reason",
]


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def _write_synthetic(path, rows: list[dict]) -> None:
    cols = [k for k in rows[0] if k != "wkt"]
    col_list = ", ".join([*cols, "geom"])
    values = ", ".join(
        "("
        + ", ".join(_sql_literal(r[c]) for c in cols)
        + f", ST_GeomFromText('{r['wkt']}'))"
        for r in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t({col_list})"
        )
        conn.execute(f"COPY synth TO '{path}'")


def _fetch(path, columns: str, order_by: str):
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        return conn.execute(
            f"SELECT {columns} FROM '{path}' ORDER BY {order_by}"
        ).fetchall()


def _read_changelog(path) -> list[dict]:
    with duckdb.connect() as conn:
        rows = conn.execute(f"SELECT * FROM '{path}'").fetchall()
    return [dict(zip(_CHANGELOG_COLUMNS, r, strict=True)) for r in rows]


def _rows_where(rows: list[dict], **kwargs) -> list[dict]:
    return [r for r in rows if all(r[k] == v for k, v in kwargs.items())]


def test_cli_help():
    result = CliRunner().invoke(cli, ["code-update", "--help"])
    assert result.exit_code == 0
    assert "Code a new release so units that carry over" in result.output


_ALL_CLASSES_OLD_ROWS = [
    {
        "adm1_pcode": "AA.001",
        "adm1_name": "N1",
        "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    },
    {
        "adm1_pcode": "AA.002",
        "adm1_name": "N2",
        "wkt": "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))",
    },
    {
        "adm1_pcode": "AA.003",
        "adm1_name": "N3",
        "wkt": "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))",
    },
    {
        "adm1_pcode": "AA.004",
        "adm1_name": "N4",
        "wkt": "POLYGON((30 0, 33 0, 33 1, 30 1, 30 0))",
    },
    {
        "adm1_pcode": "AA.005",
        "adm1_name": "N5a",
        "wkt": "POLYGON((40 0, 41 0, 41 1, 40 1, 40 0))",
    },
    {
        "adm1_pcode": "AA.006",
        "adm1_name": "N5b",
        "wkt": "POLYGON((41 0, 43 0, 43 1, 41 1, 41 0))",
    },
    {
        "adm1_pcode": "AA.007",
        "adm1_name": "N7",
        "wkt": "POLYGON((60 0, 61 0, 61 1, 60 1, 60 0))",
    },
    {
        "adm1_pcode": "AA.008",
        "adm1_name": "RL8",
        "wkt": "POLYGON((70 0, 71 0, 71 1, 70 1, 70 0))",
    },
    {
        "adm1_pcode": "AA.009",
        "adm1_name": "CX1",
        "wkt": "POLYGON((80 0, 82 0, 82 2, 80 2, 80 0))",
    },
    {
        "adm1_pcode": "AA.010",
        "adm1_name": "CX2",
        "wkt": "POLYGON((82 0, 84 0, 84 2, 82 2, 82 0))",
    },
]
_ALL_CLASSES_NEW_ROWS = [
    {
        "adm1_pcode": "x1",
        "adm1_name": "N1",
        "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    },
    {
        "adm1_pcode": "x2",
        "adm1_name": "N2 Renamed",
        "wkt": "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))",
    },
    {
        "adm1_pcode": "x3",
        "adm1_name": "N3",
        "wkt": "POLYGON((20 0, 21 0, 21 0.9, 20 0.9, 20 0))",
    },
    {
        "adm1_pcode": "x4a",
        "adm1_name": "N4a",
        "wkt": "POLYGON((30 0, 31 0, 31 1, 30 1, 30 0))",
    },
    {
        "adm1_pcode": "x4b",
        "adm1_name": "N4b",
        "wkt": "POLYGON((31 0, 33 0, 33 1, 31 1, 31 0))",
    },
    {
        "adm1_pcode": "x5",
        "adm1_name": "N5",
        "wkt": "POLYGON((40 0, 43 0, 43 1, 40 1, 40 0))",
    },
    {
        "adm1_pcode": "x9",
        "adm1_name": "C1",
        "wkt": "POLYGON((50 0, 51 0, 51 1, 50 1, 50 0))",
    },
    {
        "adm1_pcode": "x8",
        "adm1_name": "RL8",
        "wkt": "POLYGON((70.9 0, 71.9 0, 71.9 1, 70.9 1, 70.9 0))",
    },
    {
        "adm1_pcode": "x10",
        "adm1_name": "CXA",
        "wkt": "POLYGON((80 0, 84 0, 84 1, 80 1, 80 0))",
    },
    {
        "adm1_pcode": "x11",
        "adm1_name": "CXB",
        "wkt": "POLYGON((80 1, 84 1, 84 2, 80 2, 80 1))",
    },
]
_EXPECTED_SPLIT_COUNT = 2
_EXPECTED_COMPLEX_COUNT = 2


@pytest.fixture
def all_classes_result(tmp_path):
    """One spatially-separated region per relationship_class (or class pair)."""
    old_path = tmp_path / "old.parquet"
    new_path = tmp_path / "new.parquet"
    _write_synthetic(old_path, _ALL_CLASSES_OLD_ROWS)
    _write_synthetic(new_path, _ALL_CLASSES_NEW_ROWS)

    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(
        old_path,
        new_path,
        output_path,
        changelog_path,
        root_code="AA",
        delimiter=".",
        min_width=3,
        name_field_a="adm{n}_name",
        code_field_a="adm{n}_pcode",
        name_field_b="adm{n}_name",
        code_field_b="adm{n}_pcode",
        link_by_name=True,
        tau_match=0.4,
    )

    assert changelog_path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _read_changelog(changelog_path)
    predecessor_by_code = dict(
        _fetch(output_path, "adm1_pcode, predecessor_code", "adm1_pcode")
    )
    return rows, predecessor_by_code


def test_unchanged_retains_code(all_classes_result):
    rows, _ = all_classes_result
    unchanged = _rows_where(rows, relationship_class="unchanged")
    assert len(unchanged) == 1
    assert unchanged[0]["old_code"] == "AA.001"
    assert unchanged[0]["new_code"] == "AA.001"
    assert unchanged[0]["code_outcome"] == "retained"


def test_renamed_retains_code_and_updates_name(all_classes_result):
    rows, _ = all_classes_result
    renamed = _rows_where(rows, relationship_class="renamed")
    assert len(renamed) == 1
    assert renamed[0]["old_code"] == "AA.002"
    assert renamed[0]["new_code"] == "AA.002"
    assert renamed[0]["new_name"] == "N2 Renamed"
    assert renamed[0]["code_outcome"] == "retained"


def test_modified_gets_new_code_with_predecessor(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    modified = _rows_where(rows, relationship_class="modified")
    assert len(modified) == 1
    assert modified[0]["old_code"] == "AA.003"
    assert modified[0]["code_outcome"] == "new"
    assert predecessor_by_code[modified[0]["new_code"]] == "AA.003"


def test_split_shares_cluster_and_predecessor(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    split = _rows_where(rows, relationship_class="split")
    assert len(split) == _EXPECTED_SPLIT_COUNT
    assert len({r["cluster_id"] for r in split}) == 1
    assert all(r["old_code"] is None for r in split)
    assert all(r["code_outcome"] == "new" for r in split)
    for r in split:
        assert predecessor_by_code[r["new_code"]] == "AA.004"


def test_merge_retires_both_olds_no_predecessor_on_survivor(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    merge_retired = _rows_where(
        rows, relationship_class="merge", code_outcome="retired"
    )
    merge_new = _rows_where(rows, relationship_class="merge", code_outcome="new")
    assert {r["old_code"] for r in merge_retired} == {"AA.005", "AA.006"}
    assert len(merge_new) == 1
    assert len({r["cluster_id"] for r in [*merge_retired, *merge_new]}) == 1
    assert predecessor_by_code[merge_new[0]["new_code"]] is None


def test_removed_has_no_new_code(all_classes_result):
    rows, _ = all_classes_result
    removed = _rows_where(rows, relationship_class="removed")
    assert len(removed) == 1
    assert removed[0]["old_code"] == "AA.007"
    assert removed[0]["new_code"] is None
    assert removed[0]["code_outcome"] == "retired"


def test_created_has_no_predecessor(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    created = _rows_where(rows, relationship_class="created")
    assert len(created) == 1
    assert created[0]["old_code"] is None
    assert created[0]["code_outcome"] == "new"
    assert predecessor_by_code[created[0]["new_code"]] is None


def test_relocated_identity_linked_with_predecessor(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    relocated = _rows_where(rows, relationship_class="relocated")
    assert len(relocated) == 1
    assert relocated[0]["old_code"] == "AA.008"
    assert relocated[0]["code_outcome"] == "new"
    assert relocated[0]["match_method"] == "identity"
    assert predecessor_by_code[relocated[0]["new_code"]] == "AA.008"


def test_complex_cluster_retires_olds_no_predecessor_on_survivors(all_classes_result):
    rows, predecessor_by_code = all_classes_result
    complex_retired = _rows_where(
        rows, relationship_class="complex", code_outcome="retired"
    )
    complex_new = _rows_where(rows, relationship_class="complex", code_outcome="new")
    assert {r["old_code"] for r in complex_retired} == {"AA.009", "AA.010"}
    assert len(complex_new) == _EXPECTED_COMPLEX_COUNT
    assert len({r["cluster_id"] for r in [*complex_retired, *complex_new]}) == 1
    assert all(predecessor_by_code[r["new_code"]] is None for r in complex_new)


def test_multilevel_cascade_rewrites_unchanged_children_under_new_parent(tmp_path):
    """A modified level-1 parent cascades its new prefix to unchanged descendants."""
    old_rows = [
        {
            "adm1_pcode": "AA.001",
            "adm1_name": "Province1",
            "adm2_pcode": "AA.001.001",
            "adm2_name": "District1",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "adm1_pcode": "AA.001",
            "adm1_name": "Province1",
            "adm2_pcode": "AA.001.002",
            "adm2_name": "District2",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "adm1_pcode": "AA.001",
            "adm1_name": "Province1",
            "adm2_pcode": "AA.001.003",
            "adm2_name": "District3",
            "wkt": "POLYGON((2 0, 3 0, 3 1, 2 1, 2 0))",
        },
        {
            "adm1_pcode": "AA.002",
            "adm1_name": "Province2",
            "adm2_pcode": "AA.002.001",
            "adm2_name": "OnlyDistrict",
            "wkt": "POLYGON((10 10, 11 10, 11 11, 10 11, 10 10))",
        },
    ]
    new_rows = [
        {
            "adm1_pcode": "x",
            "adm1_name": "Province1",
            "adm2_pcode": "y1",
            "adm2_name": "District1",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "adm1_pcode": "x",
            "adm1_name": "Province1",
            "adm2_pcode": "y2",
            "adm2_name": "District2",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "adm1_pcode": "x",
            "adm1_name": "Province1",
            "adm2_pcode": "y3",
            "adm2_name": "District3 Expanded",
            "wkt": "POLYGON((2 0, 3 0, 3 3, 2 3, 2 0))",
        },
        {
            "adm1_pcode": "z",
            "adm1_name": "Province2",
            "adm2_pcode": "y4",
            "adm2_name": "OnlyDistrict",
            "wkt": "POLYGON((10 10, 11 10, 11 11, 10 11, 10 10))",
        },
    ]
    old_path = tmp_path / "old.parquet"
    new_path = tmp_path / "new.parquet"
    _write_synthetic(old_path, old_rows)
    _write_synthetic(new_path, new_rows)

    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(
        old_path,
        new_path,
        output_path,
        changelog_path,
        root_code="AA",
        delimiter=".",
        min_width=3,
        name_field_a="adm{n}_name",
        code_field_a="adm{n}_pcode",
        name_field_b="adm{n}_name",
        code_field_b="adm{n}_pcode",
    )

    rows = _read_changelog(changelog_path)

    level1 = _rows_where(rows, level=1)
    province1 = _rows_where(level1, old_code="AA.001")[0]
    province2 = _rows_where(level1, old_code="AA.002")[0]
    assert province1["relationship_class"] == "modified"
    assert province1["code_outcome"] == "new"
    assert province2["relationship_class"] == "unchanged"
    assert province2["new_code"] == "AA.002"

    new_province1_code = province1["new_code"]
    assert new_province1_code != "AA.001"

    level2 = _rows_where(rows, level=2)
    d1 = _rows_where(level2, old_code="AA.001.001")[0]
    d2 = _rows_where(level2, old_code="AA.001.002")[0]
    d3 = _rows_where(level2, old_code="AA.001.003")[0]
    d4 = _rows_where(level2, old_code="AA.002.001")[0]

    assert d1["relationship_class"] == "unchanged"
    assert d1["new_code"] == f"{new_province1_code}.001"
    assert d2["relationship_class"] == "unchanged"
    assert d2["new_code"] == f"{new_province1_code}.002"
    assert d3["relationship_class"] == "modified"
    assert d3["new_code"].startswith(f"{new_province1_code}.")
    assert d4["relationship_class"] == "unchanged"
    assert d4["new_code"] == "AA.002.001"

    out_codes = {r[0] for r in _fetch(output_path, "adm1_pcode", "adm1_pcode")}
    assert out_codes == {new_province1_code, "AA.002"}


def test_level_count_mismatch_raises(tmp_path):
    old_rows = [
        {
            "adm1_pcode": "AA.001",
            "adm1_name": "P1",
            "adm2_pcode": "AA.001.001",
            "adm2_name": "D1",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
    ]
    new_rows = [
        {
            "adm1_pcode": "x",
            "adm1_name": "P1",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
    ]
    old_path = tmp_path / "old.parquet"
    new_path = tmp_path / "new.parquet"
    _write_synthetic(old_path, old_rows)
    _write_synthetic(new_path, new_rows)

    with pytest.raises(ValueError, match="level mismatch"):
        code_update(
            old_path,
            new_path,
            tmp_path / "out.parquet",
            tmp_path / "changelog.csv",
            root_code="AA",
            delimiter=".",
            min_width=3,
            name_field_a="adm{n}_name",
            code_field_a="adm{n}_pcode",
            name_field_b="adm{n}_name",
            code_field_b="adm{n}_pcode",
        )


@pytest.mark.parametrize(
    ("min_width", "level1", "level2"),
    [
        (2, {"AA-01", "AA-02"}, {"AA-01-01", "AA-01-02", "AA-02-01"}),
        ("1,3", {"AA-1", "AA-2"}, {"AA-1-001", "AA-1-002", "AA-2-001"}),
    ],
)
def test_custom_format_round_trip_detected_from_old_codes(
    tmp_path, min_width, level1, level2
):
    """code-update auto-detects a code-create-produced custom delimiter/width."""
    raw_rows = [
        {
            "adm1_code": "P1",
            "adm2_code": "P1-01",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "adm1_code": "P1",
            "adm2_code": "P1-02",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "adm1_code": "P2",
            "adm2_code": "P2-01",
            "wkt": "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))",
        },
    ]
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, raw_rows)

    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(
        raw_path, old_coded_path, root_code="AA", delimiter="-", min_width=min_width
    )

    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, raw_rows)

    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, new_path, output_path, changelog_path)

    level1_codes = {r[0] for r in _fetch(output_path, "adm1_code", "adm1_code")}
    level2_codes = {r[0] for r in _fetch(output_path, "adm2_code", "adm2_code")}
    assert level1_codes == level1
    assert level2_codes == level2

    rows = _read_changelog(changelog_path)
    assert all(r["relationship_class"] == "unchanged" for r in rows)
    assert all(r["code_outcome"] == "retained" for r in rows)


_TEMPLATES_AB = {
    "code_field_a": "adm{n}_code",
    "name_field_a": "adm{n}_name",
    "code_field_b": "adm{n}_code",
    "name_field_b": "adm{n}_name",
}


def _square(x, y):
    return f"POLYGON(({x} {y}, {x + 1} {y}, {x + 1} {y + 1}, {x} {y + 1}, {x} {y}))"


def test_undelimited_codes_retained_with_names_only_level(tmp_path):
    """ISO2-style codes from code-create embed survive an unchanged update."""
    raw_rows = [
        {
            "adm1_code": c1,
            "adm1_name": f"A{c1}",
            "adm2_name": n2,
            "adm3_code": c3,
            "adm3_name": f"C{c3}",
            "wkt": _square(i, 0),
        }
        for i, (c1, n2, c3) in enumerate(
            [("51", "Hidd", "0101"), ("51", "Adhari", "0102"), ("52", "Zallaq", "0201")]
        )
    ]
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, raw_rows)
    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(
        raw_path,
        old_coded_path,
        root_code="BH",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        code_field="adm{n}_code",
        name_field="adm{n}_name",
    )

    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, raw_path, output_path, changelog_path, **_TEMPLATES_AB)

    columns = "adm1_code, adm2_code, adm3_code"
    assert _fetch(output_path, columns, "adm3_code") == _fetch(
        old_coded_path, columns, "adm3_code"
    )
    rows = _read_changelog(changelog_path)
    assert all(r["code_outcome"] == "retained" for r in rows)


def test_split_never_reuses_a_retired_number(tmp_path):
    """A split unit's code is retired; its children number above it, not into it."""
    old_rows = [
        {"adm1_code": "P1", "adm2_code": f"P1-0{i}", "wkt": _square(i, 0)}
        for i in range(3)
    ] + [{"adm1_code": "P2", "adm2_code": "P2-01", "wkt": _square(0, 1)}]
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, old_rows)
    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(raw_path, old_coded_path, root_code="AA", delimiter="-", min_width=2)

    new_rows = [*old_rows[:2], old_rows[3]] + [
        {
            "adm1_code": "P1",
            "adm2_code": f"P1-x{half}",
            "wkt": f"POLYGON((2 {y0}, 3 {y0}, 3 {y0 + 0.5}, 2 {y0 + 0.5}, 2 {y0}))",
        }
        for half, y0 in (("a", 0), ("b", 0.5))
    ]
    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, new_rows)
    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, new_path, output_path, changelog_path)

    split = _rows_where(_read_changelog(changelog_path), relationship_class="split")
    assert sorted(r["new_code"] for r in split) == ["AA-01-04", "AA-01-05"]
    assert _fetch(output_path, "adm2_code, predecessor_code", "adm2_code")[2:4] == [
        ("AA-01-04", "AA-01-03"),
        ("AA-01-05", "AA-01-03"),
    ]


def test_modified_keeps_code_without_delimiter(tmp_path):
    """ISO2-style codes are lenient: a re-digitised 1:1 match keeps its code."""
    old_rows = [
        {
            "adm1_code": "51",
            "adm1_name": "A",
            "adm2_code": f"0{i}",
            "wkt": _square(i, 0),
        }
        for i in (1, 2)
    ] + [{"adm1_code": "52", "adm1_name": "B", "adm2_code": "01", "wkt": _square(1, 1)}]
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, [{**r, "adm2_name": r["adm2_code"]} for r in old_rows])
    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(
        raw_path,
        old_coded_path,
        root_code="XY",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        code_field="adm{n}_code",
        name_field="adm{n}_name",
    )

    new_rows = [
        {
            **r,
            "adm2_code": f"{r['adm1_name']}{r['adm2_code']}",
            "adm2_name": r["adm2_code"],
        }
        for r in old_rows
    ]
    new_rows[0]["wkt"] = "POLYGON((1 0, 1.9 0, 1.9 1, 1 1, 1 0))"
    new_rows[1]["wkt"] = "POLYGON((1.9 0, 3 0, 3 1, 1.9 1, 1.9 0))"
    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, new_rows)
    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, new_path, output_path, changelog_path, **_TEMPLATES_AB)

    rows = _read_changelog(changelog_path)
    assert any(r["relationship_class"] == "modified" for r in rows)
    assert all(r["code_outcome"] == "retained" for r in rows)
    assert _fetch(output_path, "adm2_code", "adm2_code") == [
        ("XY5101",),
        ("XY5102",),
        ("XY5201",),
    ]


def _two_parent_rows():
    return [
        {
            "adm1_code": c1,
            "adm1_name": n1,
            "adm2_code": f"{i:02d}",
            "adm2_name": f"{n1}{i}",
            "wkt": _square(i, y),
        }
        for c1, n1, y in (("51", "A", 0), ("52", "B", 1))
        for i in range(1, 11)
    ]


def _code_old(tmp_path, rows):
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, rows)
    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(
        raw_path,
        old_coded_path,
        root_code="XY",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        code_field="adm{n}_code",
        name_field="adm{n}_name",
    )
    return old_coded_path


def test_moved_unit_never_takes_a_code_already_used(tmp_path):
    """A unit moved under a parent that already has its code tail gets a new code."""
    old_rows = _two_parent_rows()
    old_coded_path = _code_old(tmp_path, old_rows)

    new_rows = [
        {**r, "adm2_code": f"{r['adm1_code']}{r['adm2_code']}"} for r in old_rows
    ]
    new_rows[9] = {**new_rows[9], "adm1_code": "52", "adm1_name": "B"}
    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, new_rows)
    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, new_path, output_path, changelog_path, **_TEMPLATES_AB)

    codes = [c for (c,) in _fetch(output_path, "adm2_code", "adm2_code")]
    assert len(codes) == len(set(codes))
    assert ("XY5211", "XY5110") in _fetch(
        output_path, "adm2_code, predecessor_code", "adm2_code"
    )
    moved = _rows_where(_read_changelog(changelog_path), old_code="XY5110")
    assert [(r["new_code"], r["code_outcome"]) for r in moved] == [("XY5211", "new")]


def test_same_names_under_different_parents_stay_apart(tmp_path):
    """A names-only NEW level never merges same-named units under two parents."""
    old_rows = [{**r, "adm2_name": f"N{r['adm2_code']}"} for r in _two_parent_rows()]
    old_coded_path = _code_old(tmp_path, old_rows)

    new_rows = [{k: v for k, v in r.items() if k != "adm2_code"} for r in old_rows]
    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, new_rows)
    output_path = tmp_path / "new_coded.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_coded_path, new_path, output_path, changelog_path, **_TEMPLATES_AB)

    columns = "adm1_code, adm2_code"
    assert _fetch(output_path, columns, "adm2_code") == _fetch(
        old_coded_path, columns, "adm2_code"
    )


@pytest.mark.parametrize(
    ("side", "column", "value", "match"),
    [
        ("old", "adm2_code", None, "no code"),
        ("new", "adm2_name", None, "no code"),
        ("new", "adm1_name", "Z", "more than one"),
    ],
)
def test_missing_code_or_mixed_name_raises(tmp_path, side, column, value, match):
    """A NULL code (or seeding name), or a code with two names, raises up front."""
    rows = [{**r, "adm2_name": f"N{r['adm2_code']}"} for r in _two_parent_rows()]
    old_rows = [
        {
            **r,
            "adm1_code": f"XY{r['adm1_code']}",
            "adm2_code": f"XY{r['adm1_code']}{r['adm2_code']}",
        }
        for r in rows
    ]
    new_rows = [{k: v for k, v in r.items() if k != "adm2_code"} for r in rows]
    target = old_rows if side == "old" else new_rows
    target[0][column] = value
    old_path, new_path = tmp_path / "old.parquet", tmp_path / "new.parquet"
    _write_synthetic(old_path, old_rows)
    _write_synthetic(new_path, new_rows)
    with pytest.raises(ValueError, match=match):
        code_update(old_path, new_path, tmp_path / "out.parquet", **_TEMPLATES_AB)


def test_same_named_siblings_seeded_from_names_raise(tmp_path):
    """Two NEW units with one name under one parent can't be told apart."""
    old_rows = [{**r, "adm2_name": f"N{r['adm2_code']}"} for r in _two_parent_rows()]
    old_coded_path = _code_old(tmp_path, old_rows)
    new_rows = [{k: v for k, v in r.items() if k != "adm2_code"} for r in old_rows]
    new_rows[1]["adm2_name"] = new_rows[0]["adm2_name"]
    new_path = tmp_path / "new.parquet"
    _write_synthetic(new_path, new_rows)
    with pytest.raises(ValueError, match="repeat under one parent"):
        code_update(old_coded_path, new_path, tmp_path / "out.parquet", **_TEMPLATES_AB)


def test_template_writes_root_code_at_level_0(tmp_path):
    raw_rows = [
        {
            "adm1_code": c1,
            "adm1_name": f"A{c1}",
            "adm2_code": c2,
            "adm2_name": f"B{c2}",
            "wkt": _square(i, 0),
        }
        for i, (c1, c2) in enumerate([("51", "5101"), ("51", "5102"), ("52", "5201")])
    ]
    raw_path = tmp_path / "raw.parquet"
    _write_synthetic(raw_path, raw_rows)
    old_coded_path = tmp_path / "old_coded.parquet"
    code_create(
        raw_path,
        old_coded_path,
        root_code="BH",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        code_field="adm{n}_code",
        name_field="adm{n}_name",
    )
    output_path = tmp_path / "new_coded.parquet"
    code_update(
        old_coded_path, raw_path, output_path, tmp_path / "log.csv", **_TEMPLATES_AB
    )
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    ]
    assert columns.index("adm0_code") == columns.index("adm1_name") + 1
    assert _fetch(output_path, "DISTINCT adm0_code", "1") == [("BH",)]


def test_integer_typed_code_column_in_new_is_overwritten(tmp_path):
    """NEW's raw integer column sharing OLD's code column name takes the new codes."""
    old_path = tmp_path / "old.parquet"
    _write_synthetic(
        old_path,
        [
            {"adm1_name": "A", "adm1_code": "SA01", "wkt": _square(0, 0)},
            {"adm1_name": "B", "adm1_code": "SA02", "wkt": _square(1, 0)},
        ],
    )
    new_path = tmp_path / "new.parquet"
    _write_synthetic(
        new_path,
        [
            {"adm1_name": "A", "adm1_code": 1, "wkt": _square(0, 0)},
            {"adm1_name": "B", "adm1_code": 2, "wkt": _square(1, 0)},
        ],
    )
    output_path = tmp_path / "out.parquet"
    changelog_path = tmp_path / "changelog.csv"
    code_update(old_path, new_path, output_path, changelog_path)

    assert _fetch(output_path, "adm1_name, adm1_code", "adm1_name") == [
        ("A", "SA01"),
        ("B", "SA02"),
    ]
    types = dict(
        r[:2] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    )
    assert types["adm1_code"] == "VARCHAR"
    rows = _read_changelog(changelog_path)
    assert all(r["code_outcome"] == "retained" for r in rows)
