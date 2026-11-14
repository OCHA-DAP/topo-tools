"""Resolves each level's columns and naming family, or why levels can't resolve."""

import re
from collections import Counter
from dataclasses import dataclass

from duckdb import DuckDBPyConnection

from topo_tools.core.schema_map._level_columns import (
    detect_level_anchors,
    detect_level_columns,
    detect_root_level,
    root_anchor,
    supplemental_columns,
)
from topo_tools.core.schema_map._resolve_levels import resolve_levels
from topo_tools.core.schema_map._target_schema import resolve_explicit_target_schema

_COLUMNS = (
    "level INTEGER, column_name VARCHAR, family VARCHAR, raw_family VARCHAR, "
    "anchored BOOLEAN, is_code BOOLEAN, problem VARCHAR, reason VARCHAR"
)


@dataclass(frozen=True)
class _Row:
    level: int | None
    column_name: str | None = None
    family: str | None = None
    raw_family: str | None = None
    anchored: bool | None = None
    is_code: bool = False
    problem: str | None = None
    reason: str | None = None


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _strip(column: str, head: str, tail: str) -> str | None:
    """Return column minus head (or tail), unless that splits a run of digits."""
    if head and column.startswith(head):
        rest = column[len(head) :]
        if not (head[-1].isdigit() and rest[:1].isdigit()):
            return rest
    if tail and column.endswith(tail):
        rest = column[: len(column) - len(tail)]
        if not (tail[0].isdigit() and rest[-1:].isdigit()):
            return rest
    return None


def _column_row(
    level: int, column: str, anchor: tuple[str, str, str] | None, *, is_code: bool
) -> _Row:
    """Classify column against its level's (prefix, anchor, suffix) naming split."""
    if anchor is None:
        return _Row(level, column, is_code=is_code)
    prefix, digits, suffix = anchor
    raw = _strip(column, prefix + digits, digits + suffix)
    if raw is not None:
        return _Row(level, column, _norm(raw), raw, anchored=True, is_code=is_code)
    loose = _strip(_norm(column), _norm(prefix + digits), _norm(digits + suffix))
    family = loose if loose is not None else _norm(column)
    return _Row(level, column, family, anchored=False, is_code=is_code)


def _skipped(found: list[int]) -> list[_Row]:
    return [
        _Row(
            n,
            problem="level-skipped",
            reason=f"levels {found[0]} to {found[-1]} found, but no level {n} columns",
        )
        for n in range(found[0], found[-1] + 1)
        if n not in found
    ]


def _explicit_rows(columns: list[str], code_field: str) -> list[_Row]:
    prefix, _, suffix = code_field.partition("{n}")
    pattern = re.compile(
        rf"^{re.escape(_norm(prefix))}(\d+){re.escape(_norm(suffix))}$"
    )
    codes: dict[int, str] = {}
    for c in columns:
        if m := pattern.match(_norm(c)):
            n = int(m.group(1))
            if n not in codes or c == code_field.format(n=n):
                codes[n] = c
    if not codes:
        reason = f"no column matches --code-field {code_field!r}"
        return [_Row(None, problem="levels-undetected", reason=reason)]
    found = sorted(codes)
    rows = _skipped(found)
    for n in found:
        head, tail = prefix + str(n), str(n) + suffix
        rows.extend(
            _column_row(n, c, (prefix, str(n), suffix), is_code=c == codes[n])
            for c in columns
            if _strip(c, head, tail) is not None
            or _strip(_norm(c), _norm(head), "") is not None
        )
    return rows


def _template(codes: dict[int, str]) -> str | None:
    """Return the code column naming shared by every level, as "adm{n}_pcode"."""
    found = []
    for n, column in codes.items():
        spots = [m for m in re.finditer(r"\d+", column) if int(m.group()) == n]
        if len(spots) != 1:
            return None
        found.append(column[: spots[0].start()] + "{n}" + column[spots[0].end() :])
    if len({_norm(f) for f in found}) != 1:
        return None
    return Counter(found).most_common(1)[0][0]


