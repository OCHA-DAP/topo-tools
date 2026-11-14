"""Dissolves the finest layer into every coarser detected admin level."""

from duckdb import DuckDBPyConnection

from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.schema_map._level_columns import (
    LevelColumns,
    detect_levels_with_root,
    verify_functional_cluster,
)
from topo_tools.core.schema_map._target_schema import TargetSchema


def main(
    conn: DuckDBPyConnection,
    name: str,
    levels: list[int],
    schema: TargetSchema | None,
    *,
    aggregations: dict[str, str] | None = None,
) -> None:
    """Dissolve `{name}_01` into `{name}_02_{n}` for every level but the finest.

    A None `schema` triggers structural auto-detection of each level's own
    group-by columns instead of an explicit target-schema code column.
    """
    finest = max(levels)
    table = f"{name}_01"

    if schema is not None:
        for n in levels:
            if n == finest:
                continue
            dissolve_stage.main(
                conn,
                table,
                f"{name}_02_{n}",
                group_by=[schema.code_field.format(n=n)],
                target_schema=schema,
                aggregations=aggregations,
            )
        return

    level_columns: dict[int, LevelColumns] = detect_levels_with_root(conn, table)
    for n in levels:
        if n == finest:
            continue
        cluster = level_columns[n]
        # A constant root dissolves the whole layer into one unit.
        is_root = n == 0 and cluster.identity_columns and not cluster.group_by
        if not cluster.group_by and not is_root:
            msg = f"no reliable group-by column detected for level {n} in {table!r}"
            raise ValueError(msg)
        if cluster.group_by:
            verify_functional_cluster(
                conn, table, cluster.group_by[0], cluster.group_by
            )
        # A cardinality-only lookalike (e.g. area_sqkm) isn't in identity_columns;
        # leave it for dissolve's own numeric-sum default.
        exclude = [
            c
            for lvl, cols in level_columns.items()
            if lvl > n
            for c in cols.identity_columns
        ]
        dissolve_stage.main(
            conn,
            table,
            f"{name}_02_{n}",
            group_by=cluster.group_by,
            exclude=exclude,
            aggregations=aggregations,
        )
