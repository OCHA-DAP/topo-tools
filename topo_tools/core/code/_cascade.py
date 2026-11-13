"""Ranks rows per parent group, assigns sequential formatted hierarchical codes."""

from duckdb import DuckDBPyConnection

from topo_tools.core.code._code_format import CodeFormat
from topo_tools.core.code._next_available import used_integers


def _sql_literal(value: str) -> str:
    """Single-quote a SQL string literal, escaping any embedded quote."""
    return "'" + value.replace("'", "''") + "'"


def assign_new_codes(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table: str,
    *,
    id_column: str,
    parent_column: str,
    sort_columns: list[str],
    code_column: str,
    fmt: CodeFormat,
    level: int,
    existing_codes: list[str] | None = None,
    width_group_column: str | None = None,
) -> None:
    """Assign each row in table a new sequential code within its own parent group.

    Under auto width, rows sharing a width_group_column value share one width.
    """
    existing_codes = existing_codes or []
    counts = dict(
        conn.execute(
            f'SELECT "{parent_column}", COUNT(*) FROM "{table}" GROUP BY 1'
        ).fetchall()
    )
    width = fmt.width(level)
    numbers = []
    for parent, count in counts.items():
        used = used_integers(existing_codes, parent, fmt)
        base = max(used, default=0) + 1
        picked = range(base, base + count)
        if fmt.delimiter == "" and width is not None and picked[-1] >= 10**width:
            # Placeholders like 99 count down from the top; number below them.
            cutoff = 9 * 10 ** (width - 1)
            base = max((i for i in used if i < cutoff), default=0) + 1
            picked = range(base, base + count)
            if picked[-1] >= cutoff:
                msg = (
                    f"level {level}: {parent!r} needs codes up to {picked[-1]}, past "
                    f"the top 10% ({cutoff}+) kept for placeholders at min_width "
                    f"{width}; without a delimiter the code can't be split, use a "
                    "wider width"
                )
                raise ValueError(msg)
        numbers.extend((parent, rank, n) for rank, n in enumerate(picked, start=1))

    numbers_table = f"{table}_code_numbers"
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP TABLE "{numbers_table}" (
            parent_code VARCHAR, rank INTEGER, n INTEGER
        )
    """)
    conn.executemany(f'INSERT INTO "{numbers_table}" VALUES (?, ?, ?)', numbers)

    if width is None:
        # auto: pad every tail at this level to the widest one, retained included.
        existing_width = max(
            (len(c.rsplit(fmt.delimiter, 1)[-1]) for c in existing_codes), default=1
        )
        width_sql = (
            f"GREATEST({existing_width}, "
            "MAX(LENGTH(tail)) OVER (PARTITION BY width_group))"
        )
    else:
        width_sql = str(width)

    group_sql = f'src."{width_group_column}"' if width_group_column else "NULL"
    order_sql = ", ".join(f'"{c}" NULLS LAST' for c in sort_columns)
    ranked_table = f"{table}_code_ranked"
    # lpad truncates a too-long string (unlike Python's zfill); widen the
    # target width to the tail's own length first so it only ever pads.
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP TABLE "{ranked_table}" AS
        WITH numbered AS (
            SELECT
                r.id_value, r.parent_code, r.width_group,
                CAST(num.n AS VARCHAR) AS tail
            FROM (
                SELECT
                    src."{id_column}" AS id_value,
                    src."{parent_column}" AS parent_code,
                    {group_sql} AS width_group,
                    ROW_NUMBER() OVER (
                        PARTITION BY src."{parent_column}" ORDER BY {order_sql}
                    ) AS rank
                FROM "{table}" src
            ) r
            JOIN "{numbers_table}" num
              ON num.parent_code IS NOT DISTINCT FROM r.parent_code
             AND num.rank = r.rank
        )
        SELECT
            id_value,
            parent_code || {_sql_literal(fmt.delimiter)} ||
            lpad(tail, CAST(GREATEST({width_sql}, LENGTH(tail)) AS INTEGER), '0')
            AS new_code
        FROM numbered
    """)
    conn.execute(f"""--sql
        UPDATE "{table}" t
        SET "{code_column}" = r.new_code
        FROM "{ranked_table}" r
        WHERE t."{id_column}" = r.id_value
    """)
    conn.execute(f'DROP TABLE IF EXISTS "{numbers_table}"')
    conn.execute(f'DROP TABLE IF EXISTS "{ranked_table}"')
