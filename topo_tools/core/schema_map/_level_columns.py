"""Groups a source table's columns by admin level, structurally, no naming assumed."""

import re
from dataclasses import dataclass

from duckdb import DuckDBPyConnection

from topo_tools.core.duckdb_utils import quote_identifier
from topo_tools.core.schema_map._02_map import (
    _WINNER_MAX_COLLAPSE_RATIO,
    _common_prefix,
    _has_geometry_column,
    _spatially_coherent,
    resolve_columns,
)
from topo_tools.core.schema_map._constants import CONFIDENCE_SUPPLEMENTAL
from topo_tools.core.schema_map._target_schema import DEFAULT_TARGET_SCHEMA

_MIN_LEVELS_TO_DIFF = 2
_LEVEL_DIGIT_RE = re.compile(r"\d+")
_MIN_PAIRS_FOR_LEAF_TOLERANCE = 2


@dataclass(frozen=True)
class LevelColumns:
    """One level's safe group-by key, and its own naming-anchored identity columns."""

    group_by: list[str]
    identity_columns: list[str]
    has_code: bool = True
    name_column: str | None = None


def _common_prefix_suffix(names: list[str]) -> tuple[str, str]:
    """Return the run every name shares at the start, then at the end."""
    prefix = _common_prefix(names)
    remainders = [n[len(prefix) :] for n in names]
    suffix = _common_prefix([r[::-1] for r in remainders])[::-1]
    return prefix, suffix


def _level_anchors(code_columns: dict[int, str]) -> dict[int, tuple[str, str, str]]:
    """Each level's own (prefix, anchor, suffix) split, diffed against every other."""
    if len(code_columns) < _MIN_LEVELS_TO_DIFF:
        return {}
    prefix, suffix = _common_prefix_suffix(list(code_columns.values()))
    anchors: dict[int, tuple[str, str, str]] = {}
    for level, name in code_columns.items():
        anchor = name[len(prefix) : len(name) - len(suffix)]
        if anchor:
            anchors[level] = (prefix, anchor, suffix)
    return anchors


def is_level_identity_column(name: str, prefix: str, anchor: str, suffix: str) -> bool:
    """Check name carries this level's own naming anchor, as a prefix or a suffix."""
    return name.startswith(prefix + anchor) or name.endswith(anchor + suffix)


def _strip_anchor(column: str, prefix: str, anchor: str, suffix: str) -> str:
    """Remove a level's own anchor from column, leaving its shared naming kind."""
    if column.startswith(prefix + anchor):
        return column[len(prefix) + len(anchor) :]
    return column[: len(column) - len(anchor) - len(suffix)]


def _max_cardinality_column(rows: dict, columns: list[str]) -> str:
    """Pick the cluster member least collapsed by NULLs, as the depth indicator."""
    return max(columns, key=lambda c: rows[c].unique_count or 0)


def _shared_length(names: list[str]) -> int:
    prefix, suffix = _common_prefix_suffix(names)
    return len(prefix) + len(suffix)


def _anchor_columns(
    group_by_by_level: dict[int, list[str]], codes: dict[int, str]
) -> dict[int, str]:
    """Each level's column to diff naming anchors on, sharing the most text."""
    if len(codes) < _MIN_LEVELS_TO_DIFF:
        return codes
    best, best_length = codes, _shared_length(list(codes.values()))
    reference_level = min(group_by_by_level, key=lambda k: len(group_by_by_level[k]))
    for reference in group_by_by_level[reference_level]:
        picked = {
            level: max(cols, key=lambda c, r=reference: _shared_length([r, c]))
            for level, cols in group_by_by_level.items()
        }
        picked[reference_level] = reference
        if (length := _shared_length(list(picked.values()))) > best_length:
            best, best_length = picked, length
    return best


def _resolve_levels(
    conn: DuckDBPyConnection, table: str
) -> tuple[dict, dict[int, str], dict[int, tuple[str, str, str]], dict[int, int]]:
    """Return rows, each level's code, naming anchor, and display number."""
    rows = resolve_columns(conn, table, DEFAULT_TARGET_SCHEMA)
    group_by_by_level: dict[int, list[str]] = {}
    for source, row in rows.items():
        if row.level is not None and row.role is not None:
            group_by_by_level.setdefault(row.level, []).append(source)
    codes = {
        level: _max_cardinality_column(rows, cols)
        for level, cols in group_by_by_level.items()
    }
    anchors = _level_anchors(_anchor_columns(group_by_by_level, codes))
    return rows, codes, anchors, _display_levels(codes, anchors)


