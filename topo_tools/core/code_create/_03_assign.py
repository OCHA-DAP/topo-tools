"""Top-down per-level code assignment: level 0 literal, 1..N via the shared cascade."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import next_free_sibling
from topo_tools.core.code import (
    CodeFormat,
    assign_new_codes,
    check_unique_names,
    seed_code_from_names,
    write_root_code,
)
from topo_tools.core.code_create._02_levels import Level
from topo_tools.core.code_create._constants import SOURCE_CODES

logger = getLogger(__name__)

_INTEGER_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
}  # fmt: skip


def _copy_source_codes(
    conn: DuckDBPyConnection, table: str, levels: dict[int, Level]
) -> None:
    """Keep each level's source code in its next free numbered sibling column."""
    columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    taken = set(columns)
    siblings: dict[str, str] = {}
    for n in sorted(levels):
        level = levels[n]
        if level.seeded or (n == 0 and (1 not in levels or levels[1].seeded)):
            continue
        siblings[level.code] = next_free_sibling(level.code, taken)
        taken.add(siblings[level.code])
    select = []
    for column in columns:
        select.append(f'"{column}"')
        if column in siblings:
            select.append(f'"{column}"::VARCHAR AS "{siblings[column]}"')
    conn.execute(
        f'CREATE OR REPLACE TABLE "{table}" AS '
        f'SELECT {", ".join(select)} FROM "{table}"'
    )


def _strip_parent_prefixes(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    table: str,
    levels: dict[int, Level],
    fmt: CodeFormat,
    integers: set[int],
    *,
    root_sql: str,
) -> None:
    """Strip each level's parent source code (or root) where every code repeats it."""
    numbered = sorted(n for n in levels if n >= 1)
    # Finest first, so each parent column still holds its own source code.
    for n in reversed(numbered):
        level = levels[n]
        if level.seeded:
            continue
        if n == numbered[0]:
            prefix_sql, params = f"{root_sql}::VARCHAR", []
        elif levels[n - 1].seeded:
            continue
        else:
            prefix_sql, params = f'"{levels[n - 1].code}"::VARCHAR', []
        code_sql = f'"{level.code}"::VARCHAR'
        matched, total, rest_lengths = conn.execute(
            f"""--sql
            SELECT
                COUNT(*) FILTER (
                    WHERE starts_with({code_sql}, {prefix_sql})
                      AND length({code_sql}) > length({prefix_sql})
                ),
                COUNT({code_sql}),
                COUNT(DISTINCT length({code_sql}) - length({prefix_sql}))
            FROM "{table}"
            """,
            params * 3,
        ).fetchone()
        if matched == 0:
            continue
        mixed = fmt.delimiter == "" and rest_lengths > 1
        if n in integers and (matched < total or mixed):
            logger.info(
                "level %d (%r): codes don't all repeat their parent's code at one "
                "width; padding them as numbered within the parent",
                n,
                level.code,
            )
            continue
        if matched < total:
            msg = (
                f"level {n} ({level.code!r}): {matched} of {total} source codes "
                "start with their parent's code; they must all, or none"
            )
            raise ValueError(msg)
        conn.execute(
            f'UPDATE "{table}" SET "{level.code}" = '
            f"substr({code_sql}, length({prefix_sql}) + 1)",
            params,
        )


def _prepare_source_codes(
    conn: DuckDBPyConnection, table: str, levels: dict[int, Level]
) -> set[int]:
    """Cast code columns to VARCHAR, blanks to NULL; return the integer levels."""
    types = dict(
        conn.execute(
            "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?",
            [table],
        ).fetchall()
    )
    integers = set()
    for n, level in sorted(levels.items()):
        if level.code not in types:
            continue
        if types[level.code] in _INTEGER_TYPES:
            integers.add(n)
        conn.execute(f'ALTER TABLE "{table}" ALTER "{level.code}" TYPE VARCHAR')
        conn.execute(
            f'UPDATE "{table}" SET "{level.code}" = NULL '
            f"WHERE trim(\"{level.code}\") = ''"
        )
        if n == 0 or level.seeded:
            continue
        (nulls,) = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{level.code}" IS NULL'
        ).fetchone()
        if nulls:
            # Ranking would merge every code-less unit under a parent into one.
            msg = f"level {n} ({level.code!r}) has {nulls} row(s) with no source code"
            raise ValueError(msg)
    return integers


