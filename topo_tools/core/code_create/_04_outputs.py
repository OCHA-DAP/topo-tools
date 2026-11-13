"""Exports the coded output, plus an overflow issues report when non-empty."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.code import TABLE_COPY_OPTS, CodeFormat
from topo_tools.core.code_create._02_levels import Level
from topo_tools.core.io import add_csv_bom, export_geometry_table


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table: str,
    dest: Path,
    *,
    levels: dict[int, Level],
    fmt: CodeFormat,
    source_codes: str = "replace",
    root_sql: str,
    issues_dest: Path | None = None,
    debug: bool = False,
) -> None:
    """Export table to dest; write an overflow report to issues_dest if given."""
    export_geometry_table(conn, table, dest)

    if issues_dest is not None:
        _write_overflow_issues(
            conn, table, levels, fmt, source_codes, root_sql, issues_dest
        )

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')


def _write_overflow_issues(  # noqa: PLR0913, PLR0917
    conn: DuckDBPyConnection,
    table: str,
    levels: dict[int, Level],
    fmt: CodeFormat,
    source_codes: str,
    root_sql: str,
    issues_dest: Path,
) -> None:
    """Group each numbered level's codes by parent, flag any over min_width capacity."""
    rows: list[tuple[str, int, str, str, int, int, str]] = []
    parent_sql = f"{root_sql}::VARCHAR"
    for level in sorted(n for n in levels if n >= 1):
        code_column = levels[level].code
        width = fmt.width(level)
        if width is None or (source_codes == "embed" and not levels[level].seeded):
            parent_sql = f'"{code_column}"'
            continue
        capacity = 10**width - 1
        groups = conn.execute(
            f"""--sql
            SELECT {parent_sql}, COUNT(DISTINCT "{code_column}"),
                   max(struct_pack(
                       l := length("{code_column}"), c := "{code_column}"
                   )).c
            FROM "{table}"
            GROUP BY 1
            HAVING COUNT(DISTINCT "{code_column}") > ?
            """,
            [capacity],
        ).fetchall()
        for parent_code, count, assigned_code in groups:
            reason = f"{count} children exceeds {capacity} at min_width={width}"
            rows.append(
                (
                    "digit-overflow",
                    level,
                    parent_code,
                    assigned_code,
                    count,
                    width,
                    reason,
                )
            )
        parent_sql = f'"{code_column}"'

    if not rows:
        issues_dest.unlink(missing_ok=True)
        return

    issues_table = f"{table}_issues"
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP TABLE "{issues_table}" (
            kind VARCHAR, level INTEGER, parent_code VARCHAR, assigned_code VARCHAR,
            child_count INTEGER, min_width INTEGER, reason VARCHAR
        )
    """)
    conn.executemany(f'INSERT INTO "{issues_table}" VALUES (?, ?, ?, ?, ?, ?, ?)', rows)
    conn.execute(
        f"COPY (SELECT * FROM \"{issues_table}\") TO '{issues_dest}' "
        f"{TABLE_COPY_OPTS[issues_dest.suffix]}"
    )
    add_csv_bom(issues_dest)
    conn.execute(f'DROP TABLE IF EXISTS "{issues_table}"')
