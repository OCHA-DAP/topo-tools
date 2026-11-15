"""Groups rows by attribute columns and unions their geometry per group."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import template_families
from topo_tools.core.constants import is_noise_column, is_numeric_duckdb_type
from topo_tools.core.schema_map._levels import detect_levels
from topo_tools.core.schema_map._target_schema import TargetSchema

logger = getLogger(__name__)

_ALLOWED_AGGREGATIONS = {
    "sum": "SUM",
    "min": "MIN",
    "max": "MAX",
    "avg": "AVG",
    "first": "ANY_VALUE",
}


def _distinct_counts(
    conn: DuckDBPyConnection,
    table: str,
    group_by: list[str],
    columns: list[str],
) -> dict[str, int]:
    """Return {column: max distinct count in any group}.

    Collapses per-group counts to one summary row in SQL, so result size
    scales with `columns`, never with group count (can be hundreds of thousands).
    """
    if not columns:
        return {}
    if not group_by:
        counts_sql = ", ".join(f'COUNT(DISTINCT "{c}")' for c in columns)
        row = conn.execute(f'SELECT {counts_sql} FROM "{table}"').fetchall()[0]
        return dict(zip(columns, row, strict=True))
    group_by_sql = ", ".join(f'"{c}"' for c in group_by)
    counts_sql = ", ".join(
        f'COUNT(DISTINCT "{c}") AS "__auto_{i}"' for i, c in enumerate(columns)
    )
    summary_sql = ", ".join(f'MAX("__auto_{i}")' for i in range(len(columns)))
    row = conn.execute(f"""--sql
        WITH counts AS (
            SELECT {group_by_sql}, {counts_sql}
            FROM "{table}"
            GROUP BY {group_by_sql}
        )
        SELECT {summary_sql} FROM counts
    """).fetchall()[0]
    return dict(zip(columns, row, strict=True))


def _schema_derived_exclusions(
    conn: DuckDBPyConnection,
    table: str,
    group_by: list[str],
    schema: TargetSchema,
) -> set[str]:
    """Return every column at a level finer than group_by's own detected level."""
    columns = [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    levels = detect_levels(conn, table, schema)

    target_level = None
    for level in sorted(levels, reverse=True):
        if schema.code_field.format(n=level) in group_by or (
            schema.name_field.format(n=level) in group_by
        ):
            target_level = level
            break
    if target_level is None:
        msg = (
            "target_schema given but no group_by column matches any "
            f"detected level: {group_by}"
        )
        raise ValueError(msg)

    finer_levels = [level for level in levels if level > target_level]
    families = template_families(
        columns, finer_levels, schema.name_field, schema.code_field
    )
    return {column for per_level in families.values() for column in per_level.values()}


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table_in: str,
    table_out: str,
    *,
    group_by: list[str],
    exclude: list[str] | None = None,
    target_schema: TargetSchema | None = None,
    aggregations: dict[str, str] | None = None,
) -> None:
    """Dissolve table_in into table_out, grouping by `group_by`, unioning geometry.

    A NULL `group_by` value forms its own group; other columns are kept if
    constant per group, else summed if numeric (else dropped), unless overridden.
    """
    for column, function in (aggregations or {}).items():
        if function not in _ALLOWED_AGGREGATIONS:
            msg = f"unsupported aggregation {function!r} for column {column!r}"
            raise ValueError(msg)

    always_excluded = {*group_by, "fid", "geom", *(exclude or [])}
    if target_schema is not None:
        always_excluded |= _schema_derived_exclusions(
            conn, table_in, group_by, target_schema
        )
    describe_rows = conn.execute(f'DESCRIBE "{table_in}"').fetchall()
    column_types = {row[0]: row[1] for row in describe_rows}
    noise_cols = {
        c for c in column_types if c not in {"fid", "geom"} and is_noise_column(c)
    }
    always_excluded |= noise_cols
    candidate_cols = sorted(set(column_types) - always_excluded)

    stats = _distinct_counts(conn, table_in, group_by, candidate_cols)

    kept_cols: list[str] = []
    summed_cols: list[str] = []
    overridden_cols: dict[str, str] = {}
    dropped_cols: list[str] = []
    for c in candidate_cols:
        if c in (aggregations or {}):
            overridden_cols[c] = _ALLOWED_AGGREGATIONS[aggregations[c]]
        elif stats[c] <= 1:
            kept_cols.append(c)
        elif is_numeric_duckdb_type(column_types[c]):
            summed_cols.append(c)
        else:
            dropped_cols.append(c)
    if dropped_cols:
        logger.warning(
            "dissolve: dropping %d column(s) not constant within every group: %s",
            len(dropped_cols),
            dropped_cols,
        )
    if summed_cols:
        logger.info(
            "dissolve: summing %d non-constant numeric column(s): %s",
            len(summed_cols),
            summed_cols,
        )

    group_cols_sql = [f'"{c}"' for c in group_by]
    exprs = {c: f'"{c}"' for c in group_by}
    exprs |= {c: f'any_value("{c}") AS "{c}"' for c in kept_cols}
    exprs |= {c: f'SUM("{c}") AS "{c}"' for c in summed_cols}
    exprs |= {c: f'{func}("{c}") AS "{c}"' for c, func in overridden_cols.items()}

    # Input column order, so an admin hierarchy keeps its own level order.
    select_sql = ", ".join(exprs[c] for c in column_types if c in exprs)
    group_clause = f"GROUP BY {', '.join(group_cols_sql)}" if group_by else ""
    # A window in the same query as ST_Union_Agg runs the union about 3x slower.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        SELECT {select_sql}, ST_MakeValid(ST_Union_Agg(geom)) AS geom
        FROM "{table_in}"
        {group_clause}
    """)
    order_sql = f"ORDER BY {', '.join(group_cols_sql)}" if group_by else ""
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{table_out}" AS
        SELECT row_number() OVER ({order_sql}) AS fid, * FROM "{table_out}"
        {order_sql}
    """)
