"""Builds a deduplicated shared+exterior boundary line network, tagged by depth."""

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import generic_families
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


def _generic_names(
    families: dict[str, dict[int, str]], levels: list[int], code_columns: list
) -> dict[str, dict[int, str]]:
    """Rekey auto-detected families by the generic name package-points uses."""
    renamed = {}
    for kind, per_level in families.items():
        lvl = max(per_level)
        code = code_columns[levels.index(lvl)] if lvl in levels else None
        generic = level_family_names({kind: per_level}, lvl, code)[per_level[lvl]]
        renamed[generic] = per_level
    return renamed


def _labels(
    families: dict[str, dict[int, str]],
    levels: list[int],
    code_columns: list,
    dissolved_columns: set[str],
) -> dict[str, dict[int, str]]:
    """Each label's source column per row depth; outer edges use the coarsest."""
    label_levels = {lvl: lvl for lvl in levels} | {min(levels) - 1: min(levels)}
    if not families:
        families = {"code": dict(zip(levels, code_columns, strict=True))}
    labels = {
        generic: {
            depth: per_level[lvl]
            for depth, lvl in label_levels.items()
            if per_level.get(lvl) in dissolved_columns
        }
        for generic, per_level in families.items()
    }
    labels = {generic: by_depth for generic, by_depth in labels.items() if by_depth}
    return labels or {"code": {}}


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    levels: list[int],
    schema: TargetSchema | None,
    depth_column: str,
    *,
    min_level: int | None = None,
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
        columns = [r[0] for r in conn.execute(f'DESCRIBE "{table_in}"').fetchall()]
        families = generic_families(
            columns, levels, schema.name_field, schema.code_field
        )
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
        families = _generic_names(
            group_families_by_level(conn, table_in, level_columns),
            levels,
            code_columns,
        )

    dissolved_columns = {
        r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
    }
    labels = _labels(families, levels, code_columns, dissolved_columns)

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
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_rows" AS
        SELECT s.left_fid, s.right_fid, {classification} AS depth, s.geom
        FROM "{name}_02_shared_atomic" s
        JOIN "{dissolved}" l ON l.fid = s.left_fid
        JOIN "{dissolved}" r ON r.fid = s.right_fid
        UNION ALL
        SELECT left_fid, right_fid, {min(levels) - 1} AS depth, geom
        FROM "{name}_02_exterior"
    """)

    def _label(side: str, by_depth: dict[int, str]) -> str:
        cases = "".join(
            f'WHEN {depth} THEN {side}."{col}" ' for depth, col in by_depth.items()
        )
        return f"CASE s.depth {cases}END" if cases else "NULL"

    label_sql = ", ".join(
        f'{_label(side, by_depth)} AS "{side}_{generic}"'
        for side in ("a", "b")
        for generic, by_depth in labels.items()
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT s.left_fid, s.right_fid, {label_sql},
               s.depth AS "{depth_column}", s.geom
        FROM "{name}_02_rows" s
        JOIN "{dissolved}" a ON a.fid = s.left_fid
        LEFT JOIN "{dissolved}" b ON b.fid = s.right_fid
    """)

    _check_every_fid_present(conn, dissolved, f"{name}_02")

    # fid is a fresh row_number() per run, useless once code columns resolve
    # each side's real identity; only the codes are worth exporting.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02" AS
        SELECT * EXCLUDE (left_fid, right_fid) FROM "{name}_02"
        {f'WHERE "{depth_column}" >= {min_level}' if min_level is not None else ""}
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
        "rows",
    ]:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_{table}"')
