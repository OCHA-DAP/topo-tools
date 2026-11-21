"""Every tool's geometry output sits on the GRID_RESOLUTION grid."""

from pathlib import Path

import duckdb
import pytest

from topo_tools.api.code_create import code_create
from topo_tools.api.edge_extend import extend
from topo_tools.api.package import package
from topo_tools.api.schema_fill import fill
from topo_tools.api.schema_map import map as schema_map
from topo_tools.api.topo_clean import clean
from topo_tools.api.topo_detect import detect

_DISTRICT_RAW = Path(__file__).parent / "fixtures" / "how_to_district_raw.parquet"


def _off_grid(path: Path) -> int:
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        return conn.execute(f"""
            SELECT count(*) FROM (
                SELECT UNNEST(ST_Dump(ST_Points(geometry))).geom AS p FROM '{path}'
            )
            WHERE abs(ST_X(p) * 1e9 - round(ST_X(p) * 1e9)) > 1e-3
               OR abs(ST_Y(p) * 1e9 - round(ST_Y(p) * 1e9)) > 1e-3
        """).fetchall()[0][0]


def _has_geometry(path: Path) -> bool:
    with duckdb.connect() as conn:
        rows = conn.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()
    return any(r[0] == "geometry" for r in rows)


@pytest.fixture
def shifted(tmp_path):
    """Move the district fixture a third of a grid cell, every vertex off the grid."""
    path = tmp_path / "shifted.parquet"
    with duckdb.connect() as conn:
        conn.execute("LOAD spatial")
        conn.execute(f"""
            COPY (
                SELECT * REPLACE (ST_Translate(geometry, 3.3e-10, 3.3e-10) AS geometry)
                FROM '{_DISTRICT_RAW}'
            ) TO '{path}' (FORMAT PARQUET)
        """)
    assert _off_grid(path) > 0
    return path


def test_every_output_is_on_grid(shifted, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    schema_map(shifted, out / "mapped.parquet")
    detect(shifted, out / "detect.parquet")
    clean(out / "mapped.parquet", out / "clean.parquet", maximum_gap_width="all")
    extend(out / "clean.parquet", out / "extend.parquet")
    fill(out / "clean.parquet", out / "fill.parquet")
    code_create(
        out / "clean.parquet",
        out / "coded.parquet",
        root_code="TT",
        delimiter=".",
        min_width=2,
    )
    package(out / "coded.parquet", output_path=str(out / "pkg_{x}.parquet"))

    written = [p for p in sorted(out.glob("*.parquet")) if _has_geometry(p)]
    assert {"extend.parquet", "pkg_points.parquet", "pkg_lines.parquet"} <= {
        p.name for p in written
    }
    assert {p.name: _off_grid(p) for p in written} == {p.name: 0 for p in written}
