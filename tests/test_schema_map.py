"""Smoke tests for schema-map's crosswalk inference (--map-only)."""

import csv

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.schema_map import map as schema_map
from topo_tools.cli.main import cli

_STEPS = ["inputs", "map", "outputs"]


def map(path, output_path=None, **kwargs):  # noqa: A001
    schema_map(path, csv_output=output_path, map_only=True, **kwargs)


def _write_table(path, col_names, rows):
    def _cell(col, val):
        if col == "geom":
            return f"ST_GeomFromText('{val}')"
        return "NULL" if val is None else f"'{val}'"

    values = ", ".join(
        "(" + ", ".join(_cell(c, v) for c, v in zip(col_names, row, strict=True)) + ")"
        for row in rows
    )
    cols_decl = ", ".join(col_names)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t({cols_decl})"
        )
        conn.execute(f"COPY synth TO '{path}'")


def _unit_square(i):
    return f"POLYGON(({i} 0, {i + 1} 0, {i + 1} 1, {i} 1, {i} 0))"


def _crosswalk(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return {row["source_column"]: row for row in csv.DictReader(f)}


@pytest.fixture
def chain_schema():
    """Naming templates decoupled from source names, level != source level."""
    return {"name_field": "level{n}_name", "code_field": "level{n}_pcode"}


@pytest.fixture
def chain_input(tmp_path):
    """2-level pcode chain, a name column, a non-nesting code, an unmatched column."""
    path = tmp_path / "chain.parquet"
    rows = [
        (_unit_square(0), "R0", "R0-1", "R0X1", "Alpha", "Miscellaneous One"),
        (_unit_square(1), "R0", "R0-1", "R0X2", "Alpha", "Miscellaneous Two"),
        (_unit_square(2), "R0", "R0-2", "R0X1", "Beta", "Miscellaneous Three"),
        (_unit_square(3), "R0", "R0-2", "R0X2", "Beta", "Miscellaneous One"),
    ]
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm1_pcode", "decoy_code", "adm1_name", "notes"],
        rows,
    )
    return path


@pytest.fixture
def noise_input(tmp_path):
    """Build a real 2-level chain plus GDAL/GIS bookkeeping noise columns."""
    path = tmp_path / "noise.parquet"
    rows = [
        (_unit_square(0), "R0", "R1", "1", "1", "1"),
        (_unit_square(1), "R0", "R1", "2", "2", "2"),
        (_unit_square(2), "R0", "R2", "3", "3", "3"),
        (_unit_square(3), "R0", "R2", "4", "4", "4"),
    ]
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm1_pcode", "OGC_FID", "ogc_fid_orig", "OBJECTID"],
        rows,
    )
    return path


def test_cli_help():
    result = CliRunner().invoke(cli, ["schema-map", "--help"])
    assert result.exit_code == 0
    assert "Rename a layer's admin columns" in result.output
    assert "Examples:" in result.output


def test_code_and_name_tiers(chain_input, chain_schema, tmp_path):
    out = tmp_path / "crosswalk.csv"
    map(chain_input, out, **chain_schema, overwrite=True)

    rows = _crosswalk(out)
    assert rows["adm0_pcode"]["target_column"] == ""
    assert rows["adm0_pcode"]["note"] == ""
    assert rows["adm1_pcode"]["target_column"] == "level1_pcode"
    assert rows["adm1_pcode"]["note"] == ""
    assert rows["adm1_pcode"]["unique_count"] == "2"
    assert rows["adm1_name"]["target_column"] == "level1_name"
    assert rows["adm1_name"]["note"] == ""
    assert rows["adm1_name"]["unique_count"] == "2"
    assert rows["decoy_code"]["target_column"] == ""
    assert rows["decoy_code"]["note"] == "ambiguous, level 1"
    assert rows["notes"]["target_column"] == ""
    assert rows["notes"]["note"] == ""


