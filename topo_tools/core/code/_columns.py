"""Places an added column next to its own level's columns, and writes level 0."""

from duckdb import DuckDBPyConnection


def insert_column_after(
    conn: DuckDBPyConnection,
    table: str,
    column: str,
    sql: str,
    after: tuple[str | None, ...],
) -> None:
    """Add column (valued by sql) after the last column starting with any of after."""
    columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    hits = [
        i for i, c in enumerate(columns) if any(a and c.startswith(a) for a in after)
    ]
    at = hits[-1] + 1 if hits else len(columns)
    select = [f'"{c}"' for c in columns]
    select.insert(at, f'{sql} AS "{column}"')
    conn.execute(
        f'CREATE OR REPLACE TABLE "{table}" AS SELECT {", ".join(select)} '
        f'FROM "{table}"'
    )


def write_root_code(
    conn: DuckDBPyConnection,
    table: str,
    column: str,
    root_sql: str,
    after: tuple[str | None, ...],
) -> None:
    """Set column to the root_sql expression on every row, adding it if missing."""
    columns = {r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()}
    if column not in columns:
        insert_column_after(conn, table, column, "NULL::VARCHAR", after)
    conn.execute(f'UPDATE "{table}" SET "{column}" = ({root_sql})::VARCHAR')