def detect_level_anchors(
    conn: DuckDBPyConnection, table: str
) -> dict[int, tuple[str, str, str]]:
    """Each displayed level's (prefix, anchor, suffix) naming split."""
    _, _, anchors, display = _resolve_levels(conn, table)
    return {display.get(level, level): a for level, a in anchors.items()}


def _is_functionally_dependent(
    conn: DuckDBPyConnection, table: str, canonical_column: str, column: str
) -> bool:
    """Check column takes at most one non-null value per canonical_column group."""
    canonical_id = quote_identifier(canonical_column)
    other_id = quote_identifier(column)
    variants = conn.execute(f"""--sql
        SELECT MAX(variant_count) FROM (
            SELECT COUNT(DISTINCT {other_id}) FILTER (WHERE {other_id} IS NOT NULL)
                AS variant_count
            FROM {quote_identifier(table)}
            WHERE {canonical_id} IS NOT NULL
            GROUP BY {canonical_id}
        )
    """).fetchone()[0]
    return variants is None or variants <= 1


def _display_levels(
    codes: dict[int, str], anchors: dict[int, tuple[str, str, str]]
) -> dict[int, int]:
    """Renumber each level to its own naming-anchor digit, when consistent.

    Falls back to the structural level itself when no digit is found or unordered.
    """
    ordered = sorted(codes)
    digits = []
    for level in ordered:
        source = anchors[level][1] if level in anchors else codes[level]
        match = _LEVEL_DIGIT_RE.search(source)
        if match is None:
            return {level: level for level in codes}
        digits.append(int(match.group()))
    if digits != sorted(digits) or len(set(digits)) != len(digits):
        return {level: level for level in codes}
    return dict(zip(ordered, digits, strict=True))


def detect_level_columns_or_single(
    conn: DuckDBPyConnection, table: str
) -> dict[int, LevelColumns]:
    """detect_level_columns(), falling back to one ungrouped level for zero evidence."""
    try:
        return detect_level_columns(conn, table)
    except ValueError:
        return {0: LevelColumns(group_by=[], identity_columns=[], has_code=True)}


def detect_level_columns(
    conn: DuckDBPyConnection, table: str
) -> dict[int, LevelColumns]:
    """Every column structurally tied to each admin level, in no particular naming."""
    rows, codes, anchors, display = _resolve_levels(conn, table)

    all_columns_by_level: dict[int, list[str]] = {}
    group_by_by_level: dict[int, list[str]] = {}
    for source, row in rows.items():
        if row.level is None:
            continue
        all_columns_by_level.setdefault(row.level, []).append(source)
        if row.role is not None:
            group_by_by_level.setdefault(row.level, []).append(source)
    if not all_columns_by_level:
        msg = f"no admin hierarchy level detected in {table!r}"
        raise ValueError(msg)

    assigned = {c for cols in all_columns_by_level.values() for c in cols}
    table_columns = [
        r[0] for r in conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    ]

    root_level = min(all_columns_by_level)
    result: dict[int, LevelColumns] = {}
    for level in sorted(all_columns_by_level):
        group_by = list(group_by_by_level.get(level, []))
        # The root has no parent to embed, so its own shape is never diagnostic.
        has_code = level == root_level or any(
            rows[c].role == "code" for c in all_columns_by_level[level]
        )
        name_column = next(
            (c for c in all_columns_by_level[level] if rows[c].role == "name"), None
        )
        if level not in anchors:
            result[level] = LevelColumns(
                group_by=group_by,
                identity_columns=list(all_columns_by_level[level]),
                has_code=has_code,
                name_column=name_column,
            )
            continue

        prefix, anchor, suffix = anchors[level]
        completed = [
            c
            for c in table_columns
            if c not in assigned
            and is_level_identity_column(c, prefix, anchor, suffix)
            and _is_functionally_dependent(conn, table, codes[level], c)
        ]
        assigned.update(completed)
        identity = [
            c
            for c in all_columns_by_level[level]
            if is_level_identity_column(c, prefix, anchor, suffix)
        ] + completed
        result[level] = LevelColumns(
            group_by=group_by + completed,
            identity_columns=identity,
            has_code=has_code,
            name_column=name_column,
        )
    displayed = {display.get(level, level): v for level, v in result.items()}
    leaf = detect_leaf_level(conn, table, displayed)
    if leaf is not None:
        displayed[max(displayed) + 1] = leaf
    return displayed