def test_unique_count_reveals_name_reused_across_parents(tmp_path):
    """Central repeats under both provinces: 3 raw names, 4 real units."""
    path = tmp_path / "reused_name.parquet"
    rows = [
        (_unit_square(0), "P1", "P101", "Central"),
        (_unit_square(1), "P1", "P102", "North"),
        (_unit_square(2), "P2", "P201", "Central"),
        (_unit_square(3), "P2", "P202", "South"),
    ]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", "adm1_name"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, output_path=out, level=1, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm1_name"]["target_column"] == "adm1_name"
    assert rows_out["adm1_name"]["unique_count"] == "4"
    assert rows_out["adm1_pcode"]["unique_count"] == "4"


def test_noise_columns_excluded_entirely(noise_input, tmp_path):
    out = tmp_path / "crosswalk.csv"
    map(noise_input, output_path=out, overwrite=True)

    rows = _crosswalk(out)
    assert "OGC_FID" not in rows
    assert "ogc_fid_orig" not in rows
    assert "OBJECTID" not in rows


def test_output_ordering_level_desc_name_before_code_unmatched_last(
    chain_input, chain_schema, tmp_path
):
    out = tmp_path / "crosswalk.csv"
    map(chain_input, out, **chain_schema, overwrite=True)

    with out.open(newline="", encoding="utf-8-sig") as f:
        ordered = [row["source_column"] for row in csv.DictReader(f)]
    assert ordered == ["adm1_name", "adm1_pcode", "decoy_code", "adm0_pcode", "notes"]


def test_target_schema_defaults_to_bundled_default(tmp_path):
    path = tmp_path / "cod_ab_like.parquet"
    rows = [
        (_unit_square(0), "MG", "MG1", "Alpha"),
        (_unit_square(1), "MG", "MG2", "Beta"),
    ]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", "adm1_name"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, output_path=out, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["target_column"] == "adm1_code"
    assert rows_out["adm1_pcode"]["note"] == ""
    assert rows_out["adm1_pcode"]["unique_count"] == "2"
    assert rows_out["adm1_name"]["target_column"] == "adm1_name"
    assert rows_out["adm1_name"]["note"] == ""
    assert rows_out["adm1_name"]["unique_count"] == "2"


def test_cli_target_schema_defaults_to_bundled_default(tmp_path):
    path = tmp_path / "cod_ab_like.parquet"
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm0_name"],
        [(_unit_square(0), "MG0", "Madagascar")],
    )
    result = CliRunner().invoke(cli, ["schema-map", str(path)])
    assert result.exit_code == 0, result.output

    out = path.with_stem(path.stem + "_crosswalk").with_suffix(".csv")
    rows = _crosswalk(out)
    assert rows["adm0_pcode"]["target_column"] == ""


def test_name_field_code_field_must_be_given_together_raises(chain_input):
    with pytest.raises(ValueError, match="must be given together"):
        map(chain_input, name_field="adm{n}_name", overwrite=True)


def test_target_schema_missing_placeholder_raises(chain_input):
    with pytest.raises(ValueError, match="must both contain a"):
        map(
            chain_input,
            name_field="adm_name",
            code_field="adm{n}_pcode",
            overwrite=True,
        )


def test_default_output_path(chain_input, chain_schema):
    map(chain_input, **chain_schema, overwrite=True)

    expected = chain_input.with_stem(chain_input.stem + "_crosswalk").with_suffix(
        ".csv"
    )
    assert expected.exists()


