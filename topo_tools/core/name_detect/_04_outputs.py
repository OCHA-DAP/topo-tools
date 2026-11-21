"""Rolls up column-wide findings and writes the issues report, even when empty."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.code import TABLE_COPY_OPTS
from topo_tools.core.io import add_csv_bom, export_geometry_table
from topo_tools.core.name_detect._constants import (
    PER_NAME_KINDS,
    ROLLUP_MIN_ROWS,
    ROLLUP_SHARE,
    SEVERITY,
)

REPORT_SUFFIXES = (".csv", ".parquet")

_ORDER = (
    "key, kind, severity, level, name_column, code_a, name_a, code_b, name_b, "
    "suggested, reason"
)


def _report_sql(name: str, fixed: str | None) -> str:
    """Per-unit findings, a column-wide per-name kind collapsed into one row."""
    kinds = ", ".join(f"'{k}'" for k in PER_NAME_KINDS)
    severity = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in SEVERITY.items())
    return f"""--sql
        WITH totals AS (
            SELECT level, name_column, COUNT(*) AS total
            FROM "{name}_02" GROUP BY ALL
        ), hits AS (
            SELECT kind, level, name_column, COUNT(*) AS n, any_value(reason) AS reason
            FROM "{name}_03" WHERE kind IN ({kinds}) GROUP BY ALL
        ), rolled AS (
            SELECT h.* FROM hits h JOIN totals t USING (level, name_column)
            WHERE h.n >= {ROLLUP_MIN_ROWS} AND h.n > t.total * {ROLLUP_SHARE}
        ), findings AS (
            SELECT i.* FROM "{name}_03" i
            ANTI JOIN rolled r USING (kind, level, name_column)
            UNION ALL
            SELECT r.kind, r.level, r.name_column, NULL, NULL, NULL, NULL, NULL,
                   printf('%d of %d names in this column: %s', r.n, t.total, r.reason)
            FROM rolled r JOIN totals t USING (level, name_column)
        ), merged AS (
            -- One unit blank in several language columns is one row.
            SELECT * FROM findings WHERE kind <> 'blank-name' OR code_a IS NULL
            UNION ALL
            SELECT kind, level, string_agg(name_column, ', ' ORDER BY name_column),
                   code_a, NULL, NULL, NULL, NULL, any_value(reason)
            FROM findings WHERE kind = 'blank-name' AND code_a IS NOT NULL
            GROUP BY kind, level, code_a
        )
        SELECT kind || '-' || row_number() OVER (
                   PARTITION BY kind ORDER BY level, name_column, code_a, name_a
               ) AS key,
               kind, CASE kind {severity} END AS severity, level, name_column,
               code_a, name_a, code_b, name_b, suggested, reason
               {f", {fixed} AS fixed" if fixed else ""}
        FROM merged
    """


def main(
    conn: DuckDBPyConnection,
    name: str,
    dest: Path,
    *,
    fixed: str | None = None,
    debug: bool = False,
) -> None:
    """Write `{name}_04` to dest: CSV without geometry, Parquet with each unit's.

    fixed, if given, is a SQL expression over a report row added as `fixed`.
    """
    order = f"{_ORDER}, fixed" if fixed else _ORDER
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_04" AS
        SELECT * FROM ({_report_sql(name, fixed)})
        ORDER BY severity, level, name_column, kind, code_a, name_a
    """)
    dest.parent.mkdir(exist_ok=True, parents=True)
    if dest.suffix == ".csv":
        conn.execute(
            f"COPY (SELECT {order} FROM \"{name}_04\") TO '{dest}' "
            f"{TABLE_COPY_OPTS['.csv']}"
        )
        add_csv_bom(dest)
    else:
        code_columns = conn.execute(
            f'SELECT DISTINCT level, code_column FROM "{name}_02"'
        ).fetchall()
        unions = " UNION ALL ".join(
            f"""--sql
            SELECT i.key, ST_Union_Agg(t.geom) AS geom
            FROM "{name}_04" i JOIN "{name}_01" t
              ON t."{column.replace('"', '""')}"::VARCHAR IN (i.code_a, i.code_b)
            WHERE i.level = {level} GROUP BY i.key
            """
            for level, column in code_columns
        )
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{name}_04_tmp1" AS
            SELECT {order}, g.geom
            FROM "{name}_04" LEFT JOIN ({unions}) g USING (key)
            ORDER BY severity, level, name_column, kind, code_a, name_a
        """)
        export_geometry_table(conn, f"{name}_04_tmp1", dest, exclude_fid=False)
        conn.execute(f'DROP TABLE IF EXISTS "{name}_04_tmp1"')

    if not debug:
        for suffix in ("01", "02", "03", "04"):
            conn.execute(f'DROP TABLE IF EXISTS "{name}_{suffix}"')
