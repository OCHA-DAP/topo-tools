"""Portability + naming tests for the code_create() tool."""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.code_create import code_create
from topo_tools.cli.main import cli

_MIN_WIDTH = 3


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


def test_cli_help():
    result = CliRunner().invoke(cli, ["code-create", "--help"])
    assert result.exit_code == 0
    assert "Give every unit a new hierarchical code" in result.output


def test_gadm_style_cold_start_admin0_passthrough(tmp_path):
    """Example A: GID_0 is constant, dropped structurally, left untouched."""
    rows = [
        {
            "GID_0": "AFG",
            "GID_1": "AFG.1_1",
            "NAME_1": "Badakhshan",
            "GID_2": "AFG.1.1_1",
            "NAME_2": "Arghanj Khwa",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "GID_0": "AFG",
            "GID_1": "AFG.1_1",
            "NAME_1": "Badakhshan",
            "GID_2": "AFG.1.2_1",
            "NAME_2": "Baharak",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "GID_0": "AFG",
            "GID_1": "AFG.2_1",
            "NAME_1": "Badghis",
            "GID_2": "AFG.2.1_1",
            "NAME_2": "Ab Kamari",
            "wkt": "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))",
        },
    ]
    input_path = tmp_path / "admin2.parquet"
    _write_synthetic(input_path, rows)

    output_path = tmp_path / "admin2_coded.parquet"
    code_create(input_path, output_path, root_code="AFG", delimiter=".", min_width=3)

    result = _fetch(output_path, "GID_0, GID_1, NAME_1, GID_2, NAME_2", "GID_1, GID_2")
    assert result == [
        ("AFG", "AFG.001", "Badakhshan", "AFG.001.001", "Arghanj Khwa"),
        ("AFG", "AFG.001", "Badakhshan", "AFG.001.002", "Baharak"),
        ("AFG", "AFG.002", "Badghis", "AFG.002.001", "Ab Kamari"),
    ]


def test_word_per_level_naming(tmp_path):
    """Example B: state_code/county_code, no adm{n} template needed."""
    rows = [
        {
            "state_code": "TX",
            "county_code": "TX-HARRIS",
            "county_name": "Harris",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "state_code": "TX",
            "county_code": "TX-DALLAS",
            "county_name": "Dallas",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "state_code": "CA",
            "county_code": "CA-ORANGE",
            "county_name": "Orange",
            "wkt": "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))",
        },
    ]
    input_path = tmp_path / "states.parquet"
    _write_synthetic(input_path, rows)

    output_path = tmp_path / "states_coded.parquet"
    code_create(input_path, output_path, root_code="USA", delimiter=".", min_width=3)

    result = _fetch(output_path, "state_code, county_code, county_name", "county_code")
    assert result == [
        ("USA.001", "USA.001.001", "Orange"),
        ("USA.002", "USA.002.001", "Dallas"),
        ("USA.002", "USA.002.002", "Harris"),
    ]


