"""Grid-tiles an oversized overlay boundary before intersecting against it."""

import math

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import (
    CLIP_TILE_MAX_CELL,
    CLIP_TILE_MIN_CELL,
    CLIP_TILE_MIN_VERTICES,
    CLIP_TILE_TARGET_VERTICES,
    GEOMETRY_TYPE,
)


def _adaptive_cell_size(vertex_count: int, width: float, height: float) -> float:
    """Solve a tile size from this overlay's vertex density, not a fixed constant.

    Calibrated so South Africa's real worst case (281k vertices, ADR-0016) lands
    at ~1 degree; sparser or simpler overlays get coarser cells, denser ones finer.
    """
    bbox_area = max(width, 1e-9) * max(height, 1e-9)
    cell = math.sqrt(CLIP_TILE_TARGET_VERTICES * bbox_area / vertex_count)
    return min(max(cell, CLIP_TILE_MIN_CELL), CLIP_TILE_MAX_CELL)


def subdivide_boundary(
    conn: DuckDBPyConnection, source_table: str, geom_col: str, out_table: str
) -> None:
    """Grid-subdivide source_table's single-row geometry into out_table, tile by tile.

    Below CLIP_TILE_MIN_VERTICES, out_table just gets the geometry directly.
    """
    x0, y0, x1, y1 = conn.execute(
        f"""--sql
        SELECT ST_XMin({geom_col}), ST_YMin({geom_col}),
               ST_XMax({geom_col}), ST_YMax({geom_col})
        FROM "{source_table}"
        """
    ).fetchone()
    vertex_count = (
        conn.execute(f'SELECT ST_NPoints({geom_col}) FROM "{source_table}"').fetchone()[
            0
        ]
        or 0
    )

    if vertex_count < CLIP_TILE_MIN_VERTICES:
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{out_table}" AS
            SELECT {geom_col} AS geom FROM "{source_table}"
        """)
        return

    cell = _adaptive_cell_size(vertex_count, x1 - x0, y1 - y0)
    ny = max(1, math.ceil((y1 - y0) / cell))

    occupied = [
        row[0]
        for row in conn.execute(
            f"""--sql
            WITH parts AS (
                SELECT (UNNEST(ST_Dump({geom_col}))).geom AS g FROM "{source_table}"
            )
            SELECT DISTINCT UNNEST(range(
                CAST(floor((ST_XMin(g) - {x0}) / {cell}) AS INTEGER),
                CAST(floor((ST_XMax(g) - {x0}) / {cell}) AS INTEGER) + 1
            )) AS i
            FROM parts ORDER BY i
            """
        ).fetchall()
    ]

    conn.execute(f'CREATE OR REPLACE TABLE "{out_table}" (geom {GEOMETRY_TYPE})')
    for i in occupied:
        sx0, sx1 = x0 + i * cell, x0 + (i + 1) * cell
        sy0, sy1 = y0, y1
        conn.execute(f"""--sql
            INSERT INTO "{out_table}"
            WITH strip AS (
                SELECT ST_Intersection(
                    {geom_col}, ST_MakeEnvelope({sx0}, {sy0}, {sx1}, {sy1})
                ) AS g
                FROM "{source_table}"
            ),
            gy AS (
                SELECT {sy0} + j * {cell} AS cy0,
                       {sy0} + (j + 1) * {cell} AS cy1
                FROM (SELECT UNNEST(range({ny})) AS j)
            )
            SELECT geom FROM (
                SELECT ST_Intersection(
                    strip.g, ST_MakeEnvelope({sx0}, gy.cy0, {sx1}, gy.cy1)
                ) AS geom
                FROM strip, gy
                WHERE NOT ST_IsEmpty(strip.g)
            ) WHERE NOT ST_IsEmpty(geom)
        """)