def test_cli_error_on_existing_output(chain_input, chain_schema, tmp_path):
    out = tmp_path / "exists.csv"
    out.touch()
    result = CliRunner().invoke(
        cli,
        [
            "schema-map",
            str(chain_input),
            "--map-only",
            "--csv-output",
            str(out),
            "--name-field",
            chain_schema["name_field"],
            "--code-field",
            chain_schema["code_field"],
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert "output already exists" in result.output


def test_map_steps(chain_input, chain_schema, tmp_path):
    out = tmp_path / "steps_crosswalk.csv"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        map(
            chain_input,
            out,
            **chain_schema,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert out.exists()


def test_constant_bare_letter_code_reclassified(tmp_path):
    path = tmp_path / "bare.parquet"
    rows = [
        (_unit_square(0), "MG", "MG11", "Alpha"),
        (_unit_square(1), "MG", "MG11", "Alpha"),
        (_unit_square(2), "MG", "MG12", "Beta"),
        (_unit_square(3), "MG", "MG12", "Beta"),
    ]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", "adm1_name"], rows)
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["note"] == ""
    assert rows_out["adm1_pcode"]["unique_count"] == "2"
    assert rows_out["adm1_name"]["note"] == ""
    assert rows_out["adm1_name"]["unique_count"] == "2"


def test_name_bracket_group_numbers_multiple_name_candidates(tmp_path):
    path = tmp_path / "tie.parquet"
    rows = [
        (_unit_square(0), "R0", "R0R1", "Alpha", "TypeX"),
        (_unit_square(1), "R0", "R0R1", "Alpha", "TypeX"),
        (_unit_square(2), "R0", "R0R2", "Beta", "TypeY"),
        (_unit_square(3), "R0", "R0R2", "Beta", "TypeY"),
    ]
    _write_table(
        path, ["geom", "adm0_pcode", "adm1_pcode", "adm1_name", "tied_name"], rows
    )
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm1_name"]["target_column"] == "level1_name"
    assert rows_out["tied_name"]["target_column"] == "level1_name1"


def test_admin_level_zero_never_resolved(tmp_path):
    path = tmp_path / "level_zero.parquet"
    rows = [
        (_unit_square(0), "MG", "Madagascar", "MGR1", "Alpha"),
        (_unit_square(1), "MG", "Madagascar", "MGR1", "Alpha"),
        (_unit_square(2), "MG", "Madagascar", "MGR2", "Beta"),
        (_unit_square(3), "MG", "Madagascar", "MGR2", "Beta"),
    ]
    _write_table(
        path, ["geom", "adm0_pcode", "adm0_name", "adm1_pcode", "adm1_name"], rows
    )
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="adm{n}_name",
        code_field="adm{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm0_pcode"]["target_column"] == ""
    assert rows_out["adm0_pcode"]["note"] == ""
    assert rows_out["adm0_name"]["target_column"] == ""
    assert rows_out["adm0_name"]["note"] == ""
    assert rows_out["adm1_pcode"]["note"] == ""
    assert rows_out["adm1_pcode"]["unique_count"] == "2"
    assert rows_out["adm1_name"]["note"] == ""
    assert rows_out["adm1_name"]["unique_count"] == "2"


def test_exact_bijective_match_wins_over_looser_function_match(tmp_path):
    path = tmp_path / "exact_match.parquet"
    rows = [
        (_unit_square(0), "R0", "R0R1", "Alpha", "Group1"),
        (_unit_square(1), "R0", "R0R2", "Beta", "Group1"),
        (_unit_square(2), "R0", "R0R3", "Gamma", "Group2"),
        (_unit_square(3), "R0", "R0R4", "Delta", "Group2"),
    ]
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm1_pcode", "adm1_name", "region_group"],
        rows,
    )
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm1_name"]["target_column"] == "level1_name"
    assert rows_out["region_group"]["target_column"] == ""
    assert rows_out["region_group"]["note"] == "supplemental, superset of level 1"


def test_embedded_parent_wins_chain_tie_over_unembedded_grouping(tmp_path):
    """A coarser grouping (district) nesting adm2 without embedding its codes."""
    path = tmp_path / "grouping.parquet"
    district = {"01": "S1", "02": "S1", "03": "S2", "04": "S2"}
    word = {
        "01": "North",
        "02": "South",
        "03": "East",
        "04": "West",
        "1": "Upper",
        "2": "Lower",
    }
    rows = [
        (
            _unit_square(i),
            "R0",
            f"R0{a}",
            f"Region {a}",
            f"R0{a}{district[b]}",
            f"R0{a}{b}",
            f"{a} {word[b]}",
            f"R0{a}{b}{c}",
            f"{a} {word[b]} {word[c]}",
        )
        for i, (a, b, c) in enumerate(
            (a, b, c) for a in "AB" for b in ("01", "02", "03", "04") for c in "12"
        )
    ]
    _write_table(
        path,
        [
            "geom",
            "adm0_pcode",
            "adm1_pcode",
            "adm1_name",
            "district",
            "adm2_pcode",
            "adm2_name",
            "adm3_pcode",
            "adm3_name",
        ],
        rows,
    )
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm2_pcode"]["target_column"] == "adm2_code"
    assert rows_out["adm3_pcode"]["target_column"] == "adm3_code"
    assert rows_out["district"]["note"] == "supplemental, superset of level 2"


def _unembedded_middle_crosswalk(tmp_path, middle, finest):
    """adm1 > middle > finest, finest's codes embedding adm1 but never middle's."""
    path = tmp_path / "middle.parquet"
    rows = [
        (_unit_square(i), "R0", f"R0{a}", f"R0{a}S{(b + 2) // 3}", f"R0{a}0{b}")
        for i, (a, b) in enumerate((a, b) for a in "AB" for b in range(1, 7))
    ]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", middle, finest], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)
    return _crosswalk(out)


def test_unembedded_grouping_off_the_naming_pattern_is_supplemental(tmp_path):
    rows_out = _unembedded_middle_crosswalk(tmp_path, "sendistpcode", "adm2_pcode")
    assert rows_out["adm2_pcode"]["target_column"] == "adm2_code"
    assert rows_out["sendistpcode"]["note"] == "supplemental, superset of level 2"


def test_unembedded_level_on_the_naming_pattern_stays_a_level(tmp_path):
    rows_out = _unembedded_middle_crosswalk(tmp_path, "adm2_pcode", "adm3_pcode")
    assert rows_out["adm2_pcode"]["target_column"] == "adm2_code"
    assert rows_out["adm3_pcode"]["target_column"] == "adm3_code"


def _twenty_unit_rows(cand_values):
    """20 admin1 units (bijective pcode/name), plus one candidate column.

    adm1_name uses letters, not digits, so it isn't mistaken for code-shaped.
    """
    return [
        (_unit_square(i), "R0", f"R0R{i}", f"Region{chr(64 + i)}", cand_values[i - 1])
        for i in range(1, 21)
    ]


def _write_twenty_unit_table(tmp_path, name, cand_values):
    path = tmp_path / f"{name}.parquet"
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm1_pcode", "adm1_name", "alt_name"],
        _twenty_unit_rows(cand_values),
    )
    return path


