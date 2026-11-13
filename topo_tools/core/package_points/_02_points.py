"""Dissolves each detected level and reduces it to a pole-of-inaccessibility point."""

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import family_level
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


def _name_siblings(
    schema: TargetSchema, columns: list[str], levels: list[int]
) -> dict[str, dict[int, str]]:
    """Each level's extra name columns (`adm1_name1`, ...), keyed like "name1"."""
    siblings: dict[str, dict[int, str]] = {}
    for column in columns:
        n = family_level(column, schema.name_field)
        own = schema.name_field.format(n=n) if n is not None else None
        if n not in levels or column == own:
            continue
        kind = "name" + column[len(own) :].lstrip("_")
        siblings.setdefault(kind, {})[n] = column
    return siblings


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
        # Every other level's identity column, extra-language names included,
        # else an ancestor's `adm0_name1` rides along on every finer point.
        exclude = [
            column
            for per_level in families.values()
            for lvl, column in per_level.items()
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

    An explicit schema always uses its own fixed code/name role names instead.
    """
    if schema is not None:
        families: dict[str, dict[int, str]] = {"code": {}, "name": {}}
        for n in levels:
            families["code"][n] = schema.code_field.format(n=n)
            families["name"][n] = schema.name_field.format(n=n)
        columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
        families.update(_name_siblings(schema, columns, levels))
        return families
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
        msg = f"depth_column {depth_column!r} already exists on {table_in!r}"
        raise ValueError(msg)

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
    generalizable: set[str] | None = None
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

        dissolved_columns = [
            r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
        ]
        # The synthetic root has nothing coarser to compare against, so
        # it never seeds/consults `generalizable`; real levels still do.
        if root_injected and n == 0:
            allowed = set(dissolved_columns)
        else:
            if generalizable is None:
                generalizable = set(dissolved_columns)
            allowed = generalizable
        own_rename = level_family_names(families, n, group_by[0] if group_by else None)
        own_rename = {
            col: generic
            for col, generic in own_rename.items()
            if col in dissolved_columns
        }
        # A column dropped from a coarser level's own dissolve (e.g. a
        # finest-level-only attribute) can never generalize across levels.
        drop_cols = [
            c for c in dissolved_columns if c not in allowed and c not in own_rename
        ]
        exclude_sql = ", ".join(f'"{c}"' for c in ["geom", *drop_cols])
        rename_sql = ", ".join(f'"{old}" AS "{new}"' for old, new in own_rename.items())
        rename_clause = f" RENAME ({rename_sql})" if rename_sql else ""

        points = f"{name}_02_pts{n}"
        conn.execute(f"""--sql
            CREATE OR REPLACE TABLE "{points}" AS
            SELECT * EXCLUDE ({exclude_sql}){rename_clause}, {n} AS "{depth_column}",
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