def _fix_width(  # noqa: PLR0913 (each param is a distinct required input)
    conn: DuckDBPyConnection,
    table: str,
    n: int,
    level: Level,
    fmt: CodeFormat,
    *,
    integer: bool,
    root_sql: str,
) -> None:
    """Without a delimiter, zero-pad integer codes per root; raise if mixed."""
    if fmt.delimiter != "":
        return
    per_root = conn.execute(f"""--sql
        SELECT list(DISTINCT length("{level.code}")) FROM "{table}" GROUP BY {root_sql}
    """).fetchall()
    for (raw,) in per_root:
        lengths = sorted(raw or [])
        if len(lengths) > 1 and not integer:
            msg = (
                f"level {n} ({level.code!r}) source codes vary in length "
                f"{lengths}; without a delimiter the code can't be split"
            )
            raise ValueError(msg)
    if integer:
        conn.execute(
            f"""--sql
            UPDATE "{table}"
            SET "{level.code}" = lpad("{level.code}", GREATEST(?, w.len)::INTEGER, '0')
            FROM (
                SELECT {root_sql} AS root, max(length("{level.code}")) AS len
                FROM "{table}" GROUP BY 1
            ) w
            WHERE {root_sql} = w.root
            """,
            [fmt.width(n) or 0],
        )


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table: str,
    *,
    levels: dict[int, Level],
    fmt: CodeFormat,
    source_codes: str = "replace",
    root_sql: str,
    root_column: str | None = None,
) -> None:
    """Assign a code into each level's own column, in place, root to leaf.

    root_sql is the root as SQL: a quoted literal, or root_column's own values.
    """
    if source_codes not in SOURCE_CODES:
        msg = f"source_codes must be one of {SOURCE_CODES}, got {source_codes!r}"
        raise ValueError(msg)
    fmt.check_level_count(sum(1 for n in levels if n >= 1))
    integers = _prepare_source_codes(conn, table, levels)
    if source_codes == "copy":
        _copy_source_codes(conn, table, levels)
    for n, level in sorted(levels.items()):
        if level.seeded:
            seed_code_from_names(conn, table, n, level.code, level.name)
            if n == max(levels):
                parent = levels[n - 1].code if n - 1 in levels else None
                check_unique_names(conn, table, n, level.code, parent)
    if source_codes == "embed":
        _strip_parent_prefixes(conn, table, levels, fmt, integers, root_sql=root_sql)
    if 0 in levels and levels[0].code != root_column:
        root, top = levels[0], levels[1]
        after = (root.name,) if root.name else (top.name, top.code)
        write_root_code(conn, table, root.code, root_sql, after)

    parent_sql = root_sql
    parent_column = root_column
    for n in sorted(level for level in levels if level >= 1):
        code_column = levels[n].code
        if source_codes == "embed" and not levels[n].seeded:
            _fix_width(
                conn,
                table,
                n,
                levels[n],
                fmt,
                integer=n in integers,
                root_sql=root_sql,
            )
            conn.execute(
                f'UPDATE "{table}" SET "{code_column}" = '
                f'{parent_sql} || ? || "{code_column}"::VARCHAR',
                [fmt.delimiter],
            )
            parent_sql = f'"{code_column}"'
            parent_column = code_column
            continue
        staging = f"{table}_code_lvl{n}"
        conn.execute(f"""--sql
            CREATE OR REPLACE TEMP TABLE "{staging}" AS
            SELECT ROW_NUMBER() OVER () AS row_id, orig_code,
                   orig_code AS code_val, parent_code, root_code
            FROM (
                SELECT DISTINCT
                    "{code_column}" AS orig_code,
                    {parent_sql} AS parent_code,
                    {root_sql} AS root_code
                FROM "{table}"
            ) d
        """)
        assign_new_codes(
            conn,
            staging,
            id_column="row_id",
            parent_column="parent_code",
            sort_columns=["code_val"],
            code_column="code_val",
            fmt=fmt,
            level=n,
            width_group_column="root_code",
        )
        # A raw value MAY repeat across parents; match on parent too so this
        # never collides with, or drops, the same value/NULL elsewhere.
        parent_match = (
            "TRUE"
            if parent_column is None
            else f't."{parent_column}"::VARCHAR IS NOT DISTINCT FROM s.parent_code'
        )
        conn.execute(f"""--sql
            UPDATE "{table}" t
            SET "{code_column}" = s.code_val
            FROM "{staging}" s
            WHERE t."{code_column}" IS NOT DISTINCT FROM s.orig_code AND {parent_match}
        """)
        conn.execute(f'DROP TABLE IF EXISTS "{staging}"')
        parent_sql = f'"{code_column}"'
        parent_column = code_column
