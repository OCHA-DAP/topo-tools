"""Builds a deduplicated shared+exterior boundary line network, tagged by depth."""

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import generic_families
from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.schema_map._identity_names import generic_names_by_family
from topo_tools.core.schema_map._level_columns import (
    detect_levels_with_root,
    fallback_families,
    group_families_by_level,
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
        if col is not None
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


def _outer_depth(
    conn: DuckDBPyConnection,
    table: str,
    levels: list[int],
    code_columns: list,
    *,
    constant_root: bool,
) -> int:
    """Return the coarsest level if it has one unit (its outline), else one less."""
    code = code_columns[0]
    if code is None:
        return min(levels) if constant_root else min(levels) - 1
    units = conn.execute(f'SELECT COUNT(DISTINCT "{code}") FROM "{table}"').fetchall()
    return min(levels) if units[0][0] <= 1 else min(levels) - 1


def _labels(
    families: dict[str, dict[int, str]],
    levels: list[int],
    code_columns: list,
    dissolved_columns: set[str],
    outer_depth: int,
) -> dict[str, dict[int, str]]:
    """Each label's source column per row depth; outer edges use the coarsest."""
    label_levels = {lvl: lvl for lvl in levels} | {outer_depth: min(levels)}
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


def _auto_detect(
    conn: DuckDBPyConnection, table_in: str, dissolved: str, levels: list[int]
) -> tuple[list, dict[str, dict[int, str]], bool]:
    """Dissolve at the detected finest level; return codes, families, constant root."""
    level_columns = detect_levels_with_root(conn, table_in)
    constant_root = 0 in level_columns and not level_columns[0].group_by
    for n in levels:
        is_root = n == 0 and constant_root
        if not level_columns[n].group_by and len(level_columns) > 1 and not is_root:
            msg = f"no reliable group-by column detected for level {n}"
            raise ValueError(msg)
    code_columns = [
        level_columns[n].group_by[0] if level_columns[n].group_by else None
        for n in levels
    ]
    finest_group_by = level_columns[max(levels)].group_by
    if finest_group_by:
        verify_functional_cluster(conn, table_in, code_columns[-1], finest_group_by)
    dissolve_stage.main(conn, table_in, dissolved, group_by=finest_group_by)
    families = generic_names_by_family(
        group_families_by_level(conn, table_in, level_columns)
        or fallback_families(level_columns, levels),
        levels,
        code_columns,
    )
    return code_columns, families, constant_root


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

    if min_level is not None and min_level > max(levels):
        msg = f"no admin level at or above min_level {min_level}"
        raise ValueError(msg)

    table_in = f"{name}_01"
    constant_root = False
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
        code_columns, families, constant_root = _auto_detect(
            conn, table_in, dissolved, levels
        )

    dissolved_columns = {
        r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
    }
    outer_depth = _outer_depth(
        conn, table_in, levels, code_columns, constant_root=constant_root
    )
    labels = _labels(families, levels, code_columns, dissolved_columns, outer_depth)

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
        SELECT left_fid, right_fid, {outer_depth} AS depth, geom
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
