"""Writes the code issues report, even when empty."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.code import TABLE_COPY_OPTS
from topo_tools.core.code_detect._constants import SEVERITY
from topo_tools.core.io import add_csv_bom, export_geometry_table

REPORT_SUFFIXES = (".csv", ".parquet")


def main(
    conn: DuckDBPyConnection, name: str, dest: Path, *, debug: bool = False
) -> None:
    """Write `{name}_04` to dest: CSV without geometry, Parquet with each unit's."""
    severity = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in SEVERITY.items())
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT kind || '-' || row_number() OVER (
                   PARTITION BY kind ORDER BY level, column_name, code_a, name_a
               ) AS key,
               kind, CASE kind {severity} END AS severity, level,
               column_name AS "column", code_a, name_a, code_b, name_b, reason
        FROM "{name}_03"
        ORDER BY severity, level, kind, "column", code_a, name_a
    """)
    dest.parent.mkdir(exist_ok=True, parents=True)
    if dest.suffix == ".csv":
        conn.execute(f"COPY \"{name}_04\" TO '{dest}' {TABLE_COPY_OPTS['.csv']}")
        add_csv_bom(dest)
    else:
        _write_with_geometry(conn, name, dest)
    if not debug:
        for suffix in ("01", "02", "03", "04"):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_{suffix}"')


def _write_with_geometry(conn: DuckDBPyConnection, name: str, dest: Path) -> None:
    """Write `{name}_04` with the union of the units carrying each row's code_a."""
    code_columns = conn.execute(
        f'SELECT DISTINCT level, code_column FROM "{name}_02"'
    ).fetchall()
    unions = " UNION ALL ".join(
        f"""--sql
        SELECT i.key, ST_Union_Agg(t.geom) AS geom
        FROM "{name}_04" i JOIN "{name}_01" t
          ON t."{column.replace('"', '""')}"::VARCHAR = i.code_a
        WHERE i.level = {level} GROUP BY i.key
        """
        for level, column in code_columns
    )
    geometry = f"LEFT JOIN ({unions}) g USING (key)" if unions else ""
    select_geom = "g.geom" if unions else "NULL::GEOMETRY AS geom"
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
        SELECT i.*, {select_geom} FROM "{name}_04" i {geometry}
        ORDER BY severity, level, kind, "column", code_a, name_a
    """)
    export_geometry_table(conn, f"{name}_04_tmp1", dest, exclude_fid=False)
    conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')
