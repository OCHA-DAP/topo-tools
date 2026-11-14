"""Dissolves each detected level and reduces it to a pole-of-inaccessibility point."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import generic_families
from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.schema_map._level_columns import (
    LevelColumns,
    detect_level_columns_or_single,
    detect_root_level,
    group_families_by_level,
    level_family_names,
    verify_functional_cluster,
)
from topo_tools.core.schema_map._target_schema import TargetSchema

logger = getLogger(__name__)


def _check_row_count(
    conn: DuckDBPyConnection, dissolved: str, source: str, group_by: list[str]
) -> None:
    dissolved_count = conn.execute(f'SELECT COUNT(*) FROM "{dissolved}"').fetchone()[0]
    if not group_by:
        distinct_count = 1
    else:
        group_by_sql = ", ".join(f'"{c}"' for c in group_by)
        distinct_count = conn.execute(
            f'SELECT COUNT(DISTINCT ({group_by_sql})) FROM "{source}"'
        ).fetchone()[0]
    if dissolved_count != distinct_count:
        msg = (
            f"{dissolved}: {dissolved_count} dissolved row(s) but "
            f"{distinct_count} distinct {group_by!r} value(s) in {source}"
        )
        raise ValueError(msg)


def _check_covers(conn: DuckDBPyConnection, dissolved: str, points: str) -> None:
    uncovered = conn.execute(f"""--sql
        SELECT COUNT(*) FROM "{dissolved}" d
        JOIN "{points}" p USING (fid)
        WHERE NOT ST_Covers(d.geom, p.geom)
    """).fetchone()[0]
    if uncovered:
        msg = f"{points}: {uncovered} label point(s) not covered by their own polygon"
        raise ValueError(msg)


def _level_group_by(
    schema: TargetSchema | None,
    level_columns: dict[int, LevelColumns] | None,
    n: int,
    families: dict[str, dict[int, str]],
    *,
    allow_empty_root: bool = False,
) -> tuple[list[str], list[str]]:
    """One level's dissolve group_by/exclude, from an explicit schema or auto-detect."""
    if schema is not None:
        exclude = [
            col
            for per_level in families.values()
            for lvl, col in per_level.items()
            if lvl != n
        ]
        return [schema.code_field.format(n=n)], exclude

    cluster = level_columns[n]
    if (
        not cluster.group_by
        and len(level_columns) > 1
        and not (n == 0 and allow_empty_root)
    ):
        msg = f"no reliable group-by column detected for level {n}"
        raise ValueError(msg)
    # An ancestor never survives under its own numbered name: a row
    # already carries its own level via the depth column.
    exclude = [
        c
        for lvl, cols in level_columns.items()
        if lvl != n
        for c in cols.identity_columns
    ]
    return cluster.group_by, exclude


def _identity_families(
    conn: DuckDBPyConnection,
    table: str,
    schema: TargetSchema | None,
    level_columns: dict[int, LevelColumns] | None,
    levels: list[int],
) -> dict[str, dict[int, str]]:
    """Every level's own identity columns, keyed by the file's own naming for it.

    An explicit schema keys its fields as code/name and their siblings by suffix.
    """
    if schema is not None:
        columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
        return generic_families(columns, levels, schema.name_field, schema.code_field)
    if level_columns is None or not any(
        cols.group_by for cols in level_columns.values()
    ):
        return {}
    return group_families_by_level(conn, table, level_columns)


def main(
    conn: DuckDBPyConnection,
    name: str,
    levels: list[int],
    schema: TargetSchema | None,
    depth_column: str,
) -> None:
    """Dissolve `{name}_01` per level and union its label points into `{name}_02`.

    A None `schema` triggers structural auto-detection of each level's columns.
    """
    table_in = f"{name}_01"
    columns = [row[0] for row in conn.execute(f'DESCRIBE "{table_in}"').fetchall()]
    if depth_column in columns:
        # Each point is stamped with the level it labels, like package-lines.
        logger.info("package-points: replacing input column %r", depth_column)
        conn.execute(f'ALTER TABLE "{table_in}" DROP "{depth_column}"')

    level_columns: dict[int, LevelColumns] | None = None
    root_injected = False
    if schema is None:
        level_columns = detect_level_columns_or_single(conn, table_in)
        root = detect_root_level(conn, table_in, level_columns)
        if root is not None:
            level_columns = {0: root, **level_columns}
            levels = sorted({0, *levels})
            root_injected = True
    families = _identity_families(conn, table_in, schema, level_columns, levels)

    point_tables = []
    for n in levels:
        group_by, exclude = _level_group_by(
            schema, level_columns, n, families, allow_empty_root=root_injected
        )
        if schema is None and group_by:
            verify_functional_cluster(conn, table_in, group_by[0], group_by)

        dissolved = f"{name}_02_tmp{n}"
        dissolve_stage.main(
            conn,
            table_in,
            dissolved,
            group_by=group_by,
            exclude=exclude,
            target_schema=schema,
        )
        _check_row_count(conn, dissolved, table_in, group_by)

        dissolved_columns = {
            r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
        }
        own_rename = level_family_names(families, n, group_by[0] if group_by else None)
        # Points carry only their own level's identity; metadata stays on polygons.
        identity_sql = "".join(
            f', "{col}" AS "{generic}"'
            for col, generic in own_rename.items()
            if col in dissolved_columns
        )

        points = f"{name}_02_pts{n}"
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{points}" AS
            SELECT fid{identity_sql}, {n} AS "{depth_column}",
                   (ST_MaximumInscribedCircle(geom)).center AS geom
            FROM "{dissolved}"
        """)
        _check_covers(conn, dissolved, points)
        conn.execute(f'DROP TABLE IF EXISTS "{dissolved}"')
        point_tables.append(points)

    union_sql = " UNION ALL BY NAME ".join(f'SELECT * FROM "{t}"' for t in point_tables)
    conn.execute(f'CREATE OR REPLACE TABLE "{name}_02" AS {union_sql}')
    for t in point_tables:
        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