def supplemental_columns(conn: DuckDBPyConnection, table: str) -> list[str]:
    """Columns detection set aside as a coarser grouping, never as a level."""
    rows = resolve_columns(conn, table, DEFAULT_TARGET_SCHEMA)
    return [c for c, r in rows.items() if r.note.startswith(CONFIDENCE_SUPPLEMENTAL)]


def level_like_columns(conn: DuckDBPyConnection, table: str) -> list[str]:
    """Supplemental columns whose groups cluster on the map, as a missed level would."""
    has_geom = _has_geometry_column(conn, table)
    return [
        c
        for c in supplemental_columns(conn, table)
        if not has_geom or _spatially_coherent(conn, table, c)
    ]


def detect_level_codes(conn: DuckDBPyConnection, table: str) -> dict[int, str]:
    """Each level's least-collapsed column, never a bracketed/supplemental one."""
    _, codes, _, display = _resolve_levels(conn, table)
    if not codes:
        msg = f"no admin hierarchy code column detected in {table!r}"
        raise ValueError(msg)
    return {display.get(level, level): column for level, column in codes.items()}


def root_anchor(
    conn: DuckDBPyConnection, table: str, level_columns: dict[int, LevelColumns]
) -> tuple[str, str, str] | None:
    """Find an unassigned family's (prefix, anchor, suffix), or None if ambiguous."""
    anchors = detect_level_anchors(conn, table)
    if not anchors:
        return None
    prefix, _, suffix = next(iter(anchors.values()))
    used_anchors = {a for _, a, _ in anchors.values()}

    table_columns = [
        r[0] for r in conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    ]
    assigned = {c for cols in level_columns.values() for c in cols.identity_columns}
    candidate_anchors = {
        c[len(prefix) : len(c) - len(suffix)]
        for c in table_columns
        if c not in assigned
        and c.startswith(prefix)
        and c.endswith(suffix)
        and len(c) > len(prefix) + len(suffix)
    } - used_anchors
    if len(candidate_anchors) != 1:
        return None
    return prefix, candidate_anchors.pop(), suffix


def detect_root_level(
    conn: DuckDBPyConnection, table: str, level_columns: dict[int, LevelColumns]
) -> LevelColumns | None:
    """Detect a coarser, whole-table-constant level one below the finest one.

    Completes the hierarchy via the naming style already established by
    the detected levels; `None` if no such family exists or isn't constant.
    """
    if not level_columns:
        return None
    root = root_anchor(conn, table, level_columns)
    if root is None:
        return None
    prefix, root_digits, suffix = root

    table_columns = [
        r[0] for r in conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    ]
    assigned = {c for cols in level_columns.values() for c in cols.identity_columns}
    candidates = [
        c
        for c in table_columns
        if c not in assigned
        and is_level_identity_column(c, prefix, root_digits, suffix)
    ]
    if not candidates:
        return None
    counts_sql = ", ".join(
        f"COUNT(DISTINCT {quote_identifier(c)}) "
        f"FILTER (WHERE {quote_identifier(c)} IS NOT NULL)"
        for c in candidates
    )
    counts = conn.execute(
        f"SELECT {counts_sql} FROM {quote_identifier(table)}"
    ).fetchone()
    root_columns = [c for c, n in zip(candidates, counts, strict=True) if n <= 1]
    if not root_columns:
        return None
    return LevelColumns(group_by=[], identity_columns=root_columns, has_code=True)


def _is_parent_scoped_unique(
    conn: DuckDBPyConnection, table: str, parent_column: str, column: str
) -> bool:
    """Check (parent_column, column) pairs are each distinct, tolerating noise."""
    parent_id = quote_identifier(parent_column)
    column_id = quote_identifier(column)
    populated, combos = conn.execute(f"""--sql
        SELECT COUNT(*), COUNT(DISTINCT ({parent_id}, {column_id}))
        FROM {quote_identifier(table)}
        WHERE {column_id} IS NOT NULL
    """).fetchone()
    if populated == 0:
        return False
    tolerance = 1 if populated > _MIN_PAIRS_FOR_LEAF_TOLERANCE else 0
    return populated - combos <= tolerance


