"""Resolves an include/exclude column filter pair against a table's real schema."""

from duckdb import DuckDBPyConnection


def resolve_column_selection(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    table: str,
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    always_exclude: tuple[str, ...] = (),
    always_include: tuple[str, ...] = (),
) -> list[str]:
    """Resolve include/exclude against `table`'s schema into a concrete list."""
    if include and exclude:
        msg = "include and exclude are mutually exclusive"
        raise ValueError(msg)
    if exclude:
        collisions = set(exclude) & set(always_include)
        if collisions:
            msg = (
                f"exclude collides with always-included column(s): {sorted(collisions)}"
            )
            raise ValueError(msg)

    columns = [row[0] for row in conn.execute(f'DESCRIBE "{table}"').fetchall()]

    if include:
        selected = list(include)
        for col in always_include:
            if col not in selected:
                selected.append(col)
    elif exclude:
        exclude_set = set(exclude)
        selected = [c for c in columns if c not in exclude_set]
    else:
        selected = list(columns)

    always_exclude_set = set(always_exclude)
    return [c for c in selected if c not in always_exclude_set]


def validate_merge_flags(  # noqa: PLR0913
    *,
    merge: bool,
    overlay_include: list[str] | None,
    overlay_exclude: list[str] | None,
    input_include: list[str] | None,
    input_exclude: list[str] | None,
    prefer: str | None,
    fill: dict[str, str] | None = None,
) -> None:
    """Raise on mutual-exclusion/require-merge violations among the merge flags."""
    if overlay_include and overlay_exclude:
        msg = "overlay_include and overlay_exclude are mutually exclusive"
        raise ValueError(msg)
    if input_include and input_exclude:
        msg = "input_include and input_exclude are mutually exclusive"
        raise ValueError(msg)
    if prefer is not None and prefer not in ("overlay", "input"):
        msg = "prefer must be 'overlay' or 'input'"
        raise ValueError(msg)
    narrowing_given = bool(
        overlay_include or overlay_exclude or input_include or input_exclude
    )
    if prefer and narrowing_given:
        msg = (
            "prefer is mutually exclusive with "
            "overlay_include/overlay_exclude/input_include/input_exclude"
        )
        raise ValueError(msg)
    if not merge and (narrowing_given or prefer or fill):
        msg = (
            "overlay_include/overlay_exclude/input_include/input_exclude/prefer/fill "
            "require merge=True"
        )
        raise ValueError(msg)


def resolve_merge_columns(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    *,
    merge: bool,
    overlay_include: list[str] | None,
    overlay_exclude: list[str] | None,
    input_include: list[str] | None,
    input_exclude: list[str] | None,
    prefer: str | None,
    input_table: str | None = None,
    fill: dict[str, str] | None = None,
) -> tuple[list[str] | None, list[str] | None]:
    """Resolve merge settings into concrete (overlay_columns, input_columns) lists."""
    if not merge:
        return None, None
    overlay_columns = resolve_column_selection(
        conn,
        f"{name}_overlay_01",
        include=overlay_include,
        exclude=overlay_exclude,
        always_exclude=("fid", "geom"),
    )
    input_columns = resolve_column_selection(
        conn,
        input_table or f"{name}_input_01",
        include=input_include,
        exclude=input_exclude,
        always_include=("fid", "geom", "source_file"),
    )
    if prefer == "overlay":
        input_columns = [c for c in input_columns if c not in set(overlay_columns)]
    elif prefer == "input":
        overlay_columns = [c for c in overlay_columns if c not in set(input_columns)]
    _check_fill(
        conn,
        fill,
        overlay_table=f"{name}_overlay_01",
        input_table=input_table or f"{name}_input_01",
        overlay_columns=overlay_columns,
        input_columns=input_columns,
    )
    return overlay_columns, input_columns


def _check_fill(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    fill: dict[str, str] | None,
    *,
    overlay_table: str,
    input_table: str,
    overlay_columns: list[str],
    input_columns: list[str],
) -> None:
    """Raise unless each fill pair maps a kept input column to an overlay column."""
    if not fill:
        return
    overlay_all = {r[0] for r in conn.execute(f'DESCRIBE "{overlay_table}"').fetchall()}
    input_types = {
        r[0]: r[1] for r in conn.execute(f'DESCRIBE "{input_table}"').fetchall()
    }
    for target, source in fill.items():
        if target not in input_columns or target in ("fid", "geom", "source_file"):
            msg = f"fill target {target!r} is not a kept input column"
            raise ValueError(msg)
        if target in overlay_columns:
            msg = f"fill target {target!r} is also a copied overlay column"
            raise ValueError(msg)
        if source not in overlay_all or source in ("fid", "geom"):
            msg = f"fill source {source!r} is not an overlay column"
            raise ValueError(msg)
        target_type = input_types[target]
        (bad,) = conn.execute(f"""--sql
            SELECT COUNT(*) FROM "{overlay_table}"
            WHERE "{source}" IS NOT NULL
              AND TRY_CAST("{source}" AS {target_type}) IS NULL
        """).fetchone()
        if bad:
            msg = (
                f"fill source {source!r} has {bad} value(s) that can't convert to "
                f"{target!r}'s type {target_type}"
            )
            raise ValueError(msg)


def union_input_schema(conn: DuckDBPyConnection, name: str, *, first: bool) -> None:
    """Add `{name}_input_01`'s columns to the empty `{name}_input_schema` table."""
    union = "" if first else f'SELECT * FROM "{name}_input_schema" UNION ALL BY NAME '
    conn.execute(f"""--sql
        CREATE OR REPLACE TABLE "{name}_input_schema" AS
        {union}SELECT * FROM "{name}_input_01" LIMIT 0
    """)
