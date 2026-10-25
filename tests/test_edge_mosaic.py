"""Portability smoke tests: does mosaic() run to completion on this machine.

Not a correctness suite: outputs.main already raises RuntimeError on
coverage violations, so a clean run is already vetted by the pipeline itself.
"""

import logging

import duckdb
import pytest
from click.testing import CliRunner

import topo_tools.core.edge_extend.attempt as attempt_module
from topo_tools.api.edge_extend import extend
from topo_tools.api.edge_match import match
from topo_tools.api.edge_mosaic import mosaic
from topo_tools.cli.main import cli

_LEVEL_1, _LEVEL_2 = 1, 2

# One-file-one-overlay: inputs 1+2 tile Overlay A, winning the majority
# vote, so input 3 (Overlay B territory) and input 4 (unassignable) both drop.
_INPUT_WKT = [
    (1, "POLYGON((-5 -5, 1.5 -5, 1.5 5, -5 5, -5 -5))"),
    (2, "POLYGON((1.5 -5, 8 -5, 8 5, 1.5 5, 1.5 -5))"),
    (3, "POLYGON((8 -5, 20 -5, 20 20, 8 20, 8 -5))"),
    (4, "POLYGON((100 100, 101 100, 101 101, 100 101, 100 100))"),
]
_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))"),
    (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))"),
]

_OVERLAY_A_AREA = 9.0

_STEPS = ["inputs", "assign", "clip", "stitch", "outputs"]


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
    """Write a small synthetic already-extended input feature-layer GeoParquet."""
    path = tmp_path / "children.parquet"
    _write_synthetic(path, _INPUT_WKT)
    return path


@pytest.fixture
def synthetic_overlays(tmp_path):
    """Write a small synthetic overlay-layer GeoParquet."""
    path = tmp_path / "parents.parquet"
    _write_synthetic(path, _OVERLAY_WKT)
    return path


@pytest.fixture
def synthetic_inputs_split(tmp_path):
    """Write inputs 1 & 2 (the Overlay A tiling pair) to separate files."""
    path_a = tmp_path / "child_a.parquet"
    path_b = tmp_path / "child_b.parquet"
    _write_synthetic(path_a, [_INPUT_WKT[0]])
    _write_synthetic(path_b, [_INPUT_WKT[1]])
    return [path_a, path_b]


# Input 2 straddles into Overlay B with a bigger individual overlap there
# (area 6 vs 4.5, its own plurality pick), but file_a's vote count still picks A.
_MAJORITY_INPUT_A = (1, "POLYGON((-5 -5, 1.5 -5, 1.5 5, -5 5, -5 -5))")
_MAJORITY_INPUT_STRADDLE = (2, "POLYGON((1.5 -5, 12 -5, 12 5, 1.5 5, 1.5 -5))")
_MAJORITY_INPUT_B_ONLY = (3, "POLYGON((8 -5, 20 -5, 20 20, 8 20, 8 -5))")


@pytest.fixture
def synthetic_inputs_file_majority(tmp_path):
    """file_a tiles Overlay A, one input straddling B; file_b feeds Overlay B alone."""
    path_a = tmp_path / "file_a.parquet"
    path_b = tmp_path / "file_b.parquet"
    _write_synthetic(path_a, [_MAJORITY_INPUT_A, _MAJORITY_INPUT_STRADDLE])
    _write_synthetic(path_b, [_MAJORITY_INPUT_B_ONLY])
    return [path_a, path_b]


def test_cli_help():
    result = CliRunner().invoke(cli, ["edge-mosaic", "--help"])
    assert result.exit_code == 0
    assert "Fit an already-extended input layer" in result.output
    assert "Examples:" in result.output


def test_mosaic_full_run(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    mosaic(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        ids = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM '{output_path}' ORDER BY id"
            ).fetchall()
        ]
    assert ids == [1, 2]


