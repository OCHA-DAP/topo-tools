"""Coverage-cleans the clipped output, closing seams between overlay polygons."""

from duckdb import DuckDBPyConnection

from topo_tools.core.coverage import coverage_clean_rows


def coverage_rows_sql(name: str, *, passthrough: bool) -> str:
    """Build a WHERE for the coverage rows: orphan rows overlap by design."""
    if not passthrough:
        return "TRUE"
    return f"""NOT coalesce(
        fid IN (SELECT input_fid FROM "{name}_02_unassigned"), FALSE
    )"""


def main(conn: DuckDBPyConnection, name: str, *, passthrough: bool = False) -> None:
    """Coverage-clean `{name}_04` in place, micro rows in `{name}_04_micro`."""
    table = f"{name}_04"
    where = coverage_rows_sql(name, passthrough=passthrough)
    coverage_clean_rows(conn, table, where, micro_issues_table=f"{table}_micro")
