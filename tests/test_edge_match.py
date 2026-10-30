"""Portability smoke tests: does match() run to completion on this machine.

Not a correctness suite: outputs.main already raises RuntimeError on
coverage violations, so a clean run is already vetted by the pipeline itself.
"""

import logging

import duckdb
import pytest
from click.testing import CliRunner

from topo_tools.api.edge_match import match
from topo_tools.cli.main import cli
from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.coverage import has_gaps
from topo_tools.core.edge_match import _01_inputs
from topo_tools.core.edge_match import _03_clip as match_clip
from topo_tools.core.edge_match._02_groups import _record_dropped_group

_LEVEL_1, _LEVEL_2 = 1, 2

# Overlay A contains inputs 1 & 2, Overlay B contains only input 3, input 4
# is far from both; --assign many gives this per input polygon grouping.
_INPUT_WKT = [
    (1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))"),
    (2, "POLYGON((1.5 0.5, 2 0.5, 2 1, 1.5 1, 1.5 0.5))"),
    (3, "POLYGON((11 1, 12 1, 12 2, 11 2, 11 1))"),
    (4, "POLYGON((20 0, 21 0, 21 1, 20 1, 20 0))"),
]
_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))"),
    (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))"),
]

_STEPS = ["inputs", "assign", "groups", "clip", "outputs"]


def _write_synthetic(path, wkt_rows):
    values = ", ".join(f"({fid}, ST_GeomFromText('{wkt}'))" for fid, wkt in wkt_rows)
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t(id, geom)"
        )
        conn.execute(f"COPY synth TO '{path}'")


@pytest.fixture
def synthetic_inputs(tmp_path):
    """Write a small synthetic input feature-layer GeoParquet, no real-world fixture."""
    path = tmp_path / "children.parquet"
    _write_synthetic(path, _INPUT_WKT)
    return path


@pytest.fixture
def synthetic_overlays(tmp_path):
    """Write a small synthetic overlay-layer GeoParquet."""
    path = tmp_path / "parents.parquet"
    _write_synthetic(path, _OVERLAY_WKT)
    return path


def test_inputs_cleans_input_but_loads_overlay_raw(tmp_path):
    """The input's sub-tolerance gap is closed; the same gap in the overlay is not."""
    w = SNAP_TOLERANCE / 2
    wkt = [
        (1, f"POLYGON((0 0, 101 0, 101 50, {50 + w} 50, 50 50, 0 50, 0 0))"),
        (
            2,
            (
                f"POLYGON((0 {50 + w}, 50 {50 + w}, {50 + w} {50 + w}, "
                f"101 {50 + w}, 101 101, 0 101, 0 {50 + w}))"
            ),
        ),
        (3, f"POLYGON((0 50, 50 50, 50 {50 + w}, 0 {50 + w}, 0 50))"),
        (
            4,
            (
                f"POLYGON(({50 + w} 50, 101 50, 101 {50 + w}, "
                f"{50 + w} {50 + w}, {50 + w} 50))"
            ),
        ),
    ]
    path = tmp_path / "gapped.parquet"
    _write_synthetic(path, wkt)

    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        _01_inputs.main(conn, "t", path, path)
        assert not has_gaps(conn, "t_input_01", gap_maximum_width=0)
        assert has_gaps(conn, "t_overlay_01", gap_maximum_width=0)


def test_cli_help():
    result = CliRunner().invoke(cli, ["edge-match", "--help"])
    assert result.exit_code == 0
    assert "Fit an input layer into the polygons" in result.output
    assert "Examples:" in result.output
    assert "--match-column" in result.output
    assert "--overlay-match-column" in result.output
    assert "--input-match-column" in result.output


def test_match_mutually_exclusive_with_pair(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="mutually exclusive"):
        match(
            synthetic_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            match_column="pcode",
            overlay_match_column="pcode",
        )