def test_explicit_code_name_field_overrides_ambiguous_auto_detection(tmp_path):
    """Example C: two code-shaped columns per level, --code-field breaks the tie."""
    rows = [
        {
            "adm0_code": "AFG",
            "adm1_code": "NAT-01",
            "adm1_pcode": "PC-01",
            "adm1_name": "Badakhshan",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "adm0_code": "AFG",
            "adm1_code": "NAT-02",
            "adm1_pcode": "PC-02",
            "adm1_name": "Badghis",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
    ]
    input_path = tmp_path / "ambiguous.parquet"
    _write_synthetic(input_path, rows)

    output_path = tmp_path / "ambiguous_coded.parquet"
    code_create(
        input_path,
        output_path,
        root_code="AFG",
        delimiter=".",
        min_width=3,
        code_field="adm{n}_pcode",
        name_field="adm{n}_name",
    )

    result = _fetch(
        output_path, "adm0_code, adm1_code, adm1_pcode, adm1_name", "adm1_pcode"
    )
    assert result == [
        ("AFG", "NAT-01", "AFG.001", "Badakhshan"),
        ("AFG", "NAT-02", "AFG.002", "Badghis"),
    ]


def test_overflow_writes_four_digit_code_and_issues_row(tmp_path):
    """Example D: >999 children, no repad of the first 999, issues row written."""
    total = 1200
    rows = [
        {
            "adm0_code": "BRA",
            "adm1_code": f"BRA-{i:04d}",
            "wkt": f"POLYGON(({x} 0, {x + 0.1} 0, {x + 0.1} 1, {x} 1, {x} 0))",
        }
        for i in range(total)
        for x in [i / 10]
    ]
    input_path = tmp_path / "overflow.parquet"
    _write_synthetic(input_path, rows)

    output_path = tmp_path / "overflow_coded.parquet"
    issues_path = tmp_path / "overflow_issues.csv"
    code_create(
        input_path,
        output_path,
        issues_path,
        root_code="BRA",
        delimiter=".",
        min_width=3,
    )

    assert issues_path.read_bytes().startswith(b"\xef\xbb\xbf")
    codes = {r[0] for r in _fetch(output_path, "adm1_code", "adm1_code")}
    assert len(codes) == total
    assert "BRA.999" in codes
    assert "BRA.1000" in codes
    assert all(
        len(c.split(".")[-1]) == _MIN_WIDTH
        for c in codes
        if int(c.split(".")[-1]) <= 999  # noqa: PLR2004
    )

    with duckdb.connect() as conn:
        issue = conn.execute(
            f"SELECT kind, level, parent_code, child_count, min_width "
            f"FROM '{issues_path}'"
        ).fetchone()
    assert issue == ("digit-overflow", 1, "BRA", total, 3)


def test_disputed_territory_root_code_is_opaque(tmp_path):
    """Example E: a non-ISO3 user-assigned root code works with no special casing."""
    rows = [
        {
            "region_code": "K1",
            "region_name": "North Kosovo",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "region_code": "K2",
            "region_name": "South Kosovo",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
    ]
    input_path = tmp_path / "kosovo_disputed.parquet"
    _write_synthetic(input_path, rows)

    output_path = tmp_path / "kosovo_coded.parquet"
    code_create(input_path, output_path, root_code="XKO", delimiter=".", min_width=3)

    result = _fetch(output_path, "region_code, region_name", "region_code")
    assert result == [
        ("XKO.001", "North Kosovo"),
        ("XKO.002", "South Kosovo"),
    ]


_TWO_LEVEL_ROWS = [
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


def test_custom_delimiter_and_width(tmp_path):
    input_path = tmp_path / "custom.parquet"
    _write_synthetic(input_path, _TWO_LEVEL_ROWS)

    output_path = tmp_path / "custom_coded.parquet"
    code_create(input_path, output_path, root_code="AA", delimiter="-", min_width=2)

    codes = {r[0] for r in _fetch(output_path, "adm1_code", "adm1_code")}
    assert codes == {"AA-01", "AA-02"}


def test_mismatched_code_name_field_raises(tmp_path):
    rows = [
        {"adm1_code": "A1", "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"},
    ]
    input_path = tmp_path / "one.parquet"
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="must be given together"):
        code_create(
            input_path,
            root_code="AA",
            delimiter=".",
            min_width=3,
            code_field="adm{n}_code",
        )


def test_trailing_name_only_level_raises(tmp_path):
    """A finest level with no code column can't be cold-started without --code-field."""
    rows = [
        {
            "adm1_pcode": "P1",
            "adm1_name": "Province1",
            "adm2_pcode": "A1",
            "adm2_name": "AlphaCounty",
            "adm3_name": "Ward1",
            "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        {
            "adm1_pcode": "P1",
            "adm1_name": "Province1",
            "adm2_pcode": "A2",
            "adm2_name": "BetaCounty",
            "adm3_name": "Ward2",
            "wkt": "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
        },
        {
            "adm1_pcode": "P2",
            "adm1_name": "Province2",
            "adm2_pcode": "B1",
            "adm2_name": "GammaCounty",
            "adm3_name": "Ward1",
            "wkt": "POLYGON((0 1, 1 1, 1 2, 0 2, 0 1))",
        },
        {
            "adm1_pcode": "P2",
            "adm1_name": "Province2",
            "adm2_pcode": "B2",
            "adm2_name": "DeltaCounty",
            "adm3_name": "Ward2",
            "wkt": "POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))",
        },
        {
            "adm1_pcode": "P3",
            "adm1_name": "Province3",
            "adm2_pcode": "C1",
            "adm2_name": "EpsilonCounty",
            "adm3_name": "Ward3",
            "wkt": "POLYGON((2 0, 3 0, 3 1, 2 1, 2 0))",
        },
    ]
    input_path = tmp_path / "leaf.parquet"
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="no existing code column"):
        code_create(input_path, root_code="AA", delimiter=".", min_width=3)


def test_default_output_path(tmp_path):
    input_path = tmp_path / "leaf.parquet"
    _write_synthetic(input_path, _TWO_LEVEL_ROWS)
    code_create(input_path, root_code="AA", delimiter=".", min_width=3)
    assert input_path.with_stem(input_path.stem + "_coded").exists()


def test_steps(tmp_path):
    input_path = tmp_path / "leaf.parquet"
    _write_synthetic(input_path, _TWO_LEVEL_ROWS)

    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in ("inputs", "levels", "assign", "outputs"):
        code_create(
            input_path,
            output_path,
            root_code="AA",
            delimiter=".",
            min_width=3,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()


def test_cli_error_on_existing_output(tmp_path):
    rows = [
        {"adm1_code": "A1", "wkt": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"},
    ]
    input_path = tmp_path / "leaf.parquet"
    _write_synthetic(input_path, rows)
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        [
            "code-create",
            str(input_path),
            str(output_path),
            "--root-code",
            "AA",
            "--delimiter",
            ".",
            "--min-width",
            "3",
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert "output already exists" in result.output


def test_supplemental_grouping_raises(tmp_path):
    """A name-only level whose names repeat across parents stops structural coding."""
    units = [
        ("A", "North", "X", "0101", "101"),
        ("A", "North", "X", "0102", "102"),
        ("A", "North", "Y", "0103", "103"),
        ("A", "North", "Z", "0104", "104"),
        ("B", "South", "Y", "0201", "201"),
        ("B", "South", "Z", "0202", "202"),
        ("B", "South", "W", "0203", "203"),
        ("B", "South", "W", "0204", "204"),
    ]
    rows = [
        {
            "adm1_code": a1,
            "adm1_name": n1,
            "adm2_name": n2,
            "adm3_code": c3,
            "adm3_name": n3,
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, (a1, n1, n2, c3, n3) in enumerate(units)
    ]
    input_path = tmp_path / "blocks.parquet"
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match=r"\['adm2_name'\] group units like a level"):
        code_create(input_path, root_code="AA", delimiter=".", min_width=3)


def test_scattered_type_column_does_not_stop_coding(tmp_path):
    """A unit type spread across the map can't be a missed level."""
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    rows = [
        {
            "adm1_code": f"P{i // 6 + 1}",
            "adm1_name": ["North", "South"][i // 6],
            "adm2_code": f"P{i // 6 + 1}{i % 6 + 1:02d}",
            "adm2_name": f"{names[i % 6]} {['North', 'South'][i // 6]}",
            "unit_type": ["city", "district", "town"][i % 3],
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i in range(12)
    ]
    input_path = tmp_path / "typed.parquet"
    _write_synthetic(input_path, rows)
    output_path = tmp_path / "typed_coded.parquet"
    code_create(input_path, output_path, root_code="AA", delimiter=".", min_width=2)
    result = _fetch(output_path, "adm1_code, adm2_code", "adm2_code")
    assert result[0] == ("AA.01", "AA.01.01")


def _source_coded_rows(adm3_codes=("0101", "0102", "0201", "0202")):
    units = [
        ("51", "North", "Hidd", adm3_codes[0]),
        ("51", "North", "Adhari", adm3_codes[1]),
        ("52", "South", "Zallaq", adm3_codes[2]),
        ("52", "South", "Askar", adm3_codes[3]),
    ]
    return [
        {
            "adm1_code": c1,
            "adm1_name": n1,
            "adm2_name": n2,
            "adm3_code": c3,
            "adm3_name": c3.lstrip("0"),
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, (c1, n1, n2, c3) in enumerate(units)
    ]


_TEMPLATES = {"code_field": "adm{n}_code", "name_field": "adm{n}_name"}


def test_embed_concatenates_source_codes_without_delimiter(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    code_create(
        input_path,
        output_path,
        root_code="BH",
        delimiter="",
        min_width=2,
        source_codes="embed",
        **_TEMPLATES,
    )
    assert _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code") == [
        ("BH51", "BH5101", "BH51010102"),
        ("BH51", "BH5102", "BH51020101"),
        ("BH52", "BH5201", "BH52010202"),
        ("BH52", "BH5202", "BH52020201"),
    ]


def test_embed_raises_on_mixed_length_codes_without_delimiter(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(input_path, _source_coded_rows(("101", "0102", "0201", "0202")))
    with pytest.raises(ValueError, match="vary in length"):
        code_create(
            input_path,
            root_code="BH",
            delimiter="",
            min_width=2,
            source_codes="embed",
            **_TEMPLATES,
        )


def test_replace_without_delimiter_numbers_each_level(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    code_create(
        input_path,
        output_path,
        root_code="BH",
        delimiter="",
        min_width="auto",
        **_TEMPLATES,
    )
    assert _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code")[0] == (
        "BH1",
        "BH11",
        "BH111",
    )


def test_fixed_width_overflow_raises_without_delimiter(tmp_path):
    input_path = tmp_path / "in.parquet"
    rows = [
        {
            "adm1_code": "51",
            "adm1_name": "North",
            "adm2_name": f"Unit {i:02d}",
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i in range(10)
    ]
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="past the top 10%"):
        code_create(input_path, root_code="BH", delimiter="", min_width=1, **_TEMPLATES)


def _government_rows(codes):
    return [
        {
            "adm1_code": c1,
            "adm1_name": f"A{c1}",
            "adm2_code": c2,
            "adm2_name": f"B{c2}",
            "adm3_code": c3,
            "adm3_name": f"C{c3}",
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, (c1, c2, c3) in enumerate(codes)
    ]


@pytest.mark.parametrize(
    "codes",
    [
        [("11", "22", "33"), ("11", "23", "34")],
        [("11", "1122", "112233"), ("11", "1123", "112334")],
        [("XY11", "XY1122", "XY112233"), ("XY11", "XY1123", "XY112334")],
    ],
    ids=["local", "hierarchical", "pcodes"],
)
def test_embed_accepts_local_or_parent_prefixed_codes(tmp_path, codes):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _government_rows(codes))
    code_create(
        input_path,
        output_path,
        root_code="XY",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        **_TEMPLATES,
    )
    assert _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code") == [
        ("XY11", "XY1122", "XY112233"),
        ("XY11", "XY1123", "XY112334"),
    ]


@pytest.mark.parametrize(
    ("codes", "min_width", "expected"),
    [
        (
            [(1, 101, 10101), (11, 1105, 110501)],
            "auto",
            [("XY01", "XY0101", "XY010101"), ("XY11", "XY1105", "XY110501")],
        ),
        (
            [(1, 1, 1), (11, 2, 10)],
            3,
            [
                ("XY001", "XY001001", "XY001001001"),
                ("XY011", "XY011002", "XY011002010"),
            ],
        ),
        (
            [(1, 101, 10101), (11, 1105, 110501)],
            3,
            [
                ("XY001", "XY001001", "XY001001001"),
                ("XY011", "XY011005", "XY011005001"),
            ],
        ),
        (
            [(1, 1, 1), (11, 2, 10)],
            "2,3,4",
            [("XY01", "XY01001", "XY010010001"), ("XY11", "XY11002", "XY110020010")],
        ),
    ],
    ids=["auto", "min-width", "prefixed-min-width", "per-level"],
)
def test_embed_pads_integer_source_codes(tmp_path, codes, min_width, expected):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _government_rows(codes))
    code_create(
        input_path,
        output_path,
        root_code="XY",
        delimiter="",
        min_width=min_width,
        source_codes="embed",
        **_TEMPLATES,
    )
    assert _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code") == (
        expected
    )


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (
            [(11, 110, 1), (11, 1100, 2)],
            [("XY11", "XY110110", "XY1101101"), ("XY11", "XY111100", "XY1111002")],
        ),
        (
            [(1, n, n) for n in range(1, 13)],
            [("XY1", f"XY1{n:02d}", f"XY1{n:02d}{n:02d}") for n in range(1, 13)],
        ),
    ],
    ids=["mixed-remainder", "some-repeat-parent"],
)
def test_embed_pads_integers_numbered_within_parent(tmp_path, codes, expected):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _government_rows(codes))
    code_create(
        input_path,
        output_path,
        root_code="XY",
        delimiter="",
        min_width="auto",
        source_codes="embed",
        **_TEMPLATES,
    )
    assert _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code") == (
        expected
    )


def test_embed_raises_when_only_some_codes_carry_the_parent(tmp_path):
    input_path = tmp_path / "in.parquet"
    codes = [("11", "1122", "33"), ("11", "1123", "112334")]
    _write_synthetic(input_path, _government_rows(codes))
    with pytest.raises(ValueError, match="1 of 2 source codes"):
        code_create(
            input_path,
            root_code="XY",
            delimiter="",
            min_width="auto",
            source_codes="embed",
            **_TEMPLATES,
        )


def test_copy_keeps_source_codes_in_numbered_siblings(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    code_create(
        input_path,
        output_path,
        root_code="BHR",
        delimiter=".",
        min_width=3,
        source_codes="copy",
        **_TEMPLATES,
    )
    assert _fetch(
        output_path,
        "adm1_code, adm1_code1, adm2_code, adm3_code, adm3_code1",
        "adm3_code",
    ) == [
        ("BHR.001", "51", "BHR.001.001", "BHR.001.001.001", "0102"),
        ("BHR.001", "51", "BHR.001.002", "BHR.001.002.001", "0101"),
        ("BHR.002", "52", "BHR.002.001", "BHR.002.001.001", "0202"),
        ("BHR.002", "52", "BHR.002.002", "BHR.002.002.001", "0201"),
    ]
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    ]
    for code in ("adm1_code", "adm3_code"):
        assert columns.index(f"{code}1") == columns.index(code) + 1
    assert columns.index("adm2_code") == columns.index("adm2_name") + 1


@pytest.mark.parametrize(("adm1_code", "expected"), [("51", True), (None, False)])
def test_copy_keeps_adm0_code_only_alongside_adm1(tmp_path, adm1_code, expected):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = _source_coded_rows()
    for row in rows:
        row.update(adm0_code="BH", adm0_name="Bahrain", adm1_code=adm1_code)
    _write_synthetic(input_path, rows)
    code_create(
        input_path,
        output_path,
        root_code="BHR",
        delimiter=".",
        min_width=3,
        source_codes="copy",
        **_TEMPLATES,
    )
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    ]
    assert ("adm0_code1" in columns) is expected


