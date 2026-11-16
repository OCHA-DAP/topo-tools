"""Builds a deduplicated shared+exterior boundary line network, tagged by depth."""

from duckdb import DuckDBPyConnection

from topo_tools.core.admin_columns import generic_families
from topo_tools.core.constants import SNAP_TOLERANCE
from topo_tools.core.dissolve import _02_dissolve as dissolve_stage
from topo_tools.core.duckdb_utils import bbox_columns_sql
from topo_tools.core.package_lines._constants import TILE_DEG, TILE_MARGIN_DEG
from topo_tools.core.schema_map._identity_names import generic_names_by_family
from topo_tools.core.schema_map._level_columns import (
    detect_levels_with_root,
    fallback_families,
    group_families_by_level,
    level_code_columns,
    verify_functional_cluster,
)
from topo_tools.core.schema_map._target_schema import TargetSchema


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
) -> int:
    """Return the coarsest level if it has one unit (its outline), else one less."""
    code = code_columns[0]
    if code is None:
        return min(levels)
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
) -> tuple[list, dict[str, dict[int, str]], int | None]:
    """Dissolve at the detected finest level; return codes, families, root level."""
    level_columns = detect_levels_with_root(conn, table_in)
    for n in levels:
        cols = level_columns[n]
        if not cols.group_by and len(level_columns) > 1 and not cols.is_root:
            msg = f"no reliable group-by column detected for level {n}"
            raise ValueError(msg)
    families = group_families_by_level(
        conn, table_in, level_columns
    ) or fallback_families(level_columns, levels)
    code_columns = level_code_columns(level_columns, families, levels)
    finest_group_by = level_columns[max(levels)].group_by
    if finest_group_by:
        verify_functional_cluster(conn, table_in, code_columns[-1], finest_group_by)
    dissolve_stage.main(conn, table_in, dissolved, group_by=finest_group_by)
    families = generic_names_by_family(families, levels, code_columns)
    root = next((n for n, c in level_columns.items() if c.is_root), None)
    return code_columns, families, root


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
    root_level = None
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
        code_columns, families, root_level = _auto_detect(
            conn, table_in, dissolved, levels
        )

    dissolved_columns = {
        r[0] for r in conn.execute(f'DESCRIBE "{dissolved}"').fetchall()
    }
    outer_depth = _outer_depth(conn, table_in, levels, code_columns)
    labels = _labels(families, levels, code_columns, dissolved_columns, outer_depth)

    _noded_lines(conn, name, dissolved)

    # A whole-table-constant root never separates two units; a NULL would.
    classification = _classification_sql(
        levels,
        [
            None if n == root_level else c
            for n, c in zip(levels, code_columns, strict=True)
        ],
    )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_rows" AS
        SELECT s.left_fid, s.right_fid,
               CASE WHEN s.right_fid IS NULL THEN {outer_depth}
               ELSE {classification} END AS depth, s.geom
        FROM "{name}_02_lines" s
        JOIN "{dissolved}" l ON l.fid = s.left_fid
        LEFT JOIN "{dissolved}" r ON r.fid = s.right_fid
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

    for table in ["lines", "rows"]:
        conn.execute(f'DROP TABLE IF EXISTS "{name}_02_{table}"')


def _tile_range(low: str, high: str) -> str:
    first, last = (f"floor({v} / {TILE_DEG})::BIGINT" for v in (low, high))
    return f"range({first}, {last} + 1)"


def _envelope(margin: float) -> str:
    return (
        f"ST_MakeEnvelope(tx * {TILE_DEG} - {margin}, ty * {TILE_DEG} - {margin}, "
        f"(tx + 1) * {TILE_DEG} + {margin}, (ty + 1) * {TILE_DEG} + {margin})"
    )