def test_match_agrees_with_spatial_is_a_noop(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """Code agreeing with spatial everywhere must reproduce the default run exactly."""
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        overlay_by_input = dict(
            conn.execute(f"""--sql
                SELECT c.id, p.id FROM '{synthetic_inputs}' c, '{synthetic_overlays}' p
                WHERE ST_Intersects(c.geom, p.geom)
            """).fetchall()
        )
    inputs_with_code = tmp_path / "children_coded.parquet"
    overlays_with_code = tmp_path / "parents_coded.parquet"
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        conn.execute(f"""--sql
            COPY (SELECT *, CAST(id AS VARCHAR) AS pcode FROM '{synthetic_overlays}')
            TO '{overlays_with_code}'
        """)
        rows = ", ".join(
            f"({fid}, '{overlay_by_input.get(fid, 0)}')" for fid in overlay_by_input
        )
        conn.execute(f"""--sql
            COPY (
                SELECT c.*, m.pcode FROM '{synthetic_inputs}' c
                JOIN (SELECT * FROM (VALUES {rows}) AS v(id, pcode)) m ON m.id = c.id
            ) TO '{inputs_with_code}'
        """)

    match(
        inputs_with_code,
        overlays_with_code,
        output_path,
        issues_path,
        match_column="pcode",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
        kinds = (
            [
                row[0]
                for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
            ]
            if issues_path.exists()
            else []
        )
    assert ids == [1, 2, 3]
    assert "code-mismatch" not in kinds


def test_match_full_run(synthetic_inputs, synthetic_overlays, tmp_path, caplog):
    """Auto keeps assign-one (2 of 4 overlap Overlay A); input 3 clip-empties."""
    output_path = tmp_path / "out.parquet"
    with caplog.at_level(logging.INFO):
        match(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)
    assert "assign one (auto): 2 of 4 input polygons" in caplog.text

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2, 4]


def test_match_auto_switches_to_many_under_half(synthetic_overlays, tmp_path, caplog):
    """Only 1 of 3 inputs overlaps the winner, so auto assigns each on its own."""
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0], _INPUT_WKT[2], _INPUT_WKT[3]])
    output_path = tmp_path / "out.parquet"
    with caplog.at_level(logging.INFO):
        match(input_path, synthetic_overlays, output_path, overwrite=True)
    assert "assign many (auto): only 1 of 3 input polygons" in caplog.text

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 3]


