"""Resolves each level's own code column, structural or explicit."""

from dataclasses import dataclass

from duckdb import DuckDBPyConnection

from topo_tools.core.schema_map._level_columns import (
    detect_level_columns_or_single,
    level_like_columns,
    verify_functional_cluster,
)
from topo_tools.core.schema_map._levels import detect_levels
from topo_tools.core.schema_map._target_schema import resolve_explicit_target_schema


@dataclass(frozen=True)
class Level:
    """One level's code column, name column, and whether the input lacks the code."""

    code: str
    name: str | None = None
    seeded: bool = False


def _has_codes(
    conn: DuckDBPyConnection, table: str, code: str, columns: set[str]
) -> bool:
    """Return whether the code column exists with at least one non-blank value."""
    if code not in columns:
        return False
    return (
        conn.execute(
            f'SELECT bool_or(trim("{code}"::VARCHAR) <> \'\') FROM "{table}"'
        ).fetchone()[0]
        is True
    )


def _without_root(conn: DuckDBPyConnection, table: str, root_column: str) -> str:
    """Return a view of table without root_column; raise if it is missing or blank."""
    columns = {r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()}
    if root_column not in columns:
        msg = f"root code column {root_column!r} not found in {table}"
        raise ValueError(msg)
    (blanks,) = conn.execute(
        f'SELECT COUNT(*) FROM "{table}" '
        f"WHERE coalesce(trim(\"{root_column}\"::VARCHAR), '') = ''"
    ).fetchone()
    if blanks:
        msg = f"root code column {root_column!r} is blank on {blanks} row(s)"
        raise ValueError(msg)
    view = f"{table}_levels"
    conn.execute(
        f'CREATE OR REPLACE TEMP VIEW "{view}" AS '
        f'SELECT * EXCLUDE ("{root_column}") FROM "{table}"'
    )
    return view


def main(
    conn: DuckDBPyConnection,
    table: str,
    name_field: str | None,
    code_field: str | None,
    *,
    root_column: str | None = None,
) -> dict[int, Level]:
    """Return {level: Level}, structurally detected or explicit schema.

    root_column, when given, is left out of detection: it holds each row's root.
    """
    if root_column is not None:
        table = _without_root(conn, table, root_column)
    schema = resolve_explicit_target_schema(name_field, code_field)
    if schema is not None:
        columns = {r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()}
        levels = detect_levels(conn, table, schema, require_codes=False)
        result = {}
        # Every code starts with root_code, so a template always gets level 0.
        for n in sorted({0, *levels}):
            code, name = schema.code_field.format(n=n), schema.name_field.format(n=n)
            seeded = n >= 1 and not _has_codes(conn, table, code, columns)
            result[n] = Level(code, name if name in columns else None, seeded)
        return result

    level_columns = detect_level_columns_or_single(conn, table)
    # Relative depth, not admin0 convention: a truly constant coarsest
    # column is dropped before reaching here, so every level found is rankable.
    coded = [(n, cols) for n, cols in sorted(level_columns.items()) if cols.group_by]
    if not coded:
        msg = f"no admin hierarchy level detected in {table}"
        raise ValueError(msg)
    # A skipped level would corrupt every code below it, so never guess.
    if supplemental := level_like_columns(conn, table):
        msg = (
            f"{table}: {supplemental} group units like a level but were not "
            "detected as one; pass --code-field/--name-field explicitly"
        )
        raise ValueError(msg)
    missing = [n for n, cols in coded if not cols.has_code]
    if missing:
        msg = (
            f"no existing code column to overwrite for level(s) {missing} in "
            f"{table}; pass --code-field/--name-field explicitly"
        )
        raise ValueError(msg)

    result: dict[int, Level] = {}
    parent: str | None = None
    for n, (_, cols) in enumerate(coded, start=1):
        canonical = cols.group_by[0]
        verify_functional_cluster(
            conn, table, canonical, cols.group_by, parent_column=parent
        )
        result[n] = Level(canonical, cols.name_column)
        parent = canonical
    return result