def detect_leaf_level(
    conn: DuckDBPyConnection, table: str, level_columns: dict[int, LevelColumns]
) -> LevelColumns | None:
    """Detect a finer, name-only level one past the deepest one already found.

    Mirrors detect_root_level(), checked unique scoped to its own parent.
    """
    real_levels = {k: v for k, v in level_columns.items() if k != 0}
    if not real_levels:
        return None
    deepest = max(real_levels)
    parent_code = detect_level_codes(conn, table).get(deepest)
    if parent_code is None:
        return None

    families = group_families_by_level(conn, table, level_columns)
    table_columns = [
        r[0] for r in conn.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    ]
    assigned = {c for cols in level_columns.values() for c in cols.identity_columns}
    candidates: set[str] = set()
    for per_level in families.values():
        anchors = _level_anchors(per_level)
        if deepest not in anchors:
            continue
        prefix, anchor, suffix = anchors[deepest]
        match = _LEVEL_DIGIT_RE.search(anchor)
        if match is None:
            continue
        next_anchor = str(int(match.group()) + 1)
        candidates.update(
            c
            for c in table_columns
            if c not in assigned
            and is_level_identity_column(c, prefix, next_anchor, suffix)
        )
    valid = sorted(
        c for c in candidates if _is_parent_scoped_unique(conn, table, parent_code, c)
    )
    if not valid:
        return None
    return LevelColumns(
        group_by=valid, identity_columns=valid, has_code=False, name_column=valid[0]
    )


def group_families_by_level(
    conn: DuckDBPyConnection, table: str, level_columns: dict[int, LevelColumns]
) -> dict[str, dict[int, str]]:
    """Group every level's identity columns by shared naming kind, across levels."""
    anchors = detect_level_anchors(conn, table)
    if 0 in level_columns and 0 not in anchors:
        real_levels = {k: v for k, v in level_columns.items() if k != 0}
        root = root_anchor(conn, table, real_levels)
        if root is not None:
            anchors[0] = root
    families: dict[str, dict[int, str]] = {}
    for level, cols in level_columns.items():
        if level not in anchors:
            continue
        prefix, anchor, suffix = anchors[level]
        for column in cols.identity_columns:
            kind = _strip_anchor(column, prefix, anchor, suffix)
            families.setdefault(kind, {})[level] = column
    return families


def level_family_names(
    families: dict[str, dict[int, str]], level: int, code_column: str | None
) -> dict[str, str]:
    """Map this level's own identity columns to a name shared across levels."""
    names = {}
    for kind, per_level in families.items():
        if level not in per_level:
            continue
        generic = kind.strip("_") or (
            "code" if per_level[level] == code_column else "name"
        )
        names[per_level[level]] = generic
    return names


def verify_functional_cluster(
    conn: DuckDBPyConnection,
    table: str,
    canonical_column: str,
    cluster: list[str],
    parent_column: str | None = None,
) -> None:
    """Raise ValueError if any cluster member takes >1 non-null value per group.

    Per-column, not a raw tuple COUNT(DISTINCT): that treats an all-NULL row as a group.
    """
    for other in cluster:
        if other == canonical_column:
            continue
        if not _is_functionally_dependent(conn, table, canonical_column, other):
            msg = (
                f"{table}: grouping by {cluster} fragments {canonical_column!r}; "
                f"{other!r} takes more than one non-null value within a group"
            )
            raise ValueError(msg)
    if parent_column is not None:
        _verify_no_coarser_member(conn, table, canonical_column, cluster, parent_column)


def _verify_no_coarser_member(
    conn: DuckDBPyConnection,
    table: str,
    canonical_column: str,
    cluster: list[str],
    parent_column: str,
) -> None:
    """Raise if a member collapses the level's own units under each parent."""
    parent, canonical = (
        quote_identifier(parent_column),
        quote_identifier(canonical_column),
    )
    for other in cluster:
        if other == canonical_column:
            continue
        member = quote_identifier(other)
        # Only rows where the member has a value: an empty alternate-name column
        # would otherwise count as one value per parent.
        units, values = conn.execute(f"""--sql
            SELECT COUNT(DISTINCT ({parent}, {canonical})),
                   COUNT(DISTINCT ({parent}, {member}))
            FROM {quote_identifier(table)}
            WHERE {member} IS NOT NULL
        """).fetchone()
        if units and 1 - values / units > _WINNER_MAX_COLLAPSE_RATIO:
            msg = (
                f"{table}: {other!r} ({values} values under {parent_column!r}) "
                f"groups {canonical_column!r} ({units}), a coarser level merged "
                "into this one; pass --code-field/--name-field explicitly"
            )
            raise ValueError(msg)
