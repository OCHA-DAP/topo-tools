"""Finds a layer's finest admin level code and name columns, structurally."""

from duckdb import DuckDBPyConnection

from topo_tools.core.schema_map._resolve_levels import resolve_levels
from topo_tools.core.schema_map._target_schema import resolve_explicit_target_schema


def detect_unit_columns(
    conn: DuckDBPyConnection,
    table: str,
    name_field: str | None = None,
    code_field: str | None = None,
) -> tuple[str | None, str | None]:
    """Return the finest level's code and primary name column, None where not found."""
    resolve_explicit_target_schema(name_field, code_field)
    try:
        levels = resolve_levels(conn, table, name_field, code_field)
    except ValueError:
        return None, None
    finest = levels[max(levels)]
    return finest.code, finest.names[0] if finest.names else None
