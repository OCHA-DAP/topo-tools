"""Unions in the own geometry of any overlay polygon no input polygon matched."""

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import SHARED_POINT_TOLERANCE
from topo_tools.core.duckdb_utils import bbox_columns_sql


def fill_unmatched_overlays(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    *,
    carry_columns: list[str] | None = None,
    result_table: str,
    overlay_snapshot_table: str,
    points_table: str | None = None,
) -> None:
    """Append each unmatched overlay polygon's own row (unclipped) onto result_table.

    One INSERT per fid, not one bulk copy: a true global overlay polygon can leave
    most of its rows unmatched, so this avoids materializing them all at once.
    Each is snapped to the `points_table` points (x, y, p) near it, if given.
    """
    carry_sql = "".join(f', o."{c}"' for c in (carry_columns or []))
    # result_table's clip output may lack overlay_fid/a carry column entirely;
    # add every column BY NAME below relies on, even with zero unmatched fids.
    column_types = {
        row[0]: row[1]
        for row in conn.execute(f'DESCRIBE "{overlay_snapshot_table}"').fetchall()
    }
    for column, column_type in [("overlay_fid", column_types["fid"])] + [
        (c, column_types[c]) for c in (carry_columns or [])
    ]:
        conn.execute(f"""--sql
            ALTER TABLE "{result_table}"
            ADD COLUMN IF NOT EXISTS "{column}" {column_type}
        """)
    fids = conn.execute(f"""--sql
        SELECT fid FROM "{overlay_snapshot_table}"
        WHERE fid NOT IN (SELECT DISTINCT overlay_fid FROM "{name}_02_assign")
    """).fetchall()
    tol = SHARED_POINT_TOLERANCE
    for (fid,) in fids:
        snap_join, geom_sql = "", "o.geom"
        if points_table is not None:
            snap_join = f"""
                LEFT JOIN (
                    SELECT ST_Collect(list(p.p ORDER BY p.x, p.y)) AS mp
                    FROM "{points_table}" p, (
                        SELECT {bbox_columns_sql("geom")}
                        FROM "{overlay_snapshot_table}" WHERE fid = {fid}
                    ) b
                    WHERE p.x BETWEEN b.xmin - {tol} AND b.xmax + {tol}
                      AND p.y BETWEEN b.ymin - {tol} AND b.ymax + {tol}
                    HAVING count(*) > 0
                ) s ON TRUE"""
            geom_sql = f"coalesce(ST_Snap(o.geom, s.mp, {tol}), o.geom)"
        conn.execute(f"""--sql
            INSERT INTO "{result_table}" BY NAME
            SELECT o.fid AS overlay_fid, {geom_sql} AS geom{carry_sql}
            FROM "{overlay_snapshot_table}" o{snap_join}
            WHERE o.fid = {fid}
        """)
