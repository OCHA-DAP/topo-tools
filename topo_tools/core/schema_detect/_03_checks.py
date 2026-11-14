"""Finds column-naming, column-set and hierarchy-nesting defects, one row each."""

from collections.abc import Callable
from functools import partial
from itertools import pairwise
from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.constants import is_noise_column, is_numeric_duckdb_type
from topo_tools.core.duckdb_utils import quote_identifier
from topo_tools.core.schema_map._identity_names import check_identity_names
from topo_tools.core.schema_map._target_schema import (
    TargetSchema,
    resolve_explicit_target_schema,
)

logger = getLogger(__name__)

_COLUMNS = (
    "kind VARCHAR, level INTEGER, column_name VARCHAR, code VARCHAR, reason VARCHAR"
)


def _problems(name: str, _parents: list) -> str:
    return f"""--sql
        SELECT problem, level, column_name, NULL, reason
        FROM "{name}_02" WHERE problem IS NOT NULL
    """


def _families(name: str) -> str:
    return f"""--sql
        SELECT * FROM "{name}_02" WHERE problem IS NULL AND family IS NOT NULL
    """


def _naming(name: str, _parents: list) -> str:
    # A tied family (two levels, two spellings) goes to the separator/case style
    # most of the layer's other columns use.
    return f"""--sql
        WITH s AS ({_families(name)}), styled AS (
            SELECT family, raw_family, regexp_replace(regexp_replace(
                regexp_replace(raw_family, '[0-9]+', '', 'g'),
                '[a-z]+', 'a', 'g'), '[A-Z]+', 'A', 'g') AS style
            FROM s WHERE anchored
        ), counts AS (
            SELECT family, raw_family, count(*) AS n,
                   bool_or(style = (SELECT mode(style) FROM styled)) AS fits
            FROM styled GROUP BY ALL
        ), usual AS (
            SELECT family, arg_max(raw_family, (n, fits, raw_family)) AS raw
            FROM counts GROUP BY family
        ), example AS (
            SELECT family, min(column_name) AS example
            FROM s JOIN usual USING (family)
            WHERE anchored AND raw_family = raw GROUP BY family
        )
        SELECT 'column-naming', level, column_name, NULL,
               CASE WHEN example IS NULL
                    THEN printf('does not follow level %d''s column naming', level)
                    ELSE printf('named unlike %s, its match at another level', example)
               END
        FROM s LEFT JOIN usual USING (family) LEFT JOIN example USING (family)
        WHERE NOT anchored OR raw_family IS DISTINCT FROM raw
    """


def _column_set(name: str, _parents: list) -> str:
    # A family at one level only (e.g. a reference name) is level-specific.
    return f"""--sql
        WITH s AS ({_families(name)}),
        levels AS (SELECT DISTINCT level FROM s),
        families AS (
            SELECT family, list(DISTINCT level) AS found, min(column_name) AS example
            FROM s GROUP BY family
        )
        SELECT 'column-set-mismatch', l.level, NULL, NULL,
               printf('no column like %s, which another level has', f.example)
        FROM families f CROSS JOIN levels l
        WHERE NOT list_contains(f.found, l.level)
          AND len(f.found) > 1
    """


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _pairs_sql(name: str, parents: list, select: Callable[[int, str, str], str]) -> str:
    unions = [select(level, child, parent) for level, child, parent in parents]
    if not unions:
        return f'SELECT NULL, NULL, NULL, NULL, NULL FROM "{name}_02" WHERE false'
    return " UNION ALL ".join(f"SELECT * FROM ({u})" for u in unions)


def _multiple_parents(name: str, parents: list) -> str:
    def select(level: int, child: str, parent: str) -> str:
        c, p = quote_identifier(child), quote_identifier(parent)
        return f"""--sql
            SELECT 'multiple-parents', {level}, {_literal(child)}, {c}::VARCHAR,
                   printf('under %d parents: %s', count(DISTINCT {p}),
                          string_agg(DISTINCT {p}::VARCHAR, ', '
                                     ORDER BY {p}::VARCHAR))
            FROM "{name}_01"
            WHERE {c} IS NOT NULL AND {p} IS NOT NULL
            GROUP BY {c} HAVING count(DISTINCT {p}) > 1
        """

    return _pairs_sql(name, parents, select)


def _orphan(name: str, parents: list) -> str:
    def select(level: int, child: str, parent: str) -> str:
        c, p = quote_identifier(child), quote_identifier(parent)
        return f"""--sql
            SELECT DISTINCT 'orphan-child', {level}, {_literal(child)}, {c}::VARCHAR,
                   {_literal(f"has no parent code in {parent}")}
            FROM "{name}_01" WHERE {c} IS NOT NULL AND {p} IS NULL
        """

    return _pairs_sql(name, parents, select)