def test_near_bijective_alt_name_becomes_numbered_sibling(tmp_path):
    """A 5%-collapse candidate (1 coincidental duplicate) still numbers as name1."""
    values = [f"Nom{i}" for i in range(1, 21)]
    values[1] = values[0]  # Nom1 reused for unit 2: 19/20 distinct, 5% collapse.
    path = _write_twenty_unit_table(tmp_path, "near_bijective", values)
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm1_name"]["target_column"] == "level1_name"
    assert rows_out["alt_name"]["target_column"] == "level1_name1"
    assert rows_out["alt_name"]["note"] == ""


def test_collapse_over_threshold_stays_supplemental(tmp_path):
    """A 65%-collapse candidate (Algeria-shaped) is still a coarser grouping."""
    values = [f"Group{(i - 1) % 7}" for i in range(1, 21)]  # 7/20 distinct, 65%.
    path = _write_twenty_unit_table(tmp_path, "coarse_collapse", values)
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["alt_name"]["target_column"] == ""
    assert rows_out["alt_name"]["note"] == "supplemental, superset of level 1"


def test_collapse_near_threshold_boundary(tmp_path):
    """Pins the 0.30 cutoff region: 25% numbered, 35% stays supplemental.

    Avoids testing the literal 0.30 value itself: 1 - 14/20 float-rounds to
    0.30000000000000004, an unreliable equality to assert against.
    """
    numbered_values = [f"Nom{(i - 1) % 15}" for i in range(1, 21)]  # 15/20, 25%.
    path = _write_twenty_unit_table(tmp_path, "under_boundary", numbered_values)
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )
    rows_out = _crosswalk(out)
    assert rows_out["alt_name"]["target_column"] == "level1_name1"

    over_values = [f"Nom{(i - 1) % 13}" for i in range(1, 21)]  # 13/20, 35%.
    path2 = _write_twenty_unit_table(tmp_path, "over_boundary", over_values)
    out2 = tmp_path / "crosswalk2.csv"
    map(
        path2,
        out2,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )
    rows_out2 = _crosswalk(out2)
    assert rows_out2["alt_name"]["target_column"] == ""
    assert rows_out2["alt_name"]["note"] == "supplemental, superset of level 1"


