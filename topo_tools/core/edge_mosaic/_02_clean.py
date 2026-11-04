"""Coverage-cleans the clipped output, closing seams between overlay polygons."""

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import coverage_clean_rows


def coverage_rows_sql(
    name: str, *, passthrough: bool, passthrough_fids: str | None = None
) -> str:
    """Build a WHERE for the coverage rows: passthrough rows overlap by design."""
    if not passthrough:
        return "TRUE"
    fids = passthrough_fids or f'SELECT input_fid FROM "{name}_02_unassigned"'
    return f"NOT coalesce(fid IN ({fids}), FALSE)"


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    passthrough: bool = False,
    passthrough_fids: str | None = None,
) -> None:
    """Coverage-clean `{name}_03` in place, micro rows in `{name}_03_micro`."""
    table = f"{name}_03"
    where = coverage_rows_sql(
        name, passthrough=passthrough, passthrough_fids=passthrough_fids
    )
    coverage_clean_rows(conn, table, where, micro_issues_table=f"{table}_micro")
