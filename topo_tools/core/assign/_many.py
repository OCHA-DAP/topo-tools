"""Assigns each input polygon to the overlay polygon it shares the largest area with."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.assign._one import _carry_forward_columns
from topo_tools.core.constants import CRS, EQUAL_AREA_CRS
from topo_tools.core.duckdb_utils import bbox_columns_sql

logger = getLogger(__name__)


def assign_many(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    *,
    overlay_match_column: str | None = None,
    input_match_column: str | None = None,
    carry_columns: list[str] | None = None,
    input_columns: list[str] | None = None,
) -> None:
    """Assign each input polygon to its plurality-overlap overlay; log the rest."""
    # Bbox columns precomputed here, not called inline in the join below:
    # DuckDB re-evaluates an inline envelope call per comparison, not once per row.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_input_01"
        )
        SELECT fid, part_geom, {bbox_columns_sql("part_geom")}
        FROM parts
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        WITH parts AS (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS part_geom FROM "{name}_overlay_01"
        )
        SELECT fid, part_geom, {bbox_columns_sql("part_geom")}
        FROM parts
    """)

    # Shared area per (input, overlay) fid pair, summed across all part-pairs;
    # ranked in an equal-area CRS, transforming only the intersection geometry.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_pairs" AS
        SELECT c.fid AS input_fid, p.fid AS overlay_fid,
               SUM(ST_Area(ST_Transform(
                   ST_Intersection(c.part_geom, p.part_geom),
                   '{CRS}', '{EQUAL_AREA_CRS}', true
               ))) AS shared_area
        FROM "{name}_02_tmp1" c
        JOIN "{name}_02_tmp2" p
          ON p.xmax >= c.xmin
         AND p.xmin <= c.xmax
         AND p.ymax >= c.ymin
         AND p.ymin <= c.ymax
         AND ST_Intersects(c.part_geom, p.part_geom)
        GROUP BY c.fid, p.fid
    """)

    # Plurality pick per input polygon, ties broken by lowest overlay fid.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
        SELECT input_fid, overlay_fid FROM (
            SELECT input_fid, overlay_fid,
                   ROW_NUMBER() OVER (
                       PARTITION BY input_fid ORDER BY shared_area DESC, overlay_fid ASC
                   ) AS rn
            FROM "{name}_02_pairs"
            WHERE shared_area > 0
        ) WHERE rn = 1
    """)

    if overlay_match_column and input_match_column:
        # Code candidate per input polygon, restricted to an overlay it overlaps at all
        # (guards against a stale code); ties broken by lowest overlay fid.
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
            SELECT input_fid, overlay_fid FROM (
                SELECT j.input_fid, j.overlay_fid,
                       ROW_NUMBER() OVER (
                           PARTITION BY j.input_fid ORDER BY j.overlay_fid ASC
                       ) AS rn
                FROM (
                    SELECT c.fid AS input_fid, p.fid AS overlay_fid
                    FROM "{name}_input_01" c
                    JOIN "{name}_overlay_01" p
                      ON c."{input_match_column}" = p."{overlay_match_column}"
                ) j
                JOIN "{name}_02_pairs" pr
                  ON pr.input_fid = j.input_fid
                 AND pr.overlay_fid = j.overlay_fid
                 AND pr.shared_area > 0
            ) WHERE rn = 1
        """)
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_assign" AS
            SELECT
                input_fid,
                COALESCE(code.overlay_fid, spatial.overlay_fid) AS overlay_fid,
                CASE WHEN code.overlay_fid IS NOT NULL THEN 'code'
                     ELSE 'spatial_fallback' END AS assignment_method,
                CASE WHEN code.overlay_fid IS NOT NULL
                     THEN code.overlay_fid = spatial.overlay_fid END AS spatial_agrees
            FROM "{name}_02_tmp4" code
            FULL OUTER JOIN "{name}_02_tmp3" spatial USING (input_fid)
        """)
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp4"')
    else:
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_02_assign" AS
            SELECT * FROM "{name}_02_tmp3"
        """)
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp3"')

    _carry_forward_columns(conn, name, carry_columns, input_columns)

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_unassigned" AS
        SELECT fid AS input_fid, source_file, geom FROM "{name}_input_01"
        WHERE fid NOT IN (SELECT input_fid FROM "{name}_02_assign")
    """)

    unassigned = conn.execute(
        f'SELECT input_fid FROM "{name}_02_unassigned" ORDER BY input_fid'
    ).fetchall()
    if unassigned:
        fids = [row[0] for row in unassigned]
        logger.warning(
            "assign-many: dropping %d unmatched input fid(s) with no overlay polygon "
            "overlap: %s",
            len(fids),
            fids,
        )

    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp1"')
    conn.execute(f'DROP TABLE IF EXISTS "{name}_02_tmp2"')
