"""Smoke tests for the standalone schema-join tool."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.schema_join import join
from topo_tools.cli.main import cli

_STEPS = ["inputs", "assign", "join", "outputs"]

_PARENT_ROWS = [
    ("A", "Alpha", "A01", "Alpha One", 0, 0),
    ("A", "Alpha", "A02", "Alpha Two", 2, 0),
    ("B", "Beta", "B01", "Beta One", 0, 2),
    ("B", "Beta", "B02", "Beta Two", 2, 2),
]


def _square(x: float, y: float, size: float) -> str:
    return (
        f"POLYGON(({x} {y}, {x + size} {y}, {x + size} {y + size}, "
        f"{x} {y + size}, {x} {y}))"
    )


def _rect(x0: float, y0: float, x1: float, y1: float) -> str:
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


def _join_rows() -> list[dict]:
    return [
        {
            "adm1_code": a1c,
            "adm1_name": a1n,
            "adm2_code": a2c,
            "adm2_name": a2n,
            "wkt": _square(x, y, 2),
        }
        for a1c, a1n, a2c, a2n, x, y in _PARENT_ROWS
    ]


def _input_rows() -> list[dict]:
    rows = []
    for _, _, a2c, a2n, x, y in _PARENT_ROWS:
        for i, dx in enumerate((0, 1), start=1):
            rows.append(
                {
                    "adm3_code": f"{a2c}{i:02d}",
                    "adm3_name": f"{a2n} {i}",
                    "wkt": _square(x + dx, y, 1),
                }
            )
            rows.append(
                {
                    "adm3_code": f"{a2c}{i + 2:02d}",
                    "adm3_name": f"{a2n} {i + 2}",
                    "wkt": _square(x + dx, y + 1, 1),
                }
            )
    return rows


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _write(path, rows: list[dict]) -> None:
    cols = [k for k in rows[0] if k != "wkt"]
    values = ", ".join(
        "("
        + ", ".join(_sql_literal(r[c]) for c in cols)
        + f", ST_GeomFromText('{r['wkt']}'))"
        for r in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE t AS SELECT * FROM (VALUES {values}) "
            f"AS t({', '.join([*cols, 'geom'])})"
        )
        conn.execute(f"COPY t TO '{path}'")


def _read(path, geom: str = "geometry") -> tuple[list[str], list[tuple]]:
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT * EXCLUDE ({geom}), ST_AsWKB({geom}) AS wkb "
            f"FROM '{path}' ORDER BY adm3_code"
        ).fetchall()
        cols = [d[0] for d in conn.description]
    return cols, rows


def _issues(path) -> list[tuple]:
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(
            f"SELECT kind, unit_a, reason FROM '{path}' ORDER BY unit_a, kind"
        ).fetchall()


@pytest.fixture
def join_path(tmp_path):
    path = tmp_path / "admin2.parquet"
    _write(path, _join_rows())
    return path


@pytest.fixture
def input_path(tmp_path):
    path = tmp_path / "admin3.parquet"
    _write(path, _input_rows())
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["schema-join", "--help"])
    assert result.exit_code == 0
    assert "--min-overlap" in result.output


@pytest.mark.parametrize(
    "fields",
    [{}, {"name_field": "adm{n}_name", "code_field": "adm{n}_code"}],
    ids=["auto", "explicit"],
)
def test_fills_every_coarser_level(input_path, join_path, tmp_path, fields):
    out = tmp_path / "out.parquet"
    join(input_path, join_path, out, **fields)

    with duckdb.connect() as conn:
        written = conn.execute(f"SELECT * FROM '{out}'")
        assert [d[0] for d in written.description] == [
            "geometry",
            "adm3_code",
            "adm3_name",
            "adm2_name",
            "adm2_code",
            "adm1_name",
            "adm1_code",
        ]
        codes = [r[2] for r in written.fetchall()]
    assert codes == sorted(codes)
    cols, rows = _read(out)
    assert len(rows) == len(_input_rows())
    by_code = {r[cols.index("adm3_code")]: r for r in rows}
    for code, row in by_code.items():
        assert row[cols.index("adm2_code")] == code[:3]
        assert row[cols.index("adm1_code")] == code[0]
    assert not (tmp_path / "out_issues.parquet").exists()


def test_geometry_unchanged(input_path, join_path, tmp_path):
    out = tmp_path / "out.parquet"
    join(input_path, join_path, out)

    _, before = _read(input_path, geom="geom")
    _, after = _read(out)
    assert [r[-1] for r in before] == [r[-1] for r in after]


def test_chains_coarsest_first(input_path, tmp_path):
    admin1 = tmp_path / "admin1.parquet"
    _write(
        admin1,
        [
            {"adm1_code": "A", "adm1_name": "Alpha", "wkt": _rect(0, 0, 4, 2)},
            {"adm1_code": "B", "adm1_name": "Beta", "wkt": _rect(0, 2, 4, 4)},
        ],
    )
    admin2 = tmp_path / "admin2_bare.parquet"
    _write(
        admin2,
        [
            {"adm2_code": c, "adm2_name": n, "wkt": _square(x, y, 2)}
            for _, _, c, n, x, y in _PARENT_ROWS
        ],
    )
    admin2_joined = tmp_path / "admin2_join.parquet"
    join(admin2, admin1, admin2_joined)
    out = tmp_path / "admin3_join.parquet"
    join(input_path, admin2_joined, out)

    cols, rows = _read(out)
    for row in rows:
        code = row[cols.index("adm3_code")]
        assert row[cols.index("adm2_code")] == code[:3]
        assert row[cols.index("adm1_code")] == code[0]


def test_no_overlap_input_kept_with_null_columns(join_path, tmp_path):
    src = tmp_path / "orphan.parquet"
    _write(
        src,
        [
            *_input_rows(),
            {"adm3_code": "Z0101", "adm3_name": "Nowhere", "wkt": _square(10, 10, 1)},
        ],
    )
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    cols, rows = _read(out)
    assert len(rows) == len(_input_rows()) + 1
    orphan = next(r for r in rows if r[cols.index("adm3_code")] == "Z0101")
    assert orphan[cols.index("adm2_code")] is None
    kinds = [(k, u) for k, u, _ in _issues(tmp_path / "out_issues.parquet")]
    assert kinds == [("no-overlap", len(_input_rows()) + 1)]


def test_low_overlap_threshold(join_path, tmp_path):
    src = tmp_path / "straddle.parquet"
    _write(
        src,
        [{"adm3_code": "A0101", "adm3_name": "Straddle", "wkt": _square(1.5, 1.5, 1)}],
    )
    out = tmp_path / "out.parquet"
    join(src, join_path, out)
    issues = _issues(tmp_path / "out_issues.parquet")
    assert [(k, u) for k, u, _ in issues] == [("low-overlap", 1)]
    assert issues[0][2] == "best join polygon covers 0.25 of input polygon"

    join(src, join_path, out, min_overlap=0.2)
    assert not (tmp_path / "out_issues.parquet").exists()


def test_differing_shared_column_kept_side_by_side(join_path, tmp_path):
    rows = [
        {**r, "adm2_name": "Alpha Uno" if r["adm3_code"] == "A0101" else None}
        for r in _input_rows()
    ]
    for r in rows:
        if r["adm2_name"] is None:
            code = r["adm3_code"][:3]
            r["adm2_name"] = next(n for _, _, c, n, _, _ in _PARENT_ROWS if c == code)
    for r in rows:
        r["adm2_name1"] = "taken"
    src = tmp_path / "names.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    cols, result = _read(out)
    assert "adm2_name2" in cols
    row = next(r for r in result if r[cols.index("adm3_code")] == "A0101")
    assert row[cols.index("adm2_name")] == "Alpha Uno"
    assert row[cols.index("adm2_name1")] == "taken"
    assert row[cols.index("adm2_name2")] == "Alpha One"
    issues = _issues(tmp_path / "out_issues.parquet")
    assert [k for k, _, _ in issues] == ["value-mismatch"]
    assert issues[0][2] == "adm2_name: input 'Alpha Uno' vs join 'Alpha One'"


def test_issue_unit_a_is_output_row(join_path, tmp_path):
    rows = sorted(_input_rows(), key=lambda r: r["adm3_code"], reverse=True)
    for r in rows:
        code = r["adm3_code"][:3]
        r["adm2_name"] = next(n for _, _, c, n, _, _ in _PARENT_ROWS if c == code)
        if r["adm3_code"] == "A0203":
            r["adm2_name"] = "Alpha Dos"
    src = tmp_path / "reversed.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    (unit_a,) = [u for _, u, _ in _issues(tmp_path / "out_issues.parquet")]
    with duckdb.connect() as conn:
        codes = [
            r[0] for r in conn.execute(f"SELECT adm3_code FROM '{out}'").fetchall()
        ]
    assert codes[unit_a - 1] == "A0203"


def test_identical_shared_column_skipped(join_path, tmp_path):
    rows = [{**r, "adm2_code": r["adm3_code"][:3]} for r in _input_rows()]
    src = tmp_path / "codes.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    cols, _ = _read(out)
    assert "adm2_code1" not in cols
    assert cols.count("adm2_code") == 1


@pytest.mark.parametrize("step", _STEPS)
def test_step_runs(input_path, join_path, tmp_path, step):
    out = tmp_path / "out.parquet"
    tmp_dir = tmp_path / "tmp"
    for s in _STEPS:
        join(input_path, join_path, out, tmp_dir=tmp_dir, step=s, debug=True)
        if s == step:
            break


def test_invalid_min_overlap_raises(input_path, join_path):
    with pytest.raises(ValueError, match="min_overlap"):
        join(input_path, join_path, min_overlap=0)


def test_cli_runs(input_path, join_path, tmp_path):
    out = tmp_path / "cli.parquet"
    result = CliRunner().invoke(
        cli, ["schema-join", str(input_path), str(join_path), str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_join_layer_without_coarser_code_compares_names(tmp_path):
    join_file = tmp_path / "admin2_no_adm1_code.parquet"
    _write(
        join_file,
        [{k: v for k, v in r.items() if k != "adm1_code"} for r in _join_rows()],
    )
    rows = [
        {**r, "adm2_name": "Alpha Uno" if r["adm3_code"] == "A0101" else None}
        for r in _input_rows()
    ]
    for r in rows:
        if r["adm2_name"] is None:
            code = r["adm3_code"][:3]
            r["adm2_name"] = next(n for _, _, c, n, _, _ in _PARENT_ROWS if c == code)
    src = tmp_path / "names.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_file, out)

    cols, _ = _read(out)
    assert "adm2_name1" in cols
    issues = _issues(tmp_path / "out_issues.parquet")
    assert [k for k, _, _ in issues] == ["value-mismatch"]


def test_sibling_of_digit_ending_column_is_separated(tmp_path):
    join_file = tmp_path / "gadm2.parquet"
    _write(
        join_file,
        [
            {
                "GID_1": a1c,
                "NAME_1": a1n,
                "GID_2": a2c,
                "NAME_2": a2n,
                "wkt": _square(x, y, 2),
            }
            for a1c, a1n, a2c, a2n, x, y in _PARENT_ROWS
        ],
    )
    rows = []
    for r in _input_rows():
        code = r["adm3_code"][:3]
        name = next(n for _, _, c, n, _, _ in _PARENT_ROWS if c == code)
        rows.append(
            {
                "GID_3": r["adm3_code"],
                "NAME_3": r["adm3_name"],
                "NAME_2": "Alpha Uno" if r["adm3_code"] == "A0101" else name,
                "wkt": r["wkt"],
            }
        )
    src = tmp_path / "gadm3.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_file, out, name_field="NAME_{n}", code_field="GID_{n}")

    with duckdb.connect() as conn:
        cols = [d[0] for d in conn.execute(f"SELECT * FROM '{out}'").description]
    assert cols == [
        "geometry",
        "GID_3",
        "NAME_3",
        "NAME_2",
        "NAME_2_1",
        "GID_2",
        "NAME_1",
        "GID_1",
    ]


def test_shared_column_of_another_type_kept_side_by_side(join_path, tmp_path):
    code = 7
    rows = [{**r, "adm2_code": code} for r in _input_rows()]
    src = tmp_path / "typed.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    cols, result = _read(out)
    row = next(r for r in result if r[cols.index("adm3_code")] == "A0101")
    assert row[cols.index("adm2_code")] == code
    assert row[cols.index("adm2_code1")] == "A01"


def test_keeps_input_column_order_and_places_added_columns(join_path, tmp_path):
    parents = {a2c: a2n for _, _, a2c, a2n, _, _ in _PARENT_ROWS}
    rows = [
        {
            "adm3_name": r["adm3_name"],
            "adm3_code": r["adm3_code"],
            "adm3_type": "District",
            "adm2_name": "Alpha Uno"
            if r["adm3_code"] == "A0101"
            else parents[r["adm3_code"][:3]],
            "adm2_code": r["adm3_code"][:3],
            "wkt": r["wkt"],
        }
        for r in _input_rows()
    ]
    src = tmp_path / "typed.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    with duckdb.connect() as conn:
        written = conn.execute(f"SELECT * FROM '{out}' LIMIT 0")
        assert [d[0] for d in written.description] == [
            "geometry",
            "adm3_name",
            "adm3_code",
            "adm3_type",
            "adm2_name",
            "adm2_name1",
            "adm2_code",
            "adm1_name",
            "adm1_code",
        ]


def test_added_columns_go_within_their_level(join_path, tmp_path):
    rows = [
        {
            "adm3_name": r["adm3_name"],
            "adm3_code": r["adm3_code"],
            "adm1_code": r["adm3_code"][:1],
            "wkt": r["wkt"],
        }
        for r in _input_rows()
    ]
    src = tmp_path / "partial.parquet"
    _write(src, rows)
    out = tmp_path / "out.parquet"
    join(src, join_path, out)

    with duckdb.connect() as conn:
        written = conn.execute(f"SELECT * FROM '{out}' LIMIT 0")
        assert [d[0] for d in written.description] == [
            "geometry",
            "adm3_name",
            "adm3_code",
            "adm2_name",
            "adm2_code",
            "adm1_name",
            "adm1_code",
        ]


@pytest.mark.parametrize("layer", ["input", "join"])
def test_raises_on_overlapping_layer(layer, tmp_path):
    rows = {"input": _input_rows(), "join": _join_rows()}
    rows[layer] = [*rows[layer], dict(rows[layer][0])]
    paths = {k: tmp_path / f"{k}.parquet" for k in rows}
    for k, path in paths.items():
        _write(path, rows[k])
    with pytest.raises(ValueError, match=r"pair\(s\) overlap"):
        join(paths["input"], paths["join"], tmp_path / "out.parquet")