def _clip_to_tile(margin: float) -> str:
    """Clip geom to its tile, skipping the clip for a geom already inside it."""
    inside = " AND ".join(
        [
            f"xmin >= tx * {TILE_DEG} - {margin}",
            f"xmax <= (tx + 1) * {TILE_DEG} + {margin}",
            f"ymin >= ty * {TILE_DEG} - {margin}",
            f"ymax <= (ty + 1) * {TILE_DEG} + {margin}",
        ]
    )
    clip = f"ST_CollectionExtract(ST_Intersection(geom, {_envelope(margin)}), 2)"
    return f"CASE WHEN {inside} THEN geom ELSE {clip} END"


def _noded_lines(conn: DuckDBPyConnection, name: str, dissolved: str) -> None:
    """Node every unit boundary per grid tile, label each edge by its one or two units.

    Writes `{name}_02_lines` (left_fid, right_fid NULL for an exterior edge, geom).
    """
    tol = SNAP_TOLERANCE * 10
    margin = TILE_MARGIN_DEG
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp1" AS
        SELECT fid, ST_Boundary(geom) AS geom, {bbox_columns_sql("geom")}
        FROM "{dissolved}"
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp2" AS
        SELECT row_number() OVER () AS bid, fid, tx, ty, geom,
               {bbox_columns_sql("geom")}
        FROM (
            SELECT fid, tx, ty, {_clip_to_tile(margin)} AS geom
            FROM "{name}_02_tmp1",
                 LATERAL (SELECT UNNEST({
        _tile_range(f"(xmin - {margin})", f"(xmax + {margin})")
    }) AS tx),
                 LATERAL (SELECT UNNEST({
        _tile_range(f"(ymin - {margin})", f"(ymax + {margin})")
    }) AS ty)
        )
        WHERE NOT ST_IsEmpty(geom)
    """)
    conn.execute(f'DROP TABLE "{name}_02_tmp1"')
    # Computing the edge columns in the noding query itself makes it about 3x slower.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
        SELECT tx, ty, ST_Normalize(geom) AS geom
        FROM (
            SELECT tx, ty,
                   UNNEST(ST_Dump(ST_LineMerge(ST_Union_Agg(geom)))).geom AS geom
            FROM "{name}_02_tmp2" GROUP BY tx, ty
        )
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp3" AS
        SELECT row_number() OVER (
                   ORDER BY tx, ty, ST_XMin(geom), ST_YMin(geom), ST_AsWKB(geom)
               ) AS eid, tx, ty, geom,
               ST_LineInterpolatePoint(geom, 0.5) AS mid,
               {bbox_columns_sql("geom")}
        FROM "{name}_02_tmp3"
    """)
    # Pairing on bbox columns alone first keeps geometry out of the join's hash table.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
        SELECT e.eid, b.bid
        FROM (SELECT eid, tx, ty, ST_X(mid) AS mx, ST_Y(mid) AS my
              FROM "{name}_02_tmp3") e
        JOIN (SELECT bid, tx, ty, xmin, xmax, ymin, ymax FROM "{name}_02_tmp2") b
          ON b.tx = e.tx AND b.ty = e.ty
          AND e.mx BETWEEN b.xmin - {tol} AND b.xmax + {tol}
          AND e.my BETWEEN b.ymin - {tol} AND b.ymax + {tol}
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp4" AS
        WITH near AS (
            SELECT c.eid, b.fid, ST_Distance(b.geom, e.mid) AS dist
            FROM "{name}_02_tmp4" c
            JOIN (SELECT eid, mid FROM "{name}_02_tmp3") e USING (eid)
            JOIN (SELECT bid, fid, geom FROM "{name}_02_tmp2") b USING (bid)
            WHERE ST_DWithin(b.geom, e.mid, {tol})
        ),
        ranked AS (
            SELECT eid, fid, row_number() OVER (
                PARTITION BY eid ORDER BY dist, fid
            ) AS rank
            FROM near
        )
        SELECT eid, min(fid) AS left_fid,
               CASE WHEN count(*) = 2 THEN max(fid) END AS right_fid
        FROM ranked WHERE rank <= 2 GROUP BY eid
    """)
    conn.execute(f'DROP TABLE "{name}_02_tmp2"')
    # Each side's vertices go into the other's copy of a shared border.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp5" AS
        WITH labelled AS (
            SELECT l.eid, l.left_fid, coalesce(l.right_fid, -1) AS right_key,
                   e.tx, e.ty, e.geom, e.xmin, e.xmax, e.ymin, e.ymax
            FROM "{name}_02_tmp4" l JOIN "{name}_02_tmp3" e USING (eid)
        )
        SELECT a.eid,
               ST_Snap(any_value(a.geom),
                       ST_Collect(list_append(
                           list(b.geom ORDER BY b.eid), any_value(a.geom)
                       )),
                       {tol}) AS geom
        FROM labelled a JOIN labelled b ON a.tx = b.tx AND a.ty = b.ty
          AND a.left_fid = b.left_fid AND a.right_key = b.right_key
          AND a.eid <> b.eid
          AND a.xmin <= b.xmax + {tol} AND a.xmax >= b.xmin - {tol}
          AND a.ymin <= b.ymax + {tol} AND a.ymax >= b.ymin - {tol}
        GROUP BY a.eid
    """)
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_tmp6" AS
        SELECT row_number() OVER (
                   ORDER BY left_fid, right_fid, ST_AsWKB(geom)
               ) AS pid, left_fid, right_fid,
               coalesce(right_fid, -1) AS right_key, geom
        FROM (
            SELECT l.left_fid, l.right_fid, {_clip_to_tile(0)} AS geom
            FROM (
                SELECT e.eid, e.tx, e.ty, e.xmin, e.xmax, e.ymin, e.ymax,
                       coalesce(s.geom, e.geom) AS geom
                FROM "{name}_02_tmp3" e LEFT JOIN "{name}_02_tmp5" s USING (eid)
            ) e
            JOIN "{name}_02_tmp4" l USING (eid)
        )
        WHERE NOT ST_IsEmpty(geom)
    """)
    for table in ["tmp3", "tmp4", "tmp5"]:
        conn.execute(f'DROP TABLE "{name}_02_{table}"')
    # Neighbouring tiles cut one line at points a rounding error apart.
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_02_lines" AS
        WITH ends AS (
            SELECT pid, left_fid, right_key, ST_X(pt) AS x, ST_Y(pt) AS y, pt
            FROM (
                SELECT pid, left_fid, right_key,
                       UNNEST([ST_StartPoint(geom), ST_EndPoint(geom)]) AS pt
                FROM "{name}_02_tmp6"
            )
        ),
        moves AS (
            SELECT a.pid, ST_Collect(list(b.pt ORDER BY b.x, b.y)) AS targets
            FROM ends a JOIN ends b ON a.left_fid = b.left_fid
              AND a.right_key = b.right_key AND a.pid <> b.pid
              AND abs(a.x - b.x) <= {tol} AND abs(a.y - b.y) <= {tol}
              AND (b.x < a.x OR (b.x = a.x AND b.y < a.y))
            GROUP BY a.pid
        ),
        snapped AS (
            SELECT p.pid, p.left_fid, p.right_fid, CASE WHEN m.pid IS NULL THEN p.geom
                ELSE ST_Snap(p.geom, m.targets, {tol}) END AS geom
            FROM "{name}_02_tmp6" p LEFT JOIN moves m USING (pid)
        )
        SELECT left_fid, right_fid, geom
        FROM (
            SELECT left_fid, right_fid,
                   UNNEST(ST_Dump(ST_LineMerge(
                       ST_Union_Agg(geom ORDER BY pid)
                   ))).geom AS geom
            FROM snapped GROUP BY left_fid, right_fid
        )
        WHERE NOT ST_IsEmpty(geom)
    """)
    conn.execute(f'DROP TABLE "{name}_02_tmp6"')
