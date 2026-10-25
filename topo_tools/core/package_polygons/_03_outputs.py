"""Validates topology and exports each detected level's dissolved output."""

from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import output_renames
from topo_tools.core.coverage import check_valid_topology, gap_issues_sql
from topo_tools.core.io import export_geometry_table, export_issues_table

logger = getLogger(__name__)


@dataclass(frozen=True)
class LevelOutput:
    """One level's resolved source table and output destinations.

    A None `dest` means this level is skipped entirely (the finest level,
    when its computed path resolves to the same file as the input).
    """

    level: int
    table: str
    dest: Path | None
    issues_dest: Path | None


def _export_one(
    conn: DuckDBPyConnection,
    name: str,
    item: LevelOutput,
    renames: tuple[tuple[str, str], tuple[str | None, str | None]] | None,
) -> None:
    check_valid_topology(conn, item.table)

    issues_table = f"{name}_03_{item.level}"
    micro = (
        f'UNION ALL BY NAME SELECT * FROM "{name}_01_micro"'
        if item.table == f"{name}_01"
        else ""
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{issues_table}" AS
        {gap_issues_sql(conn, item.table, coverage=True)}
        {micro}
    """)
    remaining = conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{issues_table}" WHERE kind = 'gap'
    """).fetchall()[0][0]
    if remaining:
        logger.warning(
            "package-polygons: %d gap(s) wider than the noise floor remain in "
            "level %d's output (may be a legitimate unfilled gap, not a "
            "defect), see the issues file",
            remaining,
            item.level,
        )

    columns = [r[0] for r in conn.execute(f'DESCRIBE "{item.table}"').fetchall()]
    export_geometry_table(
        conn,
        item.table,
        item.dest,
        renames=output_renames(columns, *renames) if renames else None,
    )
    export_issues_table(conn, issues_table, item.issues_dest)
    conn.execute(f'DROP TABLE IF EXISTS "{issues_table}"')


def main(
    conn: DuckDBPyConnection,
    name: str,
    items: list[LevelOutput],
    *,
    renames: tuple[tuple[str, str], tuple[str | None, str | None]] | None = None,
    debug: bool = False,
) -> None:
    """Export every level with a non-None dest; drop intermediates unless debug.

    renames is ((name_field, code_field), (output_name_field, output_code_field)).
    """
    for item in items:
        if item.dest is not None:
            _export_one(conn, name, item, renames)

    if not debug:
        for item in items:
            if item.table != f"{name}_01":
                conn.execute(f'DROP TABLE IF EXISTS "{item.table}"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
        conn.execute(f'DROP TABLE IF EXISTS "{name}_01_micro"')
