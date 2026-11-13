"""Portability smoke tests: does change() run to completion on this machine.

Not a topology suite: `_03_classify.py`'s ported classification logic is
non-obvious enough that tests assert specific outcomes, not just "did it run."
"""

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.change import change
from topo_tools.cli.main import cli

# Nine spatially-separated regions, one relationship class (or pair) each.
_OLD_WKT = [
    ("U1", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    # unchanged; code -> N1B, renamed under --link-by-code
    ("N1", "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),
    # modified: new shrinks 10%, still passes tau_match
    ("M1", "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))"),
    # split: becomes SP1+SP2 in new
    ("SP1", "POLYGON((30 0, 33 0, 33 1, 30 1, 30 0))"),
    # merge: MG1+MG2 combine into one new unit
    ("MG1", "POLYGON((40 0, 41 0, 41 1, 40 1, 40 0))"),
    ("MG2", "POLYGON((41 0, 43 0, 43 1, 41 1, 41 0))"),
    ("RM1", "POLYGON((60 0, 61 0, 61 1, 60 1, 60 0))"),  # removed: old-only
    # relocated under --link-by-code: shifts to ~10% overlap
    ("RL1", "POLYGON((70 0, 71 0, 71 1, 70 1, 70 0))"),
    # split: only one new unit keeps the code (identity-claim guard)
    ("GD1", "POLYGON((90 0, 93 0, 93 1, 90 1, 90 0))"),
]

_NEW_WKT = [
    ("U1", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
    ("N1B", "POLYGON((10 0, 11 0, 11 1, 10 1, 10 0))"),  # code differs from old N1
    # modified: shrunk 10% from old, still passes tau_match
    ("M1", "POLYGON((20 0, 21 0, 21 0.9, 20 0.9, 20 0))"),
    ("SP1", "POLYGON((30 0, 31 0, 31 1, 30 1, 30 0))"),  # split from old SP1
    ("SP2", "POLYGON((31 0, 33 0, 33 1, 31 1, 31 0))"),  # split from old SP1
    ("MG1", "POLYGON((40 0, 43 0, 43 1, 40 1, 40 0))"),  # merge of old MG1+MG2
    ("CR1", "POLYGON((50 0, 51 0, 51 1, 50 1, 50 0))"),  # created: new-only
    # relocated: ~10% overlap with old RL1
    ("RL1", "POLYGON((70.9 0, 71.9 0, 71.9 1, 70.9 1, 70.9 0))"),
    # split from old GD1: GD1 keeps the code, GD2 gets a new one (identity guard)
    ("GD1", "POLYGON((90 0, 91 0, 91 1, 90 1, 90 0))"),
    ("GD2", "POLYGON((91 0, 93 0, 93 1, 91 1, 91 0))"),
]

_STEPS = ["inputs", "overlap", "classify", "outputs"]


def _write_synthetic(path, wkt_rows):
    values = ", ".join(
        f"('{pcode}', ST_GeomFromText('{wkt}'))" for pcode, wkt in wkt_rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(pcode, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def old_layer(tmp_path):
    path = tmp_path / "old.parquet"
    _write_synthetic(path, _OLD_WKT)
    return path


@pytest.fixture
def new_layer(tmp_path):
    path = tmp_path / "new.parquet"
    _write_synthetic(path, _NEW_WKT)
    return path


def _read_changelog(path):
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"SELECT * FROM '{path}'").fetchall()


def _class_of(rows, code_a=None, code_b=None):
    """relationship_class for the row matching the given code_a/code_b."""
    for row in rows:
        if (code_a is None or row[0] == code_a) and (
            code_b is None or row[2] == code_b
        ):
            return row[4]
    return None


def test_cli_help():
    result = CliRunner().invoke(cli, ["change", "--help"])
    assert result.exit_code == 0
    assert "Compare two versions of a polygon layer" in result.output
    assert "Examples:" in result.output


def test_change_full_run(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "out.csv"
    overlay_path = tmp_path / "overlay.parquet"
    change(old_layer, new_layer, output_path, overlay_path, overwrite=True)

    assert output_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert overlay_path.exists()
    rows = _read_changelog(output_path)
    # code_a/code_b are NULL (no --code-column-a/-b set); geometry-only
    # classification still works, this only checks classes, not code linkage.
    classes = {row[4] for row in rows}
    assert classes == {"unchanged", "modified", "split", "merge", "created", "removed"}


def test_change_default_output_paths(old_layer, new_layer):
    change(old_layer, new_layer, overwrite=True)

    expected_output = (
        old_layer.parent / f"{old_layer.stem}_{new_layer.stem}_changelog.csv"
    )
    expected_overlay = expected_output.with_stem(
        expected_output.stem + "_overlay"
    ).with_suffix(old_layer.suffix)
    assert expected_output.exists()
    assert expected_overlay.exists()


def test_change_tau_match_threshold(old_layer, new_layer, tmp_path):
    """RL1's ~10% overlap is below the default tau_match; lowering it links the pair.

    code_column_a/-b are set (no --link-by-code) purely so the changelog's
    display columns populate for row lookup; classification stays spatial-only.
    """
    default_out = tmp_path / "default.csv"
    change(
        old_layer,
        new_layer,
        default_out,
        tmp_path / "default_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    default_rows = _read_changelog(default_out)
    assert _class_of(default_rows, code_a="RL1") == "removed"
    assert _class_of(default_rows, code_b="RL1") == "created"

    loose_out = tmp_path / "loose.csv"
    change(
        old_layer,
        new_layer,
        loose_out,
        tmp_path / "loose_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        tau_match=0.05,
        overwrite=True,
    )
    loose_rows = _read_changelog(loose_out)
    assert _class_of(loose_rows, code_a="RL1", code_b="RL1") == "modified"


def test_change_tau_same_threshold(old_layer, new_layer, tmp_path):
    """M1's IoU of 0.9 is below the default tau_same but above a loosened one."""
    default_out = tmp_path / "default.csv"
    change(
        old_layer,
        new_layer,
        default_out,
        tmp_path / "default_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    assert _class_of(_read_changelog(default_out), code_a="M1") == "modified"

    loose_out = tmp_path / "loose.csv"
    change(
        old_layer,
        new_layer,
        loose_out,
        tmp_path / "loose_overlay.parquet",
        code_column_a="pcode",
        code_column_b="pcode",
        tau_same=0.5,
        overwrite=True,
    )
    assert _class_of(_read_changelog(loose_out), code_a="M1") == "unchanged"


def test_change_link_by_code(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "linked.csv"
    change(
        old_layer,
        new_layer,
        output_path,
        tmp_path / "linked_overlay.parquet",
        link_by_code=True,
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="N1", code_b="N1B") == "renamed"
    assert _class_of(rows, code_a="RL1", code_b="RL1") == "relocated"


def test_change_identity_claim_guard(old_layer, new_layer, tmp_path):
    """A genuine split (GD1 -> GD1 + GD2) must not collapse into a false 1:1 match."""
    output_path = tmp_path / "guard.csv"
    change(
        old_layer,
        new_layer,
        output_path,
        tmp_path / "guard_overlay.parquet",
        link_by_code=True,
        code_column_a="pcode",
        code_column_b="pcode",
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="GD1", code_b="GD1") == "split"
    assert _class_of(rows, code_a="GD1", code_b="GD2") == "split"


def _write_hierarchy(path, wkt_rows):
    """Two-level COD-AB layer: each unit's code under an adm1 parent, plus names."""
    values = ", ".join(
        f"('P{pcode[0]}', 'Region {pcode[0]}', '{pcode}', 'Town {pcode}', "
        f"ST_GeomFromText('{wkt}'))"
        for pcode, wkt in wkt_rows
    )
    columns = "adm1_pcode, adm1_name, adm2_pcode, adm2_name, geom"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS t({columns})) TO '{path}'"
        )


@pytest.mark.parametrize("link_by_code", [False, True])
def test_change_detects_finest_level_columns(tmp_path, link_by_code):
    """Without explicit columns, the finest level's code and name fill the changelog."""
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    _write_hierarchy(old, _OLD_WKT)
    _write_hierarchy(new, _NEW_WKT)
    output_path = tmp_path / "auto.csv"
    change(
        old,
        new,
        output_path,
        tmp_path / "auto_overlay.parquet",
        link_by_code=link_by_code,
        overwrite=True,
    )
    rows = _read_changelog(output_path)
    unchanged = next(row for row in rows if row[0] == "U1")
    assert unchanged[:4] == ("U1", "Town U1", "U1", "Town U1")
    renamed = "renamed" if link_by_code else "unchanged"
    assert _class_of(rows, code_a="N1", code_b="N1B") == renamed


def test_change_field_templates(tmp_path):
    """--name-field-*/--code-field-* pick the levels; only one of a pair raises."""
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    _write_hierarchy(old, _OLD_WKT)
    _write_hierarchy(new, _NEW_WKT)
    fields = {"name_field_a": "adm{n}_name", "code_field_a": "adm{n}_pcode"}
    output_path = tmp_path / "fields.csv"
    change(old, new, output_path, tmp_path / "fields.parquet", **fields)
    assert ("U1", "Town U1") in {row[:2] for row in _read_changelog(output_path)}
    with pytest.raises(ValueError, match="must be given together"):
        change(old, new, output_path, code_field_b="adm{n}_pcode")


def test_change_link_by_code_without_detected_column(old_layer, new_layer, tmp_path):
    with pytest.raises(ValueError, match="--link-by-code needs a code column"):
        change(
            old_layer,
            new_layer,
            tmp_path / "out.csv",
            tmp_path / "out_overlay.parquet",
            link_by_code=True,
            overwrite=True,
        )


def test_change_invalid_output_format(old_layer, new_layer, tmp_path):
    with pytest.raises(ValueError, match="tabular"):
        change(old_layer, new_layer, tmp_path / "out.gpkg", overwrite=True)


def test_change_invalid_overlay_format(old_layer, new_layer, tmp_path):
    with pytest.raises(ValueError, match="overlay"):
        change(
            old_layer,
            new_layer,
            tmp_path / "out.csv",
            tmp_path / "overlay.csv",
            overwrite=True,
        )


def test_cli_positional_args(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "cli_out.csv"
    result = CliRunner().invoke(
        cli, ["change", str(old_layer), str(new_layer), str(output_path)]
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_change_error_on_existing_output(old_layer, new_layer, tmp_path):
    output_path = tmp_path / "exists.csv"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        [
            "change",
            str(old_layer),
            str(new_layer),
            str(output_path),
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_change_steps(old_layer, new_layer, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.csv"
    overlay_path = tmp_path / "steps_overlay.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        change(
            old_layer,
            new_layer,
            output_path,
            overlay_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()
    assert overlay_path.exists()


def _write_named(path, rows):
    values = ", ".join(
        f"('{code}', '{name}', ST_GeomFromText('{wkt}'))" for code, name, wkt in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS t(adm1_code, adm1_name, geom)) "
            f"TO '{path}'"
        )


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ({"link_by_code": True}, "renamed"),
        ({"link_by_name": True}, "renamed"),
        ({"link_by_code": True, "link_by_name": True}, "renamed"),
        ({}, "unchanged"),
    ],
)
def test_change_name_only_change_is_renamed_when_linked(tmp_path, link, expected):
    """Same code and shape with a new name is renamed under either link flag."""
    north = "POLYGON((0 1, 2 1, 2 2, 0 2, 0 1))"
    south = "POLYGON((0 0, 2 0, 2 1, 0 1, 0 0))"
    old, new = tmp_path / "old.parquet", tmp_path / "new.parquet"
    _write_named(old, [("XX01", "North", north), ("XX02", "South", south)])
    _write_named(new, [("XX01", "Northern", north), ("XX02", "South", south)])
    output_path = tmp_path / "named.csv"
    change(old, new, output_path, tmp_path / "named_overlay.parquet", **link)
    rows = _read_changelog(output_path)
    assert _class_of(rows, code_a="XX01", code_b="XX01") == expected
    assert _class_of(rows, code_a="XX02", code_b="XX02") == "unchanged"