def test_min_width_per_level_and_auto(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    for min_width, expected in [
        ("1,2,3", ("BHR.1", "BHR.1.01", "BHR.1.01.001")),
        ("auto", ("BHR.1", "BHR.1.1", "BHR.1.1.1")),
    ]:
        output_path = tmp_path / f"out_{min_width}.parquet"
        code_create(
            input_path,
            output_path,
            root_code="BHR",
            delimiter=".",
            min_width=min_width,
            **_TEMPLATES,
        )
        assert (
            _fetch(output_path, "adm1_code, adm2_code, adm3_code", "adm3_code")[0]
            == expected
        )


def test_min_width_list_must_match_level_count(tmp_path):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    with pytest.raises(ValueError, match="3 level"):
        code_create(
            input_path, root_code="BHR", delimiter=".", min_width="2,3", **_TEMPLATES
        )


def test_all_blank_code_column_is_numbered_by_name(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = _source_coded_rows()
    for row, blank in zip(rows, [None, "", " ", None], strict=True):
        row["adm3_code"] = blank
    _write_synthetic(input_path, rows)
    code_create(
        input_path, output_path, root_code="BH", delimiter="", min_width=1, **_TEMPLATES
    )
    assert _fetch(output_path, "adm3_code, adm3_name", "adm3_code") == [
        ("BH111", "102"),
        ("BH121", "101"),
        ("BH211", "202"),
        ("BH221", "201"),
    ]


@pytest.mark.parametrize("source_codes", ["replace", "copy", "embed"])
def test_partly_missing_codes_raise(tmp_path, source_codes):
    input_path = tmp_path / "in.parquet"
    rows = _source_coded_rows()
    rows[1]["adm3_code"] = ""
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="1 row\\(s\\) with no source code"):
        code_create(
            input_path,
            root_code="BH",
            delimiter="",
            min_width="auto",
            source_codes=source_codes,
            **_TEMPLATES,
        )


def test_integer_source_codes(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = [
        {
            "adm0_code": 13,
            "adm1_code": c1,
            "adm1_name": f"A{c1}",
            "adm2_code": c2,
            "adm2_name": f"B{c2}",
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, (c1, c2) in enumerate([(7, 70), (7, 71), (8, 80)])
    ]
    _write_synthetic(input_path, rows)
    code_create(
        input_path,
        output_path,
        root_code="XY",
        delimiter="",
        min_width="auto",
        source_codes="copy",
        **_TEMPLATES,
    )
    assert _fetch(
        output_path,
        "adm0_code, adm0_code1, adm1_code, adm1_code1, adm2_code, adm2_code1",
        "adm2_code",
    ) == [
        ("XY", "13", "XY1", "7", "XY11", "70"),
        ("XY", "13", "XY1", "7", "XY12", "71"),
        ("XY", "13", "XY2", "8", "XY21", "80"),
    ]


def test_structural_detection_ignores_empty_alternate_names(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = [
        {
            "adm1_pcode": f"XY{p}",
            "adm1_name": f"P{p}",
            "adm2_pcode": f"XY{p}{c}",
            "adm2_name": f"C{p}{c}",
            "adm2_name1": None,
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, (p, c) in enumerate(
            (p, c) for p in ("01", "02") for c in ("01", "02", "03", "04", "05")
        )
    ]
    _write_synthetic(input_path, rows)
    code_create(input_path, output_path, root_code="XY", delimiter="", min_width=2)
    assert _fetch(output_path, "count(DISTINCT adm2_pcode)", "1") == [(10,)]


def test_same_named_siblings_seeded_from_names_raise(tmp_path):
    input_path = tmp_path / "in.parquet"
    rows = [
        {
            "adm1_code": "51",
            "adm1_name": "North",
            "adm2_name": name,
            "wkt": f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))",
        }
        for i, name in enumerate(["Hill", "Hill", "Vale"])
    ]
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="'51 > Hill'"):
        code_create(input_path, root_code="XY", delimiter="", min_width=2, **_TEMPLATES)


def test_template_writes_root_code_at_level_0(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = [{"adm3_name": r.pop("adm3_name"), **r} for r in _source_coded_rows()]
    _write_synthetic(input_path, rows)
    code_create(
        input_path,
        output_path,
        root_code="BH",
        delimiter=".",
        min_width=1,
        **_TEMPLATES,
    )
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    ]
    assert columns.index("adm0_code") == columns.index("adm1_name") + 1
    assert _fetch(output_path, "DISTINCT adm0_code", "1") == [("BH",)]


def test_structural_detection_adds_no_level_0(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _source_coded_rows())
    code_create(input_path, output_path, root_code="BH", delimiter=".", min_width=1)
    columns = [
        r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{output_path}'").fetchall()
    ]
    assert not any(c.startswith("adm0") for c in columns)


def _two_root_rows() -> list[dict]:
    rows = []
    for x, root in enumerate(["AAA", "BBB"]):
        for a in range(2):
            for b in range(2):
                left = x * 4 + a * 2 + b
                rows.append(
                    {
                        "iso3": root,
                        "adm1_pcode": f"{root}{a}",
                        "adm1_name": f"{root} Region {a}",
                        "adm2_pcode": f"{root}{a}{b}",
                        "adm2_name": f"{root} District {a}{b}",
                        "wkt": f"POLYGON(({left} 0, {left + 1} 0, {left + 1} 1, "
                        f"{left} 1, {left} 0))",
                    }
                )
    return rows


def test_root_code_field_numbers_each_root_apart(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _two_root_rows())
    code_create(
        input_path, output_path, root_code_field="iso3", delimiter=".", min_width=2
    )
    combined = _fetch(output_path, "iso3, adm1_pcode, adm2_pcode", "adm2_name")
    separate = []
    for root in ["AAA", "BBB"]:
        rows = [r for r in _two_root_rows() if r["iso3"] == root]
        one_in, one_out = tmp_path / f"{root}.parquet", tmp_path / f"{root}_out.parquet"
        _write_synthetic(one_in, rows)
        code_create(one_in, one_out, root_code=root, delimiter=".", min_width=2)
        separate += _fetch(one_out, "iso3, adm1_pcode, adm2_pcode", "adm2_name")
    assert combined == separate
    assert ("BBB", "BBB.01", "BBB.01.01") in combined


def test_root_code_field_with_templates_keeps_level_0_column(tmp_path):
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    rows = [{"adm0_pcode": r.pop("iso3"), **r} for r in _two_root_rows()]
    _write_synthetic(input_path, rows)
    code_create(
        input_path,
        output_path,
        root_code_field="adm0_pcode",
        delimiter=".",
        min_width=2,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
    )
    assert _fetch(output_path, "DISTINCT adm0_pcode, adm1_pcode", "2") == [
        ("AAA", "AAA.01"),
        ("AAA", "AAA.02"),
        ("BBB", "BBB.01"),
        ("BBB", "BBB.02"),
    ]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({}, "exactly one"),
        ({"root_code": "X", "root_code_field": "iso3"}, "exactly one"),
        ({"root_code_field": "missing"}, "not found"),
    ],
)
def test_root_code_field_errors(tmp_path, kwargs, match):
    input_path = tmp_path / "in.parquet"
    _write_synthetic(input_path, _two_root_rows())
    with pytest.raises(ValueError, match=match):
        code_create(input_path, delimiter=".", min_width=2, **kwargs)


