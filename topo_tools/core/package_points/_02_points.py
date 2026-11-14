"""Dissolves each detected level and reduces it to a pole-of-inaccessibility point."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import check_generic_names, generic_families
from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.schema_map._level_columns import (
    LevelColumns,
    detect_levels_with_root,
    fallback_families,
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
    """One level's dissolve group_by and own identity columns."""
    own = [per_level[n] for per_level in families.values() if n in per_level]
    if schema is not None:
        return [schema.code_field.format(n=n)], own

    cluster = level_columns[n]
    if (
        not cluster.group_by
        and len(level_columns) > 1
        and not (n == 0 and allow_empty_root)
    ):
        msg = f"no reliable group-by column detected for level {n}"
        raise ValueError(msg)
    return cluster.group_by, own or list(
        dict.fromkeys(cluster.group_by + cluster.identity_columns)
    )


def _identity_families(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table: str,
    schema: TargetSchema | None,
    level_columns: dict[int, LevelColumns] | None,
    levels: list[int],
    *,
    depth_column: str,
) -> dict[str, dict[int, str]]:
    """Every level's own identity columns, keyed by the file's own naming for it.

    An explicit schema keys its fields as code/name and their siblings by suffix.
    """
    if schema is not None:
        columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
        return generic_families(
            columns,
            levels,
            schema.name_field,
            schema.code_field,
            reserved={"fid", "geom", "geometry", depth_column},
        )
    if level_columns is None or not any(
        cols.group_by for cols in level_columns.values()
    ):
        return {}
    return group_families_by_level(conn, table, level_columns) or fallback_families(
        level_columns, levels
    )


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    levels: list[int],
    schema: TargetSchema | None,
    depth_column: str,
    *,
    min_level: int | None = None,
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
        level_columns = detect_levels_with_root(conn, table_in)
        root_injected = 0 in level_columns and not level_columns[0].group_by
    families = _identity_families(
        conn, table_in, schema, level_columns, levels, depth_column=depth_column
    )

    columns = [r[0] for r in conn.execute(f'DESCRIBE "{table_in}"').fetchall()]
    selected = [lvl for lvl in levels if min_level is None or lvl >= min_level]
    if not selected:
        msg = f"no admin level at or above min_level {min_level}"
        raise ValueError(msg)
    point_tables = []
    for n in selected:
        group_by, own = _level_group_by(
            schema, level_columns, n, families, allow_empty_root=root_injected
        )
        if schema is None and group_by:
            verify_functional_cluster(conn, table_in, group_by[0], group_by)

        # Points carry only their own level's identity; metadata stays on polygons.
        keep = {*group_by, *own, "fid", "geom"}
        dissolved = f"{name}_02_tmp{n}"
        dissolve_stage.main(
            conn,
            table_in,
            dissolved,
            group_by=group_by,
            exclude=[c for c in columns if c not in keep],
            target_schema=schema,
        )
        _check_row_count(conn, dissolved, table_in, group_by)

        dissolved_columns = {
            r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
        }
        renames = level_family_names(families, n, group_by[0] if group_by else None)
        check_generic_names(
            [(col, renames.get(col, col)) for col in own if col in dissolved_columns],
            reserved={"fid", "geom", "geometry", depth_column},
        )
        identity_sql = "".join(
            f', "{col}" AS "{renames.get(col, col)}"'
            for col in own
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
