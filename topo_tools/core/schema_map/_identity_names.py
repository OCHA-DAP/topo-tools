"""Resolves the level-free names packaging gives each identity column family."""

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import check_generic_names, generic_families
from topo_tools.core.duckdb_utils import quote_identifier
from topo_tools.core.schema_map._level_columns import (
    detect_levels_with_root,
    fallback_families,
    group_families_by_level,
    level_family_names,
)
from topo_tools.core.schema_map._levels import detect_levels
from topo_tools.core.schema_map._target_schema import TargetSchema


def generic_names_by_family(
    families: dict[str, dict[int, str]], levels: list[int], code_columns: list
) -> dict[str, dict[int, str]]:
    """Rekey auto-detected families by their generic name at their finest level."""
    named = []
    for kind, per_level in families.items():
        lvl = max(per_level)
        code = code_columns[levels.index(lvl)] if lvl in levels else None
        generic = level_family_names({kind: per_level}, lvl, code)[per_level[lvl]]
        named.append((kind, generic, per_level))
    check_generic_names([(kind, generic) for kind, generic, _ in named])
    return {generic: per_level for _, generic, per_level in named}


def check_identity_names(
    conn: DuckDBPyConnection,
    table: str,
    schema: TargetSchema | None,
    depth_column: str = "adm_lvl",
) -> None:
    """Raise ValueError if package-points or package-lines would clash on a name."""
    reserved = {"fid", "geom", "geometry", depth_column}
    if schema is not None:
        columns = [
            r[0] for r in conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
        ]
        levels = detect_levels(conn, table, schema)
        generic_families(
            columns, levels, schema.name_field, schema.code_field, reserved=reserved
        )
        return
    level_columns = detect_levels_with_root(conn, table)
    levels = sorted(level_columns)
    codes = [
        level_columns[n].group_by[0] if level_columns[n].group_by else None
        for n in levels
    ]
    families = group_families_by_level(conn, table, level_columns) or fallback_families(
        level_columns, levels
    )
    for n, code in zip(levels, codes, strict=True):
        renames = level_family_names(families, n, code)
        check_generic_names(list(renames.items()), reserved)
    generic_names_by_family(families, levels, codes)