def test_mosaic_drops_unassigned_and_warns(synthetic_overlays, tmp_path, caplog):
    """A whole file with no overlay overlap at all is dropped and warned about."""
    file_far = tmp_path / "file_far.parquet"
    file_a = tmp_path / "file_a.parquet"
    _write_synthetic(
        file_far, [_INPUT_WKT[3]]
    )  # sole input feature, far from any overlay feature
    _write_synthetic(file_a, [_INPUT_WKT[0], _INPUT_WKT[1]])  # tiles Overlay A

    output_path = tmp_path / "out.parquet"
    with caplog.at_level(logging.WARNING):
        mosaic([file_far, file_a], synthetic_overlays, output_path, overwrite=True)

    assert any("dropping 1 input fid(s)" in r.message for r in caplog.records)


def test_mosaic_issues_file_default_path(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    output_path = tmp_path / "out.parquet"
    mosaic(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)

    expected_issues_path = output_path.with_stem(output_path.stem + "_issues")
    assert expected_issues_path.exists()


def test_mosaic_issues_file_records_clip_empty_input(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """Inputs 3/4 are forced onto Overlay A, then dropped as clip-empty."""
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(
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

    expected_clip_empty_fids = {3, 4}
    assert len(rows) == len(expected_clip_empty_fids)
    by_fid = {dict(zip(cols, row, strict=True))["unit_a"]: row for row in rows}
    assert set(by_fid) == expected_clip_empty_fids
    for row in rows:
        parsed = dict(zip(cols, row, strict=True))
        assert parsed["kind"] == "clip-empty"
        assert parsed["overlay_fid"] == 1
        assert parsed["reason"] is not None
        assert parsed["geometry"] is not None


def test_mosaic_issues_file_absent_when_nothing_dropped(tmp_path):
    """Overlay B's single-input case succeeds cleanly, so no issues file is written."""
    input_path = tmp_path / "children_single.parquet"
    overlays_path = tmp_path / "parents_single.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[2]])  # fid 3 only
    _write_synthetic(overlays_path, [_OVERLAY_WKT[1]])  # Overlay B only

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(input_path, overlays_path, output_path, issues_path, overwrite=True)

    assert not issues_path.exists()


# An overlay feature with a real interior hole (e.g. Lesotho inside South Africa);
# two already-extended input features exactly tile the outer square, no self-gap.
_ENCLAVE_OVERLAY_WKT = [
    (1, "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0), (4 4, 6 4, 6 6, 4 6, 4 4))"),
]
_ENCLAVE_INPUT_WKT = [
    (1, "POLYGON((0 0, 5 0, 5 10, 0 10, 0 0))"),
    (2, "POLYGON((5 0, 10 0, 10 10, 5 10, 5 0))"),
]


def test_mosaic_tolerates_overlay_layer_enclave(tmp_path):
    """A real hole in the overlay's own shape must not raise or be reported."""
    input_path = tmp_path / "children_enclave.parquet"
    overlays_path = tmp_path / "parents_enclave.parquet"
    _write_synthetic(input_path, _ENCLAVE_INPUT_WKT)
    _write_synthetic(overlays_path, _ENCLAVE_OVERLAY_WKT)

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(input_path, overlays_path, output_path, issues_path, overwrite=True)

    assert output_path.exists()
    if issues_path.exists():
        with duckdb.connect() as conn:
            conn.execute("LOAD spatial")
            gap_rows = conn.execute(
                f"SELECT count(*) FROM '{issues_path}' WHERE kind = 'gap'"
            ).fetchone()[0]
        assert gap_rows == 0


def test_mosaic_clip_bounds_output_to_overlay(
    synthetic_inputs, synthetic_overlays, tmp_path
):
    """Oversized already-extended inputs clip down to the overlay's true extent."""
    output_path = tmp_path / "out.parquet"
    mosaic(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(f"""--sql
            SELECT ST_Area(ST_Union_Agg(geometry))
            FROM '{output_path}' WHERE id IN (1, 2)
        """).fetchone()[0]
    assert area == pytest.approx(_OVERLAY_A_AREA, abs=1e-6)


def test_mosaic_all_unassigned(tmp_path):
    """All input features unassigned: mosaic() must raise, not write an empty output."""
    input_path = tmp_path / "children_far.parquet"
    overlays_path = tmp_path / "parents_near.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[3]])  # fid 4, far from any overlay feature
    _write_synthetic(overlays_path, _OVERLAY_WKT)

    output_path = tmp_path / "out.parquet"
    with pytest.raises(RuntimeError, match="no input polygon got an overlay"):
        mosaic(input_path, overlays_path, output_path, overwrite=True)


def test_mosaic_default_output_path(synthetic_inputs, synthetic_overlays):
    mosaic(synthetic_inputs, synthetic_overlays, overwrite=True)

    expected = synthetic_inputs.with_stem(synthetic_inputs.stem + "_mosaicked")
    assert expected.exists()


def test_cli_positional_args(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "cli_out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-mosaic",
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
            "edge-mosaic",
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
    result = CliRunner().invoke(cli, ["edge-mosaic", str(synthetic_inputs)])
    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_cli_error_on_existing_output(synthetic_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "exists.parquet"
    output_path.touch()
    result = CliRunner().invoke(
        cli,
        [
            "edge-mosaic",
            str(synthetic_inputs),
            str(synthetic_overlays),
            str(output_path),
            "--overwrite=false",
        ],
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "output already exists" in result.output


def test_mosaic_steps(synthetic_inputs, synthetic_overlays, tmp_path):
    """Each pipeline stage runs standalone, reusing one tmp_dir's DuckDB file."""
    output_path = tmp_path / "steps_out.parquet"
    work_dir = tmp_path / "work"
    for step in _STEPS:
        mosaic(
            synthetic_inputs,
            synthetic_overlays,
            output_path,
            tmp_dir=work_dir,
            step=step,
            overwrite=True,
        )
    assert output_path.exists()


def test_mosaic_never_invokes_extend_pipeline(
    synthetic_inputs, synthetic_overlays, tmp_path, monkeypatch
):
    """Regression guard: mosaic must never re-run extend's Voronoi pipeline."""

    def _boom(*_args, **_kwargs):
        msg = "mosaic must not invoke extend's Voronoi pipeline"
        raise AssertionError(msg)

    monkeypatch.setattr(attempt_module, "main", _boom)

    output_path = tmp_path / "out.parquet"
    mosaic(synthetic_inputs, synthetic_overlays, output_path, overwrite=True)
    assert output_path.exists()


def test_mosaic_multi_file_api(synthetic_inputs_split, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    mosaic(synthetic_inputs_split, synthetic_overlays, output_path, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
        }
        rows = conn.execute(f"SELECT id FROM '{output_path}' ORDER BY id").fetchall()
    assert "source_file" not in columns
    assert [r[0] for r in rows] == [1, 2]


def test_mosaic_multi_file_merge_untagged_inputs_tagged_overlay(
    synthetic_overlays, tmp_path
):
    paths = []
    for fid, wkt in _INPUT_WKT[:2]:
        path = tmp_path / f"child_{fid}.parquet"
        with duckdb.connect() as conn:
            conn.execute("LOAD spatial")
            conn.execute(f"""
                COPY (SELECT {fid} AS input_id, ST_GeomFromText('{wkt}') AS geom)
                TO '{path}' (FORMAT PARQUET, GEOPARQUET_VERSION 'NONE')
            """)
        paths.append(path)
    output_path = tmp_path / "out.parquet"
    mosaic(paths, synthetic_overlays, output_path, merge=True, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        count = conn.execute(f"SELECT count(*) FROM '{output_path}'").fetchone()[0]
    assert count == len(paths) + 1


def test_mosaic_multi_file_detached_keys_are_unique(tmp_path):
    notch = (
        "POLYGON(({x0} 0, {x2} 0, {x2} 0.008, {x1} 0.008, {x1} 0.009, "
        "{x3} 0.009, {x3} 0.0091, {x1} 0.0091, {x1} 0.01, {x0} 0.01, {x0} 0))"
    )
    overlays, paths = [], []
    for i, dx in enumerate((0, 1)):
        x0, x1, x2, x3 = dx - 0.005, dx, dx + 0.01, dx + 0.00005
        overlays.append((i + 1, notch.format(x0=x0, x1=x1, x2=x2, x3=x3)))
        path = tmp_path / f"children_{i}.parquet"
        _write_synthetic(
            path,
            [
                (2 * i + 1, f"POLYGON(({x1} 0, {x2} 0, {x2} 0.01, {x1} 0.01, {x1} 0))"),
                (2 * i + 2, f"POLYGON(({x0} 0, {x1} 0, {x1} 0.01, {x0} 0.01, {x0} 0))"),
            ],
        )
        paths.append(path)
    overlays_path = tmp_path / "parents.parquet"
    _write_synthetic(overlays_path, overlays)
    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(
        paths,
        overlays_path,
        output_path,
        issues_path,
        overwrite=True,
        original_paths=paths,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT key, fixed FROM '{issues_path}' WHERE kind = 'detached-part'"
        ).fetchall()
    keys = [k for k, _ in rows]
    assert len(set(keys)) == len(keys) == len(paths)
    assert all(fixed for _, fixed in rows)


def test_mosaic_multi_file_column_order_is_deterministic(synthetic_overlays, tmp_path):
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
        mosaic(list(paths), synthetic_overlays, output_path, overwrite=True)
        with duckdb.connect() as conn:
            conn.execute("LOAD spatial")
            return [
                row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
            ]

    forward = _columns([deep_path, shallow_path], "forward")
    reverse = _columns([shallow_path, deep_path], "reverse")
    assert forward == reverse
    assert "adm1_name" in forward


def test_mosaic_multi_file_requires_output_path(
    synthetic_inputs_split, synthetic_overlays
):
    with pytest.raises(ValueError, match="output_path is required"):
        mosaic(synthetic_inputs_split, synthetic_overlays)


def test_mosaic_single_path_still_optional_output(synthetic_inputs, synthetic_overlays):
    """Contrast case: a single (non-list) path still defaults output_path."""
    mosaic(synthetic_inputs, synthetic_overlays, overwrite=True)
    assert synthetic_inputs.with_stem(synthetic_inputs.stem + "_mosaicked").exists()


def test_cli_glob_expansion(
    synthetic_inputs_split,  # noqa: ARG001 (write side effect is the point)
    synthetic_overlays,
    tmp_path,
):
    output_path = tmp_path / "out.parquet"
    pattern = str(tmp_path / "child_*.parquet")
    result = CliRunner().invoke(
        cli,
        ["edge-mosaic", pattern, str(synthetic_overlays), str(output_path)],
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


def test_mosaic_file_majority_vote_overrides_input_plurality(
    synthetic_inputs_file_majority, synthetic_overlays, tmp_path
):
    """An input whose plurality favors another overlay follows its file majority."""
    output_path = tmp_path / "out.parquet"
    mosaic(
        synthetic_inputs_file_majority,
        synthetic_overlays,
        output_path,
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        rows = conn.execute(
            f"SELECT id, ST_Area(geometry) FROM '{output_path}' ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    straddler_area = dict(rows)[2]
    assert straddler_area == pytest.approx(4.5, abs=1e-6)


def test_cli_extra_input_flag_combines_with_glob(
    synthetic_inputs_file_majority, synthetic_overlays, tmp_path
):
    """A glob-matched file plus a --input-flagged file both feed one combined run."""
    file_a, file_b = synthetic_inputs_file_majority
    output_path = tmp_path / "out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-mosaic",
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
    assert ids == [1, 2, 3]


def test_cli_glob_no_matches(synthetic_overlays, tmp_path):
    pattern = str(tmp_path / "nomatch_*.parquet")
    result = CliRunner().invoke(
        cli,
        [
            "edge-mosaic",
            pattern,
            str(synthetic_overlays),
            str(tmp_path / "out.parquet"),
        ],
    )
    assert result.exit_code != 0
    assert "no files matched" in result.output


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
    """rows: list of (pid, wkt, pcode); no `id` column to collide with an input's."""
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


def test_match_overrides_spatial_and_reports_mismatch(tmp_path):
    """An input feature's own file mostly overlaps overlay A, but its code says B."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((1 0, 10.5 0, 10.5 1, 1 1, 1 0))", "P2")],
    )

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        match_column="pcode",
        overwrite=True,
    )

    assert output_path.exists()
    assert issues_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        area = conn.execute(
            f"SELECT ST_Area(geometry) FROM '{output_path}'"
        ).fetchone()[0]
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]
    # Clipped to overlay B's sliver (x:10-10.5), not overlay A's larger x:1-3.
    assert area == pytest.approx(0.5, abs=1e-6)
    assert kinds == ["code-mismatch"]


def test_match_falls_back_when_code_unmatched(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")],
    )
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "NOPE")],
    )

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(
        input_path,
        overlays_path,
        output_path,
        issues_path,
        match_column="pcode",
        overwrite=True,
    )

    assert output_path.exists()
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        kinds = [
            row[0]
            for row in conn.execute(f"SELECT kind FROM '{issues_path}'").fetchall()
        ]
    assert kinds == ["code-fallback"]


def test_cli_match_help():
    result = CliRunner().invoke(cli, ["edge-mosaic", "--help"])
    assert result.exit_code == 0
    assert "--match-column" in result.output
    assert "--overlay-match-column" in result.output
    assert "--input-match-column" in result.output


def test_mosaic_merge_columns_populates_output(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    output_path = tmp_path / "out.parquet"
    mosaic(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        pcode = conn.execute(f"SELECT pcode FROM '{output_path}'").fetchone()[0]
    assert pcode == "P1"


def test_mosaic_merge_bare_carries_every_overlay_column(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")]
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    output_path = tmp_path / "out.parquet"
    mosaic(input_path, overlays_path, output_path, merge=True, overwrite=True)

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        row = conn.execute(f"SELECT id, pid, pcode FROM '{output_path}'").fetchone()
    assert row == (1, 1, "P1")


def test_mosaic_gap_fill_keeps_unmatched_overlay(tmp_path):
    """Overlay B (fid 2) gets zero matched inputs, so it carries through unclipped."""
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
    mosaic(
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


def test_mosaic_gap_fill_still_reports_clip_empty_inputs(tmp_path):
    """Gap-fill rescues Overlay B; inputs 3/4 still clip-empty against Overlay A."""
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
    mosaic(
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
        clip_empty_fids = {
            row[0]
            for row in conn.execute(
                f"SELECT unit_a FROM '{issues_path}' WHERE kind = 'clip-empty'"
            ).fetchall()
        }
    assert clip_empty_fids == {3, 4}


def test_mosaic_gap_fill_merged_columns_populated(tmp_path):
    """A gap-filled overlay's carried columns hold its own real value, not NULL."""
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
    mosaic(
        input_path,
        overlays_path,
        output_path,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        pcode = conn.execute(
            f"SELECT pcode FROM '{output_path}' WHERE overlay_fid = 2"
        ).fetchone()[0]
    assert pcode == "P2"


def test_cli_merge_gap_fill(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, _INPUT_WKT)
    output_path = tmp_path / "out.parquet"
    result = CliRunner().invoke(
        cli,
        [
            "edge-mosaic",
            str(input_path),
            str(overlays_path),
            str(output_path),
            "--merge",
        ],
    )
    assert result.exit_code == 0, result.output
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        overlay_fids = {
            row[0]
            for row in conn.execute(
                f"SELECT overlay_fid FROM '{output_path}'"
            ).fetchall()
        }
    gap_filled_overlay_fid = 2
    assert gap_filled_overlay_fid in overlay_fids


def test_cli_merge_help():
    result = CliRunner().invoke(cli, ["edge-mosaic", "--help"])
    assert result.exit_code == 0
    assert "--merge" in result.output


def test_mosaic_input_exclude_drops_named_input_column(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_overlay_pcode_only(
        overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")]
    )
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    output_path = tmp_path / "out.parquet"
    mosaic(
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


def test_mosaic_narrowing_flag_without_merge_raises(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    with pytest.raises(ValueError, match="require merge"):
        mosaic(
            input_path,
            overlays_path,
            tmp_path / "out.parquet",
            overlay_include=["pcode"],
            overwrite=True,
        )


def test_mosaic_prefer_mutually_exclusive_with_narrowing_flags(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_synthetic(input_path, [_INPUT_WKT[0]])

    with pytest.raises(ValueError, match="mutually exclusive"):
        mosaic(
            input_path,
            overlays_path,
            tmp_path / "out.parquet",
            merge=True,
            prefer="overlay",
            overlay_include=["pcode"],
            overwrite=True,
        )


def test_mosaic_prefer_overlay_resolves_real_collision(tmp_path):
    """id/geom/pcode all overlap between the two layers; pcode is the real collision."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "CHILDVAL")],
    )

    output_path = tmp_path / "out.parquet"
    mosaic(
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


def test_mosaic_prefer_input_resolves_real_collision(tmp_path):
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(overlays_path, [(1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1")])
    input_path = tmp_path / "children.parquet"
    _write_with_code(
        input_path,
        [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "CHILDVAL")],
    )

    output_path = tmp_path / "out.parquet"
    mosaic(
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


def test_mosaic_multi_file_zero_overlap_file_does_not_abort_batch(tmp_path):
    """A zero-overlap file in a 3+ file default-drop batch must not abort the run."""
    overlays_path = tmp_path / "parents.parquet"
    _write_synthetic(overlays_path, _OVERLAY_WKT)
    file_a = tmp_path / "file_a.parquet"
    file_b = tmp_path / "file_b.parquet"
    file_c = tmp_path / "file_c.parquet"
    _write_synthetic(file_a, [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))")])
    _write_synthetic(
        file_b, [(2, "POLYGON((10.5 0.5, 11 0.5, 11 1, 10.5 1, 10.5 0.5))")]
    )
    _write_synthetic(
        file_c, [(3, "POLYGON((100 100, 101 100, 101 101, 100 101, 100 100))")]
    )

    output_path = tmp_path / "out.parquet"
    issues_path = tmp_path / "issues.parquet"
    mosaic(
        [file_a, file_b, file_c],
        overlays_path,
        output_path,
        issues_path,
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
    assert ids == [1, 2]
    assert kinds == ["unassigned"]


def test_mosaic_multi_file_code_join_fid_stays_unique(tmp_path):
    """Per-file fid_offset keeps input_fid globally unique across a 3+ file batch."""
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
            (3, "POLYGON((20 0, 23 0, 23 3, 20 3, 20 0))", "P3"),
        ],
    )
    file_a = tmp_path / "file_a.parquet"
    file_b = tmp_path / "file_b.parquet"
    file_c = tmp_path / "file_c.parquet"
    _write_with_code(
        file_a, [(1, "POLYGON((0.5 0.5, 1 0.5, 1 1, 0.5 1, 0.5 0.5))", "P1")]
    )
    _write_with_code(
        file_b, [(2, "POLYGON((10.5 0.5, 11 0.5, 11 1, 10.5 1, 10.5 0.5))", "P2")]
    )
    _write_with_code(
        file_c, [(3, "POLYGON((20.5 0.5, 21 0.5, 21 1, 20.5 1, 20.5 0.5))", "P3")]
    )

    output_path = tmp_path / "out.parquet"
    mosaic(
        [file_a, file_b, file_c],
        overlays_path,
        output_path,
        match_column="pcode",
        overwrite=True,
    )

    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        columns = {
            row[0] for row in conn.execute(f"DESCRIBE '{output_path}'").fetchall()
        }
        rows = conn.execute(f"SELECT id FROM '{output_path}' ORDER BY id").fetchall()
    assert "source_file" not in columns
    assert [r[0] for r in rows] == [1, 2, 3]


def test_mosaic_multi_file_step_rejected(
    synthetic_inputs_split, synthetic_overlays, tmp_path
):
    with pytest.raises(ValueError, match="step is not supported"):
        mosaic(
            synthetic_inputs_split,
            synthetic_overlays,
            tmp_path / "out.parquet",
            step="assign",
            overwrite=True,
        )


def test_match_and_mosaic_merge_give_identical_results(tmp_path):
    """edge-match on raw input features == edge-mosaic on their edge-extend()ed version.

    Same --merge overlay-column carry and overlay gap-fill outcome either way,
    proving the two tools' --merge behavior is a true superset relationship.
    """
    overlays_path = tmp_path / "parents.parquet"
    _write_with_code(
        overlays_path,
        [
            (1, "POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))", "P1"),
            (2, "POLYGON((10 0, 13 0, 13 3, 10 3, 10 0))", "P2"),
        ],
    )
    raw_inputs_path = tmp_path / "children.parquet"
    _write_synthetic(raw_inputs_path, [_INPUT_WKT[0]])  # only overlaps Overlay A

    extended_inputs_path = tmp_path / "children_extended.parquet"
    extend(raw_inputs_path, extended_inputs_path, overwrite=True)

    match_out = tmp_path / "match_out.parquet"
    match(
        raw_inputs_path,
        overlays_path,
        match_out,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )
    mosaic_out = tmp_path / "mosaic_out.parquet"
    mosaic(
        extended_inputs_path,
        overlays_path,
        mosaic_out,
        merge=True,
        overlay_include=["pcode"],
        overwrite=True,
    )

    # A normally-clipped row's overlay_fid is dropped by the shared clip
    # engine (only a gap-fill row sets it explicitly), so compare on pcode.
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        match_pcodes = {
            row[0]
            for row in conn.execute(f"SELECT pcode FROM '{match_out}'").fetchall()
        }
        mosaic_pcodes = {
            row[0]
            for row in conn.execute(f"SELECT pcode FROM '{mosaic_out}'").fetchall()
        }
    assert match_pcodes == mosaic_pcodes == {"P1", "P2"}


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
        rows = conn.execute(f"SELECT * FROM '{path}'").fetchall()
    return cols, rows


def test_fill_schema_off_by_default_leaves_output_unchanged(
    admin_inputs, synthetic_overlays, tmp_path
):
    output_path = tmp_path / "out.parquet"
    mosaic(admin_inputs, synthetic_overlays, output_path, overwrite=True)
    cols, _ = _columns_and_rows(output_path)
    assert "adm_lvl" not in cols


def test_fill_schema_stamps_depth_and_fills(admin_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    mosaic(
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
            "edge-mosaic",
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
        mosaic(
            synthetic_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            overwrite=True,
            fill_schema=True,
        )


def test_fill_depth_column_flag(admin_inputs, synthetic_overlays, tmp_path):
    output_path = tmp_path / "out.parquet"
    mosaic(
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
        mosaic(
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
        mosaic(
            admin_inputs,
            synthetic_overlays,
            tmp_path / "out.parquet",
            overwrite=True,
            name_field="adm{n}_name",
            code_field="adm{n}_code",
        )


def test_fill_schema_multi_file(synthetic_overlays, tmp_path):
    """Second insertion point: _mosaic_multi_file()'s own outputs branch."""
    path_a = tmp_path / "child_a.parquet"
    path_b = tmp_path / "child_b.parquet"
    _write_admin_synthetic(path_a, [_ADMIN_INPUT_ROWS[0]])
    _write_admin_synthetic(path_b, [_ADMIN_INPUT_ROWS[1]])
    output_path = tmp_path / "out.parquet"
    mosaic(
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
