"""Builds a deduplicated shared+exterior boundary line network, tagged by depth."""

from duckdb import DuckDBPyConnection

from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.schema_map._level_columns import (
    detect_level_columns_or_single,
    group_families_by_level,
    level_family_names,
    verify_functional_cluster,
)
from topo_tools.core.schema_map._target_schema import TargetSchema

_EMPTY_LINES = "ST_GeomFromText('MULTILINESTRING EMPTY')"


def _classification_sql(levels: list[int], code_columns: list[str]) -> str:
    if len(levels) == 1:
        return str(levels[0])
    cases = "".join(
        f'WHEN l."{col}" IS DISTINCT FROM r."{col}" THEN {level} '
        for level, col in zip(levels[:-1], code_columns[:-1], strict=True)
    )
    return f"CASE {cases}ELSE {levels[-1]} END"


def _check_every_fid_present(
    conn: DuckDBPyConnection, dissolved: str, lines: str
) -> None:
    missing = conn.execute(f"""--sql
        SELECT fid FROM "{dissolved}"
        WHERE fid NOT IN (
            SELECT left_fid FROM "{lines}"
            UNION
            SELECT right_fid FROM "{lines}" WHERE right_fid IS NOT NULL
        )
    """).fetchall()
    if missing:
        fids = [m[0] for m in missing]
        msg = f"{lines}: fid(s) missing from every boundary row: {fids}"
        raise ValueError(msg)


