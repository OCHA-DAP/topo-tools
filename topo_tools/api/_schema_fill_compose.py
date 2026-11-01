"""Opt-in schema-fill composition for edge-match/edge-mosaic."""

from duckdb import DuckDBPyConnection

from topo_tools.core.schema_fill import _02_fill as fill_stage
from topo_tools.core.schema_map._level_columns import detect_level_columns
from topo_tools.core.schema_map._levels import detect_levels
from topo_tools.core.schema_map._target_schema import resolve_explicit_target_schema


def validate_fill_flags(
    *, fill_schema: bool, name_field: str | None, code_field: str | None
) -> None:
    """Raise if name_field/code_field are given without fill_schema."""
    if (name_field is not None or code_field is not None) and not fill_schema:
        msg = "name_field/code_field require fill_schema=True"
        raise ValueError(msg)


def apply_optional_fill(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    table: str,
    *,
    requested: bool,
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
    debug: bool,
) -> None:
    """Fill table in place via schema-fill's own stage, right before export."""
    if not requested:
        return

    columns = [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    if depth_column in columns:
        msg = f"depth_column {depth_column!r} already exists on {table!r}"
        raise ValueError(msg)

    schema = resolve_explicit_target_schema(name_field, code_field)
    levels = (
        detect_levels(conn, table, schema)
        if schema is not None
        else sorted(detect_level_columns(conn, table))
    )

    pre_table = f"{name}_fill_01"
    post_table = f"{name}_fill_02"
    conn.execute(f'ALTER TABLE "{table}" RENAME TO "{pre_table}"')
    fill_stage.main(
        conn,
        pre_table,
        post_table,
        levels=levels,
        schema=schema,
        depth_column=depth_column,
    )
    conn.execute(f'ALTER TABLE "{post_table}" RENAME TO "{table}"')
    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{pre_table}"')
