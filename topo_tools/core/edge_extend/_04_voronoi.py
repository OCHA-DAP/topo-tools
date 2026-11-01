"""Generates Voronoi polygons from boundary points and clips to bounding extent."""

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import WORLD_BOUNDS_WKT


class AssignmentError(RuntimeError):
    """Raised when a point falls in no Voronoi cell, or a cell holds several."""


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Create Voronoi polygons from points."""
    # A stable per-point id: a fid can have many boundary points, so
    # completeness below must track individual points, not fids.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp0" AS
        SELECT row_number() OVER () AS point_id, fid, geom FROM "{name}_03b"
    """)

    # Clipped per-cell, after the dump, only when a cell actually exceeds
    # bounds: see docs/adr/0096 for why a global or unconditional clip breaks.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
        SELECT row_number() OVER () AS cell_id, CASE
            WHEN ST_XMin(geom) < -180 OR ST_XMax(geom) > 180
              OR ST_YMin(geom) < -90 OR ST_YMax(geom) > 90
            THEN ST_CollectionExtract(
                ST_Intersection(geom, ST_GeomFromText('{WORLD_BOUNDS_WKT}')), 3
            )
            ELSE geom
        END AS geom
        FROM (
            SELECT UNNEST(ST_Dump(
                ST_CollectionExtract(
                    ST_VoronoiDiagram(ST_Collect(list(geom))), 3
                )
            )).geom AS geom
            FROM "{name}_04_tmp0"
        )
    """)

    # ST_Intersects (not ST_Within) catches generators landing exactly on a
    # cell boundary; such a point can match multiple cells, so dedupe by point_id.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp2" AS
        SELECT a.point_id, a.fid, b.cell_id, b.geom,
            ST_Contains(b.geom, a.geom) AS inside
        FROM "{name}_04_tmp0" AS a
        JOIN "{name}_04_tmp1" AS b
        ON ST_Intersects(a.geom, b.geom)
    """)

    point_count = conn.execute(f'SELECT count(*) FROM "{name}_04_tmp0"').fetchall()[0][
        0
    ]
    assigned_count = conn.execute(
        f'SELECT count(DISTINCT point_id) FROM "{name}_04_tmp2"'
    ).fetchall()[0][0]
    if assigned_count < point_count:
        msg = (
            f"Voronoi assignment incomplete: "
            f"{assigned_count}/{point_count} points assigned"
        )
        raise AssignmentError(msg)
    check_cells(conn, name)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_03b"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp0"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')

    # ST_MakeValid defends against invalid cells from degenerate point
    # configurations: feeding one to ST_Union_Agg segfaults GEOS.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT fid, ST_Union_Agg(ST_MakeValid(geom)) AS geom
        FROM "{name}_04_tmp2"
        GROUP BY fid
    """)

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp2"')


def check_cells(conn: DuckDBPyConnection, name: str) -> None:
    """Raise if any Voronoi cell holds more than one distinct generator point."""
    # A Voronoi cell holds exactly one generator: a cell holding several
    # distinct points is a degenerate GEOS cell that would overlap its neighbours.
    bad = conn.execute(f"""--sql
        WITH multi AS (
            SELECT cell_id FROM "{name}_04_tmp2" WHERE inside
            GROUP BY cell_id HAVING count(*) > 1
        )
        SELECT t.cell_id, count(DISTINCT (ST_X(p.geom), ST_Y(p.geom))) AS n,
            count(DISTINCT t.fid) AS fids, any_value(ST_Extent(c.geom)) AS bbox
        FROM "{name}_04_tmp2" t
        JOIN multi USING (cell_id)
        JOIN "{name}_04_tmp0" p USING (point_id)
        JOIN "{name}_04_tmp1" c USING (cell_id)
        WHERE t.inside
        GROUP BY t.cell_id
        HAVING n > 1
        ORDER BY n DESC
    """).fetchall()
    if bad:
        cell_id, n, fids, bbox = bad[0]
        msg = (
            f"Voronoi cell check failed: {len(bad)} cell(s) hold more than one "
            f"point, worst cell {cell_id} holds {n} points from {fids} fid(s), "
            f"bbox {bbox}"
        )
        raise AssignmentError(msg)