def _structural_rows(conn: DuckDBPyConnection, table: str) -> list[_Row]:
    try:
        detected = detect_level_columns(conn, table)
    except ValueError as e:
        return [_Row(None, problem="levels-undetected", reason=str(e))]
    coded = {n: cols for n, cols in detected.items() if cols.group_by}
    if not coded:
        reason = f"no admin level with a code column detected in {table}"
        return [_Row(None, problem="levels-undetected", reason=reason)]
    rows = _skipped(sorted(coded))
    rows.extend(
        _Row(
            None,
            column,
            problem="supplemental-column",
            reason="set aside as a coarser grouping, not a level; if it is one, "
            "pass --name-field/--code-field",
        )
        for column in supplemental_columns(conn, table)
    )
    anchors = detect_level_anchors(conn, table)
    by_level = {
        n: [
            _column_row(n, c, anchors.get(n), is_code=False)
            for c in dict.fromkeys(cols.group_by[:1] + cols.identity_columns)
        ]
        for n, cols in coded.items()
    }
    columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
    assigned = {r.column_name for found in by_level.values() for r in found}
    for n, (prefix, digits, _) in anchors.items():
        if n in by_level:
            by_level[n].extend(
                _column_row(n, c, anchors[n], is_code=False)
                for c in columns
                if c not in assigned
                and _strip(_norm(c), _norm(prefix + digits), "") is not None
            )
    # group_by[0] can be a constant non-identity column, so the shared family wins.
    votes = Counter(
        r.family
        for n, found in by_level.items()
        for r in found
        if r.column_name == coded[n].group_by[0]
    )
    code_family = votes.most_common(1)[0][0]
    for n, found in sorted(by_level.items()):
        code = next(
            (r.column_name for r in found if r.anchored and r.family == code_family),
            coded[n].group_by[0],
        )
        rows.extend(
            _Row(**{**r.__dict__, "is_code": r.column_name == code})
            for r in found
            if r.column_name == code
            or r.column_name in coded[n].identity_columns
            or not r.anchored
        )
    codes = {r.level: r.column_name for r in rows if r.is_code}
    if (template := _template(codes)) is not None:
        # Detection can merge a nested level's columns into the next one, so
        # level membership follows the shared naming once one exists.
        problems = [r for r in rows if r.problem == "supplemental-column"]
        return problems + _explicit_rows(columns, template)
    return rows + _root_rows(conn, table, detected, rows)


def _root_rows(
    conn: DuckDBPyConnection, table: str, detected: dict, rows: list[_Row]
) -> list[_Row]:
    """Add a whole-table-constant root level above the coarsest, named like the rest."""
    coarsest = min(r.level for r in rows if r.is_code)
    root = detect_root_level(conn, table, detected)
    anchor = root_anchor(conn, table, detected)
    if root is None or anchor is None or coarsest < 1:
        return []
    level, root_columns = root
    code_family = next(r.family for r in rows if r.is_code and r.level == coarsest)
    found = [
        _column_row(level, c, anchor, is_code=False)
        for c in root_columns.identity_columns
    ]
    return [_Row(**{**r.__dict__, "is_code": r.family == code_family}) for r in found]


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    name_field: str | None = None,
    code_field: str | None = None,
) -> None:
    """Write `{name}_02`: one row per level column, plus any unresolved-level row."""
    table = f"{name}_01"
    schema = resolve_explicit_target_schema(name_field, code_field)
    if schema is None:
        rows = _structural_rows(conn, table)
    else:
        columns = [r[0] for r in conn.execute(f'DESCRIBE "{table}"').fetchall()]
        rows = _explicit_rows(columns, schema.code_field)
    # code-detect and name-detect resolve levels this way, so their refusal is ours.
    if not any(r.problem == "levels-undetected" for r in rows):
        try:
            resolve_levels(conn, table, name_field, code_field)
        except ValueError as e:
            rows.append(_Row(None, problem="levels-undetected", reason=str(e)))
    conn.execute(f'CREATE OR REPLACE TABLE "{name}_02" ({_COLUMNS})')
    fields = list(_Row.__dataclass_fields__)
    if rows:
        conn.executemany(
            f'INSERT INTO "{name}_02" ({", ".join(fields)}) '
            f"VALUES ({', '.join('?' for _ in fields)})",
            [[getattr(r, f) for f in fields] for r in rows],
        )