def main(
    conn: DuckDBPyConnection,
    name: str,
    levels: list[int],
    schema: TargetSchema | None,
    depth_column: str,
) -> None:
    """Dissolve at the finest level, then extract and classify its boundary network.

    A None `schema` triggers structural auto-detection of each level's columns.
    """
    reserved = {"left_fid", "right_fid", "geom"}
    if depth_column in reserved:
        msg = f"depth_column {depth_column!r} collides with a fixed output column"
        raise ValueError(msg)

    table_in = f"{name}_01"
    finest = max(levels)
    dissolved = f"{name}_02_dissolved"
    if schema is not None:
        code_columns = [schema.code_field.format(n=n) for n in levels]
        finest_generics = {
            "code": code_columns[-1],
            "name": schema.name_field.format(n=finest),
        }
        dissolve_stage.main(
            conn,
            table_in,
            dissolved,
            group_by=[code_columns[-1]],
            target_schema=schema,
        )
    else:
        level_columns = detect_level_columns_or_single(conn, table_in)
        for n in levels:
            if not level_columns[n].group_by and len(level_columns) > 1:
                msg = f"no reliable group-by column detected for level {n}"
                raise ValueError(msg)
        code_columns = [
            level_columns[n].group_by[0] if level_columns[n].group_by else None
            for n in levels
        ]
        finest_group_by = level_columns[finest].group_by
        if finest_group_by:
            verify_functional_cluster(conn, table_in, code_columns[-1], finest_group_by)
        dissolve_stage.main(conn, table_in, dissolved, group_by=finest_group_by)
        families = group_families_by_level(conn, table_in, level_columns)
        renamed = level_family_names(families, finest, code_columns[-1])
        finest_generics = {renamed.get(col, col): col for col in finest_group_by}

    dissolved_columns = {
        r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
    }
    finest_generics = {
        generic: col
        for generic, col in finest_generics.items()
        if col in dissolved_columns
    }
    if not finest_generics:
        finest_generics = {"code": None}

    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_parts" AS
        SELECT fid, geom, {bbox_columns_sql("geom")}
        FROM (SELECT fid, UNNEST(ST_Dump(geom)).geom AS geom FROM "{dissolved}")
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_pairs" AS
        SELECT DISTINCT a.fid AS left_fid, b.fid AS right_fid
        FROM "{name}_02_parts" a JOIN "{name}_02_parts" b
          ON a.fid < b.fid
          AND a.xmin <= b.xmax AND a.xmax >= b.xmin
          AND a.ymin <= b.ymax AND a.ymax >= b.ymin
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_boundary" AS
        SELECT fid, ST_Boundary(geom) AS boundary FROM "{dissolved}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_boundary_parts" AS
        SELECT fid, geom, {bbox_columns_sql("geom")}
        FROM (
            SELECT fid, UNNEST(ST_Dump(boundary)).geom AS geom
            FROM "{name}_02_boundary"
        )
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_touching" AS
        SELECT p.left_fid, p.right_fid
        FROM "{name}_02_pairs" p
        JOIN "{dissolved}" wl ON wl.fid = p.left_fid
        JOIN "{dissolved}" wr ON wr.fid = p.right_fid
        WHERE ST_Touches(wl.geom, wr.geom)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_shared" AS
        WITH pieces AS (
            SELECT t.left_fid, t.right_fid,
                   ST_CollectionExtract(ST_Intersection(lp.geom, rp.geom), 2) AS geom
            FROM "{name}_02_touching" t
            JOIN "{name}_02_boundary_parts" lp ON lp.fid = t.left_fid
            JOIN "{name}_02_boundary_parts" rp ON rp.fid = t.right_fid
              AND lp.xmin <= rp.xmax AND lp.xmax >= rp.xmin
              AND lp.ymin <= rp.ymax AND lp.ymax >= rp.ymin
        ),
        merged AS (
            SELECT left_fid, right_fid, ST_LineMerge(ST_Union_Agg(geom)) AS geom
            FROM pieces
            WHERE NOT ST_IsEmpty(geom)
            GROUP BY left_fid, right_fid
        )
        SELECT left_fid, right_fid, geom FROM merged
        WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_shared_by_fid" AS
        SELECT fid, ST_Union_Agg(geom) AS geom
        FROM (
            SELECT left_fid AS fid, geom FROM "{name}_02_shared"
            UNION ALL
            SELECT right_fid AS fid, geom FROM "{name}_02_shared"
        )
        GROUP BY fid
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_exterior_raw" AS
        SELECT bp.fid,
               ST_LineMerge(ST_Difference(
                   bp.geom, COALESCE(s.geom, {_EMPTY_LINES})
               )) AS geom
        FROM "{name}_02_boundary_parts" bp
        LEFT JOIN "{name}_02_shared_by_fid" s ON s.fid = bp.fid
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_shared_atomic" AS
        SELECT left_fid, right_fid, geom
        FROM (
            SELECT left_fid, right_fid, UNNEST(ST_Dump(geom)).geom AS geom
            FROM "{name}_02_shared"
        )
        WHERE NOT ST_IsEmpty(geom)
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_exterior" AS
        SELECT fid AS left_fid, NULL::BIGINT AS right_fid, geom
        FROM (
            SELECT fid, UNNEST(ST_Dump(geom)).geom AS geom
            FROM "{name}_02_exterior_raw"
        )
        WHERE NOT ST_IsEmpty(geom)
    """)

    classification = _classification_sql(levels, code_columns)

    def _side_expr(table: str, col: str | None) -> str:
        return f'{table}."{col}"' if col else "NULL"

    a_cols = ", ".join(
        f'{{table}}."{col}" AS "a_{generic}"' if col else f'NULL AS "a_{generic}"'
        for generic, col in finest_generics.items()
    )
    shared_b_cols = ", ".join(
        f'{_side_expr("r", col)} AS "b_{generic}"'
        for generic, col in finest_generics.items()
    )
    exterior_b_cols = ", ".join(f'NULL AS "b_{generic}"' for generic in finest_generics)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT s.left_fid, s.right_fid,
               {a_cols.format(table="l")}, {shared_b_cols},
               {classification} AS "{depth_column}", s.geom
        FROM "{name}_02_shared_atomic" s
        JOIN "{dissolved}" l ON l.fid = s.left_fid
        JOIN "{dissolved}" r ON r.fid = s.right_fid

        UNION ALL BY NAME

        SELECT e.left_fid, e.right_fid,
               {a_cols.format(table="d")}, {exterior_b_cols},
               {min(levels) - 1} AS "{depth_column}", e.geom
        FROM "{name}_02_exterior" e
        JOIN "{dissolved}" d ON d.fid = e.left_fid
    """)

    _check_every_fid_present(conn, dissolved, f"{name}_02")

    # fid is a fresh row_number() per run, useless once code columns resolve
    # each side's real identity; only the codes are worth exporting.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT * EXCLUDE (left_fid, right_fid) FROM "{name}_02"
        ORDER BY left_fid, right_fid NULLS LAST, hash(geom)
    """)

    for table in [
        "parts",
        "pairs",
        "boundary",
        "boundary_parts",
        "touching",
        "shared",
        "shared_by_fid",
        "exterior_raw",
        "shared_atomic",
        "exterior",
    ]:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_{table}"')
