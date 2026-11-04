"""Unions in the own geometry of any overlay polygon no input polygon matched."""

from duckdb import DuckDBPyConnection


def fill_unmatched_overlays(
    conn: DuckDBPyConnection,
    name: str,
    *,
    carry_columns: list[str] | None = None,
    result_table: str,
    overlay_snapshot_table: str,
) -> None:
    """Append each unmatched overlay polygon's own row (unclipped) onto result_table.

    One INSERT per fid, not one bulk copy: a true global overlay polygon can leave
    most of its rows unmatched, so this avoids materializing them all at once.
    """
    carry_sql = "".join(f', "{c}"' for c in (carry_columns or []))
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
    for (fid,) in fids:
        conn.execute(f"""--sql
            INSERT INTO "{result_table}" BY NAME
            SELECT fid AS overlay_fid, geom{carry_sql}
            FROM "{overlay_snapshot_table}"
            WHERE fid = {fid}
        """)