def test_two_tolerated_siblings_get_sequential_numbering_no_collision(tmp_path):
    """Two in-tolerance bracket winners at an already-named level don't collide."""
    path = tmp_path / "two_siblings.parquet"
    values1 = [f"Nom{i}" for i in range(1, 21)]
    values1[1] = values1[0]  # 19/20 distinct.
    values2 = [f"Autre{i}" for i in range(1, 21)]
    values2[1] = values2[0]
    values2[3] = values2[2]  # 18/20 distinct.
    rows = [(*row, values2[i]) for i, row in enumerate(_twenty_unit_rows(values1))]
    _write_table(
        path,
        ["geom", "adm0_pcode", "adm1_pcode", "adm1_name", "alt_name1", "alt_name2"],
        rows,
    )
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    targets = {
        rows_out["adm1_name"]["target_column"],
        rows_out["alt_name1"]["target_column"],
        rows_out["alt_name2"]["target_column"],
    }
    assert targets == {"level1_name", "level1_name1", "level1_name2"}


def test_code_bracket_group_numbers_bijective_code_companions(tmp_path):
    path = tmp_path / "code_companions.parquet"
    rows = [
        (_unit_square(0), "R0", "R0R1", "R0ALT1"),
        (_unit_square(1), "R0", "R0R1", "R0ALT1"),
        (_unit_square(2), "R0", "R0R2", "R0ALT2"),
        (_unit_square(3), "R0", "R0R2", "R0ALT2"),
    ]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", "alt1_pcode"], rows)
    out = tmp_path / "crosswalk.csv"
    map(
        path,
        out,
        name_field="level{n}_name",
        code_field="level{n}_pcode",
        overwrite=True,
    )

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["target_column"] == "level1_pcode"
    assert rows_out["alt1_pcode"]["target_column"] == "level1_pcode1"