def _varying(name: str, _parents: list, codes: list, metadata: list[str]) -> str:
    # Packaging drops a text column with several values in one coarsest unit.
    if not codes or not metadata:
        return f'SELECT NULL, NULL, NULL, NULL, NULL FROM "{name}_02" WHERE false'
    level, code = codes[0]
    c = quote_identifier(code)
    flags = ", ".join(
        f'count(DISTINCT {quote_identifier(col)}) > 1 AS "v{i}"'
        for i, col in enumerate(metadata)
    )
    keys = ", ".join(f"('v{i}', {_literal(col)})" for i, col in enumerate(metadata))
    names = ", ".join(f'"v{i}"' for i in range(len(metadata)))
    label = _literal(f" unit(s) of {code}")
    return f"""--sql
        SELECT 'varying-column', {level}, m.column_name, min(u.unit),
               'several values within ' || count(*) || {label}
        FROM (
            SELECT {c}::VARCHAR AS unit, {flags}
            FROM "{name}_01" WHERE {c} IS NOT NULL GROUP BY {c}
        ) UNPIVOT (varies FOR flag IN ({names})) u
        JOIN (VALUES {keys}) m(flag, column_name) USING (flag)
        WHERE u.varies
        GROUP BY m.column_name
    """


def _identity_names(
    name: str,
    _parents: list,
    conn: DuckDBPyConnection,
    schema: TargetSchema | None,
    codes: list,
) -> str:
    # Packaging names each identity family without its level; two must not share one.
    try:
        if codes:
            check_identity_names(conn, f"{name}_01", schema)
    except ValueError as e:
        return f"SELECT 'unusable-column-name', NULL, NULL, NULL, {_literal(str(e))}"
    return f'SELECT NULL, NULL, NULL, NULL, NULL FROM "{name}_02" WHERE false'


_CHECKS = {
    "problems": _problems,
    "column-naming": _naming,
    "column-set-mismatch": _column_set,
    "multiple-parents": _multiple_parents,
    "orphan-child": _orphan,
}


def _codes(conn: DuckDBPyConnection, name: str) -> list[tuple[int, str]]:
    """Each level's (level, code column), coarsest first."""
    return conn.execute(f"""--sql
        SELECT level, column_name FROM "{name}_02"
        WHERE is_code AND problem IS NULL ORDER BY level
    """).fetchall()


def _metadata(conn: DuckDBPyConnection, name: str) -> list[str]:
    """Every non-numeric column outside the level and supplemental columns."""
    known = {
        r[0]
        for r in conn.execute(
            f'SELECT column_name FROM "{name}_02" WHERE column_name IS NOT NULL'
        ).fetchall()
    }
    return [
        column
        for column, dtype, *_ in conn.execute(f'DESCRIBE "{name}_01"').fetchall()
        if column not in {*known, "fid", "geom"}
        and not is_noise_column(column)
        and not is_numeric_duckdb_type(dtype)
        and not dtype.startswith("GEOMETRY")
    ]


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    name_field: str | None = None,
    code_field: str | None = None,
    debug: bool = False,
) -> None:
    """Write `{name}_03`: every finding, one row per column, level or unit."""
    codes = _codes(conn, name)
    parents = [
        (level, child, parent) for (_, parent), (level, child) in pairwise(codes)
    ]
    checks = {
        **_CHECKS,
        "varying-column": partial(
            _varying, codes=codes, metadata=_metadata(conn, name)
        ),
        "unusable-column-name": partial(
            _identity_names,
            conn=conn,
            schema=resolve_explicit_target_schema(name_field, code_field),
            codes=codes,
        ),
    }
    tmps = []
    for i, (kind, build) in enumerate(checks.items(), start=1):
        tmp = f"{name}_03_tmp{i}"
        tmps.append(tmp)
        try:
            conn.execute(f'CREATE OR REPLACE TABLE "{tmp}" ({_COLUMNS})')
            conn.execute(f'INSERT INTO "{tmp}" {build(name, parents)}')
        except Exception as e:  # noqa: BLE001 (one failing check must not hide the rest)
            logger.warning("%s check failed (%s)", kind, e)
            conn.execute(f'CREATE OR REPLACE TABLE "{tmp}" ({_COLUMNS})')
            conn.execute(
                f'INSERT INTO "{tmp}" (kind, reason) VALUES (?, ?)',
                ["check-failed", f"{kind} check failed: {e}"],
            )
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_03" AS
        {" UNION ALL ".join(f'SELECT * FROM "{t}"' for t in tmps)}
    """)
    if not debug:
        for tmp in tmps:
            conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
