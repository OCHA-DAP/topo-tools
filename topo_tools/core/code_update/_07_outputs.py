"""Joins new codes back onto NEW's finest table; exports both geometry and changelog."""

from pathlib import Path

from duckdb import DuckDBPyConnection

from topo_tools.core.code import (
    TABLE_COPY_OPTS,
    CodeFormat,
    parent_prefix,
    write_root_code,
)
from topo_tools.core.code_update._02_levels import SideLevels
from topo_tools.core.code_update._06_assign import ChangeRow
from topo_tools.core.io import add_csv_bom, export_geometry_table


def _flag_overflow(changelog: list[ChangeRow], fmt: CodeFormat) -> None:
    """Mark 'new'-outcome rows as 'overflow' when their parent's child count spills."""
    by_parent: dict[tuple[int, str], list[ChangeRow]] = {}
    for row in changelog:
        if row.code_outcome not in ("new", "retained") or row.new_code is None:
            continue
        parent_code = parent_prefix(row.new_code, fmt)
        by_parent.setdefault((row.level, parent_code), []).append(row)
    for (level, parent_code), rows in by_parent.items():
        width = fmt.width(level)
        if width is None or len(rows) <= 10**width - 1:
            continue
        reason = (
            f"{len(rows)} children under {parent_code} at level {level} exceeds "
            f"{10**width - 1} at min_width={width}"
        )
        for row in rows:
            if row.code_outcome == "new":
                row.code_outcome = "overflow"
                row.reason = reason


def main(  # noqa: PLR0913
    conn: DuckDBPyConnection,
    name: str,
    finest_table: str,
    dest: Path,
    changelog_dest: Path,
    *,
    side_a: SideLevels,
    side_b: SideLevels,
    new_code_by_fid: dict[int, dict[int, str]],
    raw_val_by_fid: dict[int, dict[int, str]],
    changelog: list[ChangeRow],
    fmt: CodeFormat,
    predecessor_field: str = "predecessor_code",
    debug: bool = False,
) -> None:
    """Write each level's new code into finest_table under OLD's own column names.

    raw_val_by_fid supplies the fid -> raw NEW value lookup without needing dsl_{n}_b.
    """
    _flag_overflow(changelog, fmt)
    column_types = {
        r[0]: r[1] for r in conn.execute(f'DESCRIBE "{finest_table}"').fetchall()
    }
    existing = set(column_types)

    # Runs before the code-column loop below: if OLD's output column name and
    # NEW's raw column name coincide, that loop overwrites raw_col in place.
    finest = max(side_a.columns)
    predecessor_by_fid = {
        row.b_fid: row.predecessor_code
        for row in changelog
        if row.level == finest and row.b_fid is not None
    }
    if predecessor_field not in existing:
        conn.execute(
            f'ALTER TABLE "{finest_table}" ADD COLUMN "{predecessor_field}" VARCHAR'
        )
        existing.add(predecessor_field)
    pred_mapping = f"{name}_out_map_predecessor"
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP TABLE "{pred_mapping}" (
            raw_val VARCHAR, predecessor VARCHAR
        )
    """)
    conn.executemany(
        f'INSERT INTO "{pred_mapping}" VALUES (?, ?)',
        [
            (raw_val_by_fid[finest][b_fid], predecessor)
            for b_fid, predecessor in predecessor_by_fid.items()
        ],
    )
    finest_raw_col = side_b.columns[finest]
    conn.execute(f"""--sql
        UPDATE "{finest_table}" t
        SET "{predecessor_field}" = m.predecessor
        FROM "{pred_mapping}" m
        WHERE t."{finest_raw_col}" = m.raw_val
    """)
    conn.execute(f'DROP TABLE IF EXISTS "{pred_mapping}"')

    for n in sorted(side_a.columns):
        output_col, raw_col = side_a.columns[n], side_b.columns[n]
        if output_col not in existing:
            conn.execute(
                f'ALTER TABLE "{finest_table}" ADD COLUMN "{output_col}" VARCHAR'
            )
            existing.add(output_col)
        elif column_types.get(output_col, "VARCHAR") != "VARCHAR":
            # NEW can carry its own non-text column under OLD's code column name
            # (e.g. a numeric source id); codes are text, so retype it first.
            conn.execute(
                f'ALTER TABLE "{finest_table}" ALTER COLUMN "{output_col}" TYPE VARCHAR'
            )
            column_types[output_col] = "VARCHAR"

        mapping = f"{name}_out_map_{n}"
        conn.execute(f"""--sql
            CREATE OR REPLACE TEMP TABLE "{mapping}" (raw_val VARCHAR, new_code VARCHAR)
        """)
        conn.executemany(
            f'INSERT INTO "{mapping}" VALUES (?, ?)',
            [
                (raw_val_by_fid[n][fid], code)
                for fid, code in new_code_by_fid[n].items()
            ],
        )
        conn.execute(f"""--sql
            UPDATE "{finest_table}" t
            SET "{output_col}" = m.new_code
            FROM "{mapping}" m
            WHERE t."{raw_col}" = m.raw_val
        """)
        conn.execute(f'DROP TABLE IF EXISTS "{mapping}"')

    if side_a.schema is not None:
        present = {r[0] for r in conn.execute(f'DESCRIBE "{finest_table}"').fetchall()}
        root_name = side_a.schema.name_field.format(n=0)
        after = (
            (root_name,)
            if root_name in present
            else (side_a.columns[1], side_b.names.get(1))
        )
        root = side_a.schema.code_field.format(n=0)
        write_root_code(conn, finest_table, root, fmt.root_code, after)

    export_geometry_table(conn, finest_table, dest)

    changelog_table = f"{name}_changelog"
    conn.execute(f"""--sql
        CREATE OR REPLACE TEMP TABLE "{changelog_table}" (
            level INTEGER, old_code VARCHAR, old_name VARCHAR, new_code VARCHAR,
            new_name VARCHAR, relationship_class VARCHAR, cluster_id INTEGER,
            match_method VARCHAR, code_outcome VARCHAR, reason VARCHAR
        )
    """)
    conn.executemany(
        f'INSERT INTO "{changelog_table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            (
                row.level,
                row.old_code,
                row.old_name,
                row.new_code,
                row.new_name,
                row.relationship_class,
                row.cluster_id,
                row.match_method,
                row.code_outcome,
                row.reason,
            )
            for row in changelog
        ],
    )
    conn.execute(
        f"COPY (SELECT * FROM \"{changelog_table}\") TO '{changelog_dest}' "
        f"{TABLE_COPY_OPTS[changelog_dest.suffix]}"
    )
    add_csv_bom(changelog_dest)
    conn.execute(f'DROP TABLE IF EXISTS "{changelog_table}"')

    if not debug:
        conn.execute(f'DROP TABLE IF EXISTS "{finest_table}"')