def test_blank_root_code_field_raises(tmp_path):
    input_path = tmp_path / "in.parquet"
    rows = _two_root_rows()
    rows[0]["iso3"] = " "
    _write_synthetic(input_path, rows)
    with pytest.raises(ValueError, match="blank on 1 row"):
        code_create(input_path, root_code_field="iso3", delimiter=".", min_width=2)


def _uneven_root_rows() -> list[dict]:
    rows = []
    for root, count in [("AAA", 2), ("BBB", 12)]:
        for a in range(1, count + 1):
            left = len(rows)
            rows.append(
                {
                    "iso3": root,
                    "adm1_pcode": a,
                    "adm1_name": f"{root} Region {a}",
                    "wkt": f"POLYGON(({left} 0, {left + 1} 0, {left + 1} 1, "
                    f"{left} 1, {left} 0))",
                }
            )
    return rows


@pytest.mark.parametrize(
    ("delimiter", "source_codes"), [(".", "replace"), ("", "embed")]
)
def test_root_code_field_auto_width_per_root(tmp_path, delimiter, source_codes):
    fields = {"name_field": "adm{n}_name", "code_field": "adm{n}_pcode"}
    common = {"delimiter": delimiter, "min_width": "auto", **fields}
    common["source_codes"] = source_codes
    input_path, output_path = tmp_path / "in.parquet", tmp_path / "out.parquet"
    _write_synthetic(input_path, _uneven_root_rows())
    code_create(input_path, output_path, root_code_field="iso3", **common)
    combined = _fetch(output_path, "adm1_name, adm1_pcode", "adm1_name")
    separate = []
    for root in ["AAA", "BBB"]:
        rows = [r for r in _uneven_root_rows() if r["iso3"] == root]
        one_in, one_out = tmp_path / f"{root}.parquet", tmp_path / f"{root}_out.parquet"
        _write_synthetic(one_in, rows)
        code_create(one_in, one_out, root_code=root, **common)
        separate += _fetch(one_out, "adm1_name, adm1_pcode", "adm1_name")
    assert combined == sorted(separate)
    assert ("AAA Region 1", f"AAA{delimiter}1") in combined