def test_match_multi_overlay_preserves_per_input_grouping(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """--assign many gives per-polygon groups: 1&2, 3, and 4 dropped."""
    output_path = tmp_path / "out.parquet"
    match(
        synthetic_inputs,
        synthetic_overlays,
        output_path,
        assign="many",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2, 3]


def test_match_drops_unassigned_and_warns(
    synthetic_inputs, synthetic_overlays, tmp_path, caplog
):
    """Only --assign many can leave an input with no winner."""
    output_path = tmp_path / "out.parquet"
    with caplog.at_level(logging.WARNING):
        match(
            synthetic_inputs,
            synthetic_overlays,
            output_path,
            assign="many",
            overwrite=True,
        )

    assert any("dropping" in r.message and "4" in r.message for r in caplog.records)


def test_match_issues_file_default_path(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    match(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)

    expected_issues_path = output_path.with_stem(output_path.stem + "_issues")
    assert expected_issues_path.exists()


def test_match_issues_file_records_unassigned_input(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """--assign many leaves input 4 with no winner at all."""
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        synthetic_inputs,
        synthetic_overlays,
        output_path,
        issues_path,
        assign="many",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(f"SELECT * FROM '{issues_path}'").fetchall()
        cols = [
            d[0] for d in conn.execute(f"SELECT * FROM '{issues_path}'").description
        ]

    unassigned_input_fid = 4
    assert len(rows) == 1
    row = dict(zip(cols, rows[0], strict=True))
    assert row["kind"] == "unassigned"
    assert row["unit_a"] == unassigned_input_fid
    assert row["overlay_fid"] is None
    assert row["reason"] is None
    assert row["geometry"] is not None


def test_match_default_issues_file_records_clip_empty_input(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """Default assign-one forces input 3 onto Overlay A; its clip comes back empty."""
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        synthetic_inputs,
        synthetic_overlays,
        output_path,
        issues_path,
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(f"SELECT * FROM '{issues_path}'").fetchall()
        cols = [
            d[0] for d in conn.execute(f"SELECT * FROM '{issues_path}'").description
        ]

    clip_empty_input_fid = 3
    winning_overlay_fid = 1
    assert len(rows) == 1
    row = dict(zip(cols, rows[0], strict=True))
    assert row["kind"] == "clip-empty"
    assert row["unit_a"] == clip_empty_input_fid
    assert row["overlay_fid"] == winning_overlay_fid
    assert row["reason"] is not None


def test_match_issues_file_absent_when_nothing_dropped(tmp_path):
    """Overlay B's single-input group succeeds cleanly, so no issues file is written."""
    input_path = tmp_path / "children_single.parquet"
    overlays_path = tmp_path / "parents_single.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[2]])  # fid 3 only
    _write_synthetic(overlays_path, [_OVERLAY_WKT[1]])  # Overlay B only

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(input_path, overlays_path, output_path, issues_path, overwrite=True)

    assert not issues_path.exists()


# An overlay feature with a real interior hole (e.g. Lesotho inside South Africa);
# two input features exactly tile the outer square, so no gap is self-inflicted.
_ENCLAVE_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0), (4 4, 6 4, 6 6, 4 6, 4 4))"),
]
_ENCLAVE_INPUT_WKT = [
    (1, "POLYGON((0 0, 5 0, 5 10, 0 10, 0 0))"),
    (2, "POLYGON((5 0, 10 0, 10 10, 5 10, 5 0))"),
]


def test_match_tolerates_overlay_layer_enclave(tmp_path):
    """A real hole in the overlay's own shape must not raise or be reported."""
    input_path = tmp_path / "children_enclave.parquet"
    overlays_path = tmp_path / "parents_enclave.parquet"
    _write_synthetic(input_path, _ENCLAVE_INPUT_WKT)
    _write_synthetic(overlays_path, _ENCLAVE_OVERLAY_WKT)

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(input_path, overlays_path, output_path, issues_path, overwrite=True)

    assert output_path.exists()
    if issues_path.exists():
        with duckdb.connect() as conn:
            conn.execute("LOAD spatial")
            gap_rows = conn.execute(
                f"SELECT count(*) FROM '{issues_path}' WHERE kind = 'gap'"
            ).fetchone()[0]
        assert gap_rows == 0


def test_record_dropped_group():
    """Exercises the group-failure recording helper directly, no subprocess involved."""
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute("""--sql
            CREATE TABLE t_input_01 AS
            SELECT * FROM (VALUES
                (1, 'children.parquet',
                    ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')),
                (2, 'children.parquet',
                    ST_GeomFromText('POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))'))
            ) AS v(fid, source_file, geom)
        """)
        conn.execute("""--sql
            CREATE TABLE t_02_assign AS
            SELECT * FROM (VALUES (1, 10), (2, 10)) AS v(input_fid, overlay_fid)
        """)
        conn.execute("""--sql
            CREATE TABLE t_03b AS
            SELECT NULL::BIGINT AS input_fid, NULL::BIGINT AS overlay_fid,
                   NULL::VARCHAR AS reason, NULL::VARCHAR AS source_file,
                   NULL::GEOMETRY AS geom
            WHERE FALSE
        """)

        _record_dropped_group(
            conn,
            "t",
            10,
            "boom: something failed",
            'SELECT input_fid FROM "t_02_assign" WHERE overlay_fid = 10',
        )

        rows = conn.execute(
            "SELECT input_fid, overlay_fid, reason FROM t_03b ORDER BY input_fid"
        ).fetchall()

    assert rows == [
        (1, 10, "boom: something failed"),
        (2, 10, "boom: something failed"),
    ]


def test_match_clip_step_aborts_on_bad_overlay_fid(tmp_path):
    """A single bad overlay_fid in the clip step aborts the whole run.

    clip's hard-fail-on-first-bad-overlay_fid semantics apply uniformly to
    match too, not match's old per-group continue-past-failure behavior.
    """
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute("""--sql
            CREATE TABLE t_03a AS
            SELECT * FROM (VALUES
                (1, 99, ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))'))
            ) AS v(fid, overlay_fid, geom)
        """)
        conn.execute("""--sql
            CREATE TABLE t_overlay_01 AS
            SELECT * FROM (VALUES
                (1, ST_GeomFromText('POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))'))
            ) AS v(fid, geom)
        """)

        with pytest.raises(RuntimeError, match="overlay_fid=99"):
            match_clip.main(conn, "t", tmp_path)


def test_match_single_overlay_group(tmp_path):
    """Overlay B has exactly one assigned input feature.

    Exercises the always-group, even-size-1 path explicitly, isolated from
    Overlay A's multi-input feature group.
    """
    input_path = tmp_path / "children_single.parquet"
    overlays_path = tmp_path / "parents_single.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[2]])  # fid 3 only
    _write_synthetic(overlays_path, [_OVERLAY_WKT[1]])  # Overlay B only

    output_path = tmp_path / "out.parquet"
    match(input_path, overlays_path, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row_count = conn.execute(f"SELECT COUNT(*) FROM '{output_path}'").fetchone()[0]
    assert row_count == 1


def test_match_default_output_path(synthetic_inputs, synthetic_overlays):
    match(synthetic_inputs, synthetic_overlays, overwrite=True)

    expected = synthetic_inputs.with_stem(synthetic_inputs.stem + "_matched")
    assert expected.exists()


def test_cli_positional_args(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-match",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output_path.exists()


def test_cli_issues_file_option(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    issues_path = tmp_path / "cli_issues.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-match",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
            "--issues-file",
            str(issues_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert issues_path.exists()


def test_cli_clip_file_required(synthetic_inputs):
    result = CliRunner().invoke(cli, ["edge-match", str(synthetic_inputs)])
    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_cli_clean_error_on_existing_output(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        [
            "edge-match",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_match_steps(synthetic_inputs, synthetic_overlays, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        match(
            synthetic_inputs,
            synthetic_overlays,
            output_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()


def test_match_all_unassigned(tmp_path):
    """Every input feature fails to match any overlay feature.

    match() should raise, not silently write an empty output file.
    """
    input_path = tmp_path / "children_far.parquet"
    overlays_path = tmp_path / "parents_near.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[3]])  # fid 4, far from any overlay feature
    _write_synthetic(overlays_path, _OVERLAY_WKT)

    output_path = tmp_path / "out.parquet"
    with pytest.raises(RuntimeError, match="no group produced any output"):
        match(input_path, overlays_path, output_path, overwrite=True)


def _write_with_code(path, rows):
    """rows: list of (fid, wkt, pcode)."""
    values = ", ".join(
        f"({fid}, ST_GeomFromText('{wkt}'), '{code}')" for fid, wkt, code in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE synth AS
            SELECT * FROM (VALUES {values}) AS t(id, geom, pcode)
        """)
        conn.execute(f"COPY synth TO '{path}'")


def _write_overlay_pcode_only(path, rows):
    """rows: list of (pid, wkt, pcode); no 'id' column, avoids a merge collision."""
    values = ", ".join(
        f"({pid}, ST_GeomFromText('{wkt}'), '{code}')" for pid, wkt, code in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE synth AS
            SELECT * FROM (VALUES {values}) AS t(pid, geom, pcode)
        """)
        conn.execute(f"COPY synth TO '{path}'")


def test_match_carry_columns_survives_group_subprocess(tmp_path):
    """A carried overlay column must survive the per-group extend/merge round-trip."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, _INPUT_WKT[:3])  # fids 1, 2, 3, all matched

    output_path = tmp_path / "out.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        overlay_include=["pcode"],
        assign="many",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT id, pcode FROM '{output_path}' ORDER BY id"
        ).fetchall()
    assert rows == [(1, "P1"), (2, "P1"), (3, "P2")]


def test_cli_merge_help():
    result = CliRunner().invoke(cli, ["edge-match", "--help"])
    assert result.exit_code == 0
    assert "--merge" in result.output
    assert "--carry-column" not in result.output


def test_match_merge_bare_passthrough_keeps_orphan_and_carries_columns(tmp_path):
    """Bare --merge carries every overlay column and keeps an orphan unclipped."""
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, _INPUT_WKT)  # fids 1-4; 4 is far, unmatched

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        merge=True,
        assign="many",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT id, pcode FROM '{output_path}' ORDER BY id"
        ).fetchall()
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]

    assert rows == [(1, "P1"), (2, "P1"), (3, "P2"), (4, None)]
    assert "passthrough" in kinds
    assert "unassigned" not in kinds


def test_match_no_merge_still_drops_orphan(tmp_path):
    """Without --merge, an orphan is dropped and reported unassigned, as before."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, _INPUT_WKT)

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        assign="many",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]

    assert ids == [1, 2, 3]
    assert kinds == ["unassigned"]


def test_match_gap_fill_keeps_unmatched_overlay(tmp_path):
    """Overlay B gets zero matched input features, so it carries through unclipped."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0], _INPUT_WKT[1]])

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT overlay_fid, pcode FROM '{output_path}' WHERE overlay_fid = 2"
        ).fetchall()
        issue_rows = conn.execute(
            f"SELECT kind, overlay_fid FROM '{issues_path}' WHERE kind = 'gap-fill'"
        ).fetchall()
    assert rows == [(2, "P2")]
    assert issue_rows == [("gap-fill", 2)]


def test_match_gap_fill_and_passthrough_together(tmp_path):
    """An unmatched overlay and an unmatched input file can both appear in one run."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    file_a = tmp_path / "file_a.parquet"
    file_far = tmp_path / "file_far.parquet"
    _write_synthetic(file_a, [_INPUT_WKT[0]])  # matches Overlay A only
    _write_synthetic(file_far, [_INPUT_WKT[3]])  # zero overlap with any overlay feature

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        [file_a, file_far],
        overlays_path,
        output_path,
        issues_path,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        kinds = {
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        }
    assert "gap-fill" in kinds
    assert "passthrough" in kinds


def test_match_input_exclude_drops_named_input_column(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")]
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    output_path = tmp_path / "out.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        input_exclude=["id"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
        }
    assert "id" not in columns
    assert "pcode" in columns


def test_match_narrowing_flag_without_merge_raises(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")]
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    with pytest.raises(ValueError, match="require merge"):
        match(
            input_path,
            overlays_path,
            tmp_path / "out.parquet",
            overlay_include=["pcode"],
            overwrite=True,
        )


def test_match_prefer_mutually_exclusive_with_narrowing_flags(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")]
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    with pytest.raises(ValueError, match="mutually exclusive"):
        match(
            input_path,
            overlays_path,
            tmp_path / "out.parquet",
            merge=True,
            prefer="overlay",
            overlay_include=["pcode"],
            overwrite=True,
        )


def test_match_prefer_overlay_resolves_real_collision(tmp_path):
    """id/geom/pcode all overlap between the two layers; pcode is the real collision."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "CHILDVAL")],
    )

    output_path = tmp_path / "out.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        prefer="overlay",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        pcode = conn.execute(f"SELECT pcode FROM '{output_path}'").fetchone()[0]
    assert pcode == "P1"


def test_match_prefer_input_resolves_real_collision(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "CHILDVAL")],
    )

    output_path = tmp_path / "out.parquet"
    match(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        prefer="input",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        pcode = conn.execute(f"SELECT pcode FROM '{output_path}'").fetchone()[0]
    assert pcode == "CHILDVAL"


@pytest.fixture
def synthetic_inputs_split(tmp_path):
    """Write inputs 1 & 2 (the Overlay A pair) to separate files."""
    path_a = tmp_path / "child_a.parquet"
    path_b = tmp_path / "child_b.parquet"
    _write_synthetic(path_a, [_INPUT_WKT[0]])
    _write_synthetic(path_b, [_INPUT_WKT[1]])
    return [path_a, path_b]


@pytest.fixture
def synthetic_inputs_split_with_orphan(tmp_path):
    """Write inputs 1 & 2 (Overlay A pair) and an unmatched input, one per file."""
    path_a = tmp_path / "child_a.parquet"
    path_b = tmp_path / "child_b.parquet"
    path_c = tmp_path / "child_c.parquet"
    _write_synthetic(path_a, [_INPUT_WKT[0]])
    _write_synthetic(path_b, [_INPUT_WKT[1]])
    _write_synthetic(path_c, [_INPUT_WKT[3]])
    return [path_a, path_b, path_c]


def test_match_multi_file_api(synthetic_inputs_split, synthetic_overlays, tmp_path):
    """Inputs from different files landing on the same overlay extend together."""
    output_path = tmp_path / "out.parquet"
    match(synthetic_inputs_split, synthetic_overlays, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
        }
        rows = conn.execute(f"SELECT id FROM '{output_path}' ORDER BY id").fetchall()
    assert "source_file" not in columns
    assert [r[0] for r in rows] == [1, 2]


def test_match_multi_file_column_order_is_deterministic(synthetic_overlays, tmp_path):
    """UNION ALL BY NAME must not let caller-supplied file order pick the schema."""
    deep_path = tmp_path / "deep.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE deep AS SELECT * FROM (VALUES
                (1, 'A1', 'B1', ST_GeomFromText('{_INPUT_WKT[0][1]}'))
            ) AS t(id, adm1_name, adm2_name, geom)
        """)
        conn.execute(f"COPY deep TO '{deep_path}'")

    shallow_path = tmp_path / "shallow.parquet"
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(f"""--sql
            CREATE TABLE shallow AS SELECT * FROM (VALUES
                (2, 'B2', ST_GeomFromText('{_INPUT_WKT[1][1]}'))
            ) AS t(id, adm2_name, geom)
        """)
        conn.execute(f"COPY shallow TO '{shallow_path}'")

    def _columns(paths, tag):
        output_path = tmp_path / f"out_{tag}.parquet"
        match(list(paths), synthetic_overlays, output_path, overwrite=True)
        with duckdb.connect() as conn:
            conn.execute("LOAD spatial")
            return [
                row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
            ]

    forward = _columns([deep_path, shallow_path], "forward")
    reverse = _columns([shallow_path, deep_path], "reverse")
    assert forward == reverse
    assert "adm1_name" in forward


def test_match_multi_file_rejects_multi_overlay(
    synthetic_inputs_split, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="not supported when multiple"):
        match(
            synthetic_inputs_split,
            synthetic_overlays,
            tmp_path / "out.parquet",
            assign="many",
            overwrite=True,
        )


def test_match_multi_file_rejects_step(
    synthetic_inputs_split, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="step is not supported"):
        match(
            synthetic_inputs_split,
            synthetic_overlays,
            tmp_path / "out.parquet",
            step="assign",
            overwrite=True,
        )


def test_match_multi_file_requires_output_path(
    synthetic_inputs_split, synthetic_overlays
):
    with pytest.raises(ValueError, match="output_path is required"):
        match(synthetic_inputs_split, synthetic_overlays)


def test_match_multi_file_source_file_populated_in_issues(
    synthetic_inputs_split_with_orphan, synthetic_overlays, tmp_path
):
    """Issues rows carry an parent-dir/filename source_file, not the full path."""
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    match(
        synthetic_inputs_split_with_orphan,
        synthetic_overlays,
        output_path,
        issues_path,
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT source_file FROM '{issues_path}' WHERE kind = 'unassigned'"
        ).fetchall()
    orphan_path = synthetic_inputs_split_with_orphan[2]
    assert len(rows) == 1
    assert rows[0][0] == "/".join(orphan_path.parts[-2:])


def test_cli_edge_match_glob_expansion(
    synthetic_inputs_split,  # noqa: ARG001 (write side effect is the point)
    synthetic_overlays,
    tmp_path,
):
    output_path = tmp_path / "out.parquet"
    pattern = str(tmp_path / "child_*.parquet")
    result = CliRunner().invoke(
        cli,
        ["edge-match", pattern, str(synthetic_overlays), str(output_path)],
    )
    assert result.exit_code == 0, result.output

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2]


def test_cli_edge_match_extra_input_flag_combines_with_glob(
    synthetic_inputs_split, synthetic_overlays, tmp_path
):
    """A glob-matched file plus a --input-flagged file both feed one combined run."""
    file_a, file_b = synthetic_inputs_split
    output_path = tmp_path / "out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-match",
            str(file_a),
            str(synthetic_overlays),
            str(output_path),
            "--input",
            str(file_b),
        ],
    )
    assert result.exit_code == 0, result.output

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2]


def test_cli_edge_match_glob_no_matches(synthetic_overlays, tmp_path):
    pattern = str(tmp_path / "nomatch_*.parquet")
    result = CliRunner().invoke(
        cli,
        ["edge-match", pattern, str(synthetic_overlays), str(tmp_path / "out.parquet")],
    )
    assert result.exit_code != 0
    assert "no files matched" in result.output


def _write_admin_synthetic(path, rows: list[dict]) -> None:
    cols = [k for k in rows[0] if k != "wkt"]
    col_list = ", ".join([*cols, "geom"])
    values = ", ".join(
        "("
        + ", ".join(
            "NULL"
            if r[c] is None
            else f"'{r[c]}'"
            if isinstance(r[c], str)
            else str(r[c])
            for c in cols
        )
        + f", ST_GeomFromText('{r['wkt']}'))"
        for r in rows
    )
    with duckdb.connect() as conn:
        conn.execute("INSTALL spatial; LOAD spatial;")
        conn.execute(
            f"CREATE TABLE synth AS SELECT * FROM (VALUES {values}) AS t({col_list})"
        )
        conn.execute(f"COPY synth TO '{path}'")


_ADMIN_INPUT_ROWS = [
    {
        "adm1_code": "AA",
        "adm1_name": "Country A",
        "adm2_code": "AA01",
        "adm2_name": "Prov1",
        "wkt": _INPUT_WKT[0][1],
    },
    {
        "adm1_code": "AA",
        "adm1_name": "Country A",
        "adm2_code": None,
        "adm2_name": None,
        "wkt": _INPUT_WKT[1][1],
    },
]


@pytest.fixture
def admin_inputs(tmp_path):
    path = tmp_path / "admin_children.parquet"
    _write_admin_synthetic(path, _ADMIN_INPUT_ROWS)
    return path


def _columns_and_rows(path):
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        cols = [row[0] for row in conn.execute(f"DESCRIBE '{path}'").fetchall()]
        rows = conn.execute(f"SELECT * FROM '{path}' ORDER BY adm2_code").fetchall()
    return cols, rows


def test_fill_schema_off_by_default_leaves_output_unchanged(
    admin_inputs, synthetic_overlays, tmp_path
):
    output_path = tmp_path / "out.parquet"
    match(admin_inputs, synthetic_overlays, output_path, overwrite=True)
    cols, _ = _columns_and_rows(output_path)
    assert "adm_lvl" not in cols


def test_fill_schema_stamps_depth_and_fills(admin_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    match(
        admin_inputs, synthetic_overlays, output_path, overwrite=True, fill_schema=True
    )
    cols, rows = _columns_and_rows(output_path)
    assert "adm_lvl" in cols
    lvl = cols.index("adm_lvl")
    name2 = cols.index("adm2_name")

    by_level = {r[lvl]: r for r in rows}
    assert by_level[_LEVEL_2][name2] == "Prov1"
    assert by_level[_LEVEL_1][name2] == "Country A"


def test_cli_fill_schema_flag(admin_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-match",
            str(admin_inputs),
            str(synthetic_overlays),
            str(output_path),
            "--fill-schema",
        ],
    )
    assert result.exit_code == 0, result.output
    cols, _ = _columns_and_rows(output_path)
    assert "adm_lvl" in cols


def test_fill_schema_without_admin_columns_raises(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="no admin hierarchy level detected"):
        match(
            synthetic_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            overwrite=True,
            fill_schema=True,
        )


def test_fill_depth_column_flag(admin_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    match(
        admin_inputs,
        synthetic_overlays,
        output_path,
        overwrite=True,
        fill_schema=True,
        depth_column="adm_depth",
    )
    cols, _ = _columns_and_rows(output_path)
    assert "adm_depth" in cols
    assert "adm_lvl" not in cols


def test_depth_column_collision_raises(synthetic_overlays, tmp_path):
    rows = [{**row, "adm_lvl": 99} for row in _ADMIN_INPUT_ROWS]
    path = tmp_path / "collide.parquet"
    _write_admin_synthetic(path, rows)
    with pytest.raises(ValueError, match=r"adm_lvl.*already exists"):
        match(
            path,
            synthetic_overlays,
            tmp_path / "out.parquet",
            overwrite=True,
            fill_schema=True,
        )


def test_name_field_code_field_require_fill_schema(
    admin_inputs, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="require fill_schema"):
        match(
            admin_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            overwrite=True,
            name_field="adm{n}_name",
            code_field="adm{n}_code",
        )


def test_fill_schema_multi_file(synthetic_overlays, tmp_path):
    """Second insertion point: _match_multi_file()'s own outputs branch."""
    path_a = tmp_path / "child_a.parquet"
    path_b = tmp_path / "child_b.parquet"
    _write_admin_synthetic(path_a, [_ADMIN_INPUT_ROWS[0]])
    _write_admin_synthetic(path_b, [_ADMIN_INPUT_ROWS[1]])
    output_path = tmp_path / "out.parquet"
    match(
        [path_a, path_b],
        synthetic_overlays,
        output_path,
        overwrite=True,
        fill_schema=True,
    )
    cols, rows = _columns_and_rows(output_path)
    assert "adm_lvl" in cols
    lvl = cols.index("adm_lvl")
    assert {r[lvl] for r in rows} == {_LEVEL_1, _LEVEL_2}