def test_parent_embedding_code_outranks_earlier_surrogate_id(tmp_path):
    path = tmp_path / "surrogate.parquet"
    rows = [
        (_unit_square(0), "199", "R0", "R0R1"),
        (_unit_square(1), "199", "R0", "R0R1"),
        (_unit_square(2), "303", "R0", "R0R2"),
        (_unit_square(3), "303", "R0", "R0R2"),
    ]
    _write_table(path, ["geom", "unit_id", "adm0_pcode", "adm1_pcode"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["target_column"] == "adm1_code"
    assert rows_out["unit_id"]["target_column"] == "adm1_code1"


def test_grass_cat_column_excluded_as_noise(tmp_path):
    path = tmp_path / "grass.parquet"
    rows = [
        (_unit_square(0), "65", "R0", "R0R1"),
        (_unit_square(1), "65", "R0", "R0R1"),
        (_unit_square(2), "14", "R0", "R0R2"),
        (_unit_square(3), "14", "R0", "R0R2"),
    ]
    _write_table(path, ["geom", "cat", "adm0_pcode", "adm1_pcode"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)

    assert "cat" not in _crosswalk(out)


def test_unedited_crosswalk_with_ambiguous_and_unmatched_survives_refactor(
    chain_input, chain_schema, tmp_path
):
    """An unedited crosswalk (blank-target columns dropped) must not raise."""
    crosswalk = tmp_path / "crosswalk.csv"
    map(chain_input, crosswalk, **chain_schema, overwrite=True)

    out = tmp_path / "mapped.parquet"
    schema_map(chain_input, out, csv_input=crosswalk, **chain_schema)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            r[0] for r in conn.execute(f"DESCRIBE SELECT * FROM '{out}'").fetchall()
        }
    assert columns == {
        "level1_pcode",
        "level1_name",
        "geometry",
    }


@pytest.fixture
def no_country_input(tmp_path):
    """2-level pcode chain with no constant country column."""
    path = tmp_path / "no_country.parquet"
    rows = [
        (_unit_square(0), "R1", "North", "R1D1", "A"),
        (_unit_square(1), "R1", "North", "R1D2", "B"),
        (_unit_square(2), "R2", "South", "R2D1", "C"),
        (_unit_square(3), "R2", "South", "R2D2", "D"),
    ]
    _write_table(path, ["geom", "reg_code", "reg_name", "dist_code", "dist_name"], rows)
    return path


def test_level_anchors_finest_level(no_country_input, tmp_path):
    out = tmp_path / "crosswalk.csv"
    map(no_country_input, out, level=2, overwrite=True)

    rows = _crosswalk(out)
    assert rows["dist_code"]["target_column"] == "adm2_code"
    assert rows["dist_name"]["target_column"] == "adm2_name"
    assert rows["reg_code"]["target_column"] == "adm1_code"
    assert rows["reg_name"]["target_column"] == "adm1_name"


def test_level_omitted_numbers_coarsest_one(no_country_input, tmp_path, caplog):
    out = tmp_path / "crosswalk.csv"
    map(no_country_input, out, overwrite=True)

    rows = _crosswalk(out)
    assert rows["reg_code"]["target_column"] == "adm1_code"
    assert rows["dist_code"]["target_column"] == "adm2_code"
    assert "no single-value country column" in caplog.text


def test_level_omitted_constant_root_no_warning(
    chain_input, chain_schema, tmp_path, caplog
):
    out = tmp_path / "crosswalk.csv"
    map(chain_input, out, **chain_schema, overwrite=True)

    assert _crosswalk(out)["adm1_pcode"]["target_column"] == "level1_pcode"
    assert "no single-value country column" not in caplog.text


def test_single_level_file_defaults_one_or_takes_level(tmp_path, caplog):
    path = tmp_path / "flat.parquet"
    rows = [
        (_unit_square(0), "C001", "Alpha"),
        (_unit_square(1), "C002", "Beta"),
        (_unit_square(2), "C003", "Gamma"),
        (_unit_square(3), "C004", "Delta"),
    ]
    _write_table(path, ["geom", "com_code", "com_name"], rows)
    out = tmp_path / "crosswalk.csv"

    map(path, out, overwrite=True)
    assert _crosswalk(out)["com_code"]["target_column"] == "adm1_code"
    assert "no single-value country column" in caplog.text

    map(path, out, level=3, overwrite=True)
    assert _crosswalk(out)["com_code"]["target_column"] == "adm3_code"


def test_level_skips_folded_constant_root(chain_input, chain_schema, tmp_path):
    out = tmp_path / "crosswalk.csv"
    map(chain_input, out, **chain_schema, level=3, overwrite=True)

    rows = _crosswalk(out)
    assert rows["adm0_pcode"]["target_column"] == ""
    assert rows["adm1_pcode"]["target_column"] == "level3_pcode"
    assert rows["adm1_name"]["target_column"] == "level3_name"
    assert rows["decoy_code"]["note"] == "ambiguous, level 3"


def test_level_too_shallow_raises(no_country_input, tmp_path):
    with pytest.raises(ValueError, match="too shallow"):
        map(no_country_input, tmp_path / "crosswalk.csv", level=0, overwrite=True)


def test_cli_level(no_country_input, tmp_path):
    out = tmp_path / "crosswalk.csv"
    result = CliRunner().invoke(
        cli,
        [
            "schema-map",
            str(no_country_input),
            "--map-only",
            "--csv-output",
            str(out),
            "--level",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _crosswalk(out)["reg_code"]["target_column"] == "adm1_code"


def test_single_row_constants_are_one_level(tmp_path):
    path = tmp_path / "admin0.parquet"
    columns = ["geom", "adm0_name", "adm0_pcode", "iso2", "version", "area_sqkm"]
    _write_table(path, columns, [(_unit_square(0), "Alpha", "AA", "A", "v1", 12.5)])
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)
    assert all(not r["target_column"] for r in _crosswalk(out).values())


def test_blank_constant_is_not_embedded_in_names(tmp_path):
    path = tmp_path / "blank.parquet"
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    rows = [
        (_unit_square(i), "AA", "Country", "", name, f"AA{i:02d}")
        for i, name in enumerate(names)
    ]
    columns = ["geom", "adm0_pcode", "adm0_name", "blank", "adm1_name", "adm1_pcode"]
    _write_table(path, columns, rows)
    out = tmp_path / "crosswalk.csv"
    map(path, out, overwrite=True)
    assert _crosswalk(out)["adm1_name"]["target_column"] == "adm1_name"


def test_name_containing_a_short_root_constant_stays_a_name(tmp_path):
    """Three names contain the root's `sk`, one doesn't: still not an embedding."""
    path = tmp_path / "lang.parquet"
    names = ["Nitriansky", "Trnavsky", "Presovsky", "Kosicky"]
    rows = [(_unit_square(i), "XY", "sk", f"XY{i}", n) for i, n in enumerate(names)]
    _write_table(path, ["geom", "adm0_pcode", "lang", "adm1_pcode", "adm1_name"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, output_path=out, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["target_column"] == "adm1_code"
    assert rows_out["adm1_name"]["target_column"] == "adm1_name"


def test_numbered_names_beside_an_embedding_code_stay_names(tmp_path):
    path = tmp_path / "numbered.parquet"
    rows = [(_unit_square(i), "XY", f"XY{i}", f"Region {i}") for i in range(4)]
    _write_table(path, ["geom", "adm0_pcode", "adm1_pcode", "adm1_name"], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, output_path=out, overwrite=True)

    rows_out = _crosswalk(out)
    assert rows_out["adm1_pcode"]["target_column"] == "adm1_code"
    assert rows_out["adm1_name"]["target_column"] == "adm1_name"


@pytest.mark.parametrize(
    ("coarser", "finer"), [("prov_2019", "dist_2019"), ("iso3", "adm3_pcode")]
)
def test_shared_digit_in_unrelated_names_keeps_two_levels(coarser, finer, tmp_path):
    path = tmp_path / "shared_digit.parquet"
    rows = [
        (_unit_square(4 * p + d), f"P{p}", f"Z{p}{d}x")
        for p in range(3)
        for d in range(4)
    ]
    _write_table(path, ["geom", coarser, finer], rows)
    out = tmp_path / "crosswalk.csv"
    map(path, output_path=out, overwrite=True)

    rows_out = _crosswalk(out)
    targets = [rows_out[c]["target_column"] for c in (coarser, finer)]
    assert all(targets), targets
    assert targets[0] != targets[1], targets
