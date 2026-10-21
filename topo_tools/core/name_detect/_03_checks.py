"""Runs every name check against `{name}_02`, one row per finding, per unit."""

from logging import getLogger

from duckdb import DuckDBPyConnection

from topo_tools.core.name_detect._constants import (
    CASE_LONG_WORD,
    CASE_SHORT_WORD,
    CODE_IN_NAME_MIN_LENGTH,
    CONFUSABLE_SCRIPTS,
    INVISIBLE_PATTERN,
    MIXED_CASE_SHARE,
    MOJIBAKE_PATTERN,
    ODD_SPACE_PATTERN,
    PLACEHOLDER_TOKENS,
    REPLACEMENT_CHARACTER,
    ROMAN_NUMERAL_PATTERN,
    SIBLING_FILLED_SHARE,
    STRONG_MOJIBAKE_PATTERN,
)
from topo_tools.core.name_detect._macros import create_macros

logger = getLogger(__name__)

_COLUMNS = (
    "kind VARCHAR, level INTEGER, name_column VARCHAR, code_a VARCHAR, "
    "name_a VARCHAR, code_b VARCHAR, name_b VARCHAR, suggested VARCHAR, "
    "reason VARCHAR"
)


def _coded(source: str) -> str:
    return f'(SELECT * FROM "{source}" WHERE code IS NOT NULL)'


def _named(source: str) -> str:
    return (
        f"(SELECT * FROM {_coded(source)} WHERE name IS NOT NULL AND trim(name) <> '')"
    )


def _per_name(
    kind: str, source: str, where: str, reason: str, suggested: str = "NULL"
) -> str:
    return f"""--sql
        SELECT '{kind}', level, name_column, code, name, NULL, NULL,
               {suggested}, {reason}
        FROM {_named(source)} WHERE {where}
    """


def _blank(source: str) -> str:
    # A sibling column (an alternate name) is checked only when mostly filled.
    return f"""--sql
        WITH filled AS (
            SELECT level, name_column,
                   avg((name IS NOT NULL AND trim(name) <> '')::INT) AS share
            FROM "{source}" GROUP BY ALL
        )
        SELECT 'blank-name', t.level, t.name_column, t.code, t.name, NULL, NULL,
               NULL, 'no name'
        FROM {_coded(source)} t JOIN filled f USING (level, name_column)
        WHERE (t.name IS NULL
               OR trim(regexp_replace(t.name, '\\s|{INVISIBLE_PATTERN}', '', 'g')) = '')
          AND (t.name_index = 0 OR f.share >= {SIBLING_FILLED_SHARE})
    """


def _placeholder(source: str) -> str:
    tokens = ", ".join(f"'{t}'" for t in PLACEHOLDER_TOKENS)
    return _per_name(
        "placeholder-name",
        source,
        f"lower(trim(name)) IN ({tokens}) OR lower(trim(name)) = lower(code) "
        "OR NOT regexp_matches(name, '[\\p{L}\\p{N}]')",
        "'a placeholder, not a name'",
    )


def _letters_in(column: str, scripts: tuple[str, ...]) -> str:
    """Return SQL testing that every letter in column belongs to one of scripts."""
    allowed = "".join(f"\\p{{{s}}}" for s in scripts)
    return f"NOT regexp_matches({column}, '[^\\P{{L}}{allowed}]')"


# The same units matched in several language columns are reported once.
_FIRST_COLUMN_ONLY = (
    "QUALIFY row_number() OVER (PARTITION BY level, codes ORDER BY name_index) = 1"
)


def _duplicate(source: str) -> str:
    return f"""--sql
        SELECT 'duplicate-name', level, name_column, codes[1], name, codes[2], name,
               NULL, printf('%d units under %s share this name: %s',
                            len(codes), coalesce(parent_code, 'the root'),
                            array_to_string(codes, ', '))
        FROM (
            SELECT level, name_column, name_index, parent_code, name,
                   list_sort(list(DISTINCT code)) AS codes
            FROM {_named(source)} GROUP BY ALL
        )
        WHERE len(codes) > 1
        {_FIRST_COLUMN_ONLY}
    """


def _squash(text: str) -> str:
    """Return SQL dropping separators between letters, other punctuation to a space."""
    other = "[^\\p{L}\\p{M}\\p{N}]+"
    between = f"([\\p{{L}}\\p{{M}}]){other}(\\p{{L}})"
    joined = f"regexp_replace({text}, '{between}', '\\1\\2', 'g')"
    return f"trim(regexp_replace({joined}, '{other}', ' ', 'g'))"


def _normalized_duplicate(source: str) -> str:
    # Accents are folded only in Latin names and only against an unaccented
    # spelling: elsewhere (Vietnamese tones, Burmese vowels) they change the name.
    strict = _squash("lower(nfc_normalize(name))")
    folded = _squash("lower(strip_accents(name))")
    latin = _letters_in("name", ("Latin",))
    return f"""--sql
        WITH n AS (
            SELECT *, {strict} AS strict,
                   CASE WHEN {latin} THEN {folded} ELSE {strict} END AS norm
            FROM {_named(source)}
        )
        SELECT 'normalized-duplicate-name', level, name_column,
               units[1].code, units[1].name,
               units[2].code, units[2].name, NULL,
               printf('%d units under %s have names that differ only in case, '
                      'accents or punctuation: %s', len(codes),
                      coalesce(parent_code, 'the root'), array_to_string(names, ' / '))
        FROM (
            SELECT level, name_column, name_index, parent_code,
                   list_sort(list(DISTINCT code)) AS codes,
                   list_sort(list(DISTINCT name)) AS names,
                   list_sort(list(DISTINCT {{'code': code, 'name': name}})) AS units,
                   count(DISTINCT strict) = 1 OR bool_or(name = strip_accents(name))
                       AS unaccented_match
            FROM n WHERE norm <> ''
            GROUP BY level, name_column, name_index, parent_code, norm
        )
        WHERE len(codes) > 1 AND len(names) > 1 AND unaccented_match
        {_FIRST_COLUMN_ONLY}
    """


def _encoding(source: str) -> str:
    return _per_name(
        "encoding-artifact",
        source,
        f"contains(name, '{REPLACEMENT_CHARACTER}') "
        f"OR regexp_matches(name, '{STRONG_MOJIBAKE_PATTERN}') "
        f"OR (regexp_matches(name, '{MOJIBAKE_PATTERN}') "
        "AND name_repair_mojibake(name) IS NOT NULL)",
        f"CASE WHEN contains(name, '{REPLACEMENT_CHARACTER}') "
        "THEN 'contains U+FFFD, characters lost when the file was read' "
        "WHEN name_repair_mojibake(name) IS NULL "
        "THEN 'looks like text read with the wrong encoding, no safe repair' "
        "ELSE 'text read with the wrong encoding' END",
        "name_repair_mojibake(name)",
    )


def _invisible(source: str) -> str:
    return _per_name(
        "invisible-character",
        source,
        f"regexp_matches(name, '{INVISIBLE_PATTERN}|{ODD_SPACE_PATTERN}')",
        "'contains invisible characters or non-standard spaces'",
        "name_clean(name)",
    )


def _unnormalized(source: str) -> str:
    return _per_name(
        "unnormalized-unicode",
        source,
        "length(nfc_normalize(name)) < length(name)",
        "'accents stored as separate characters (not NFC)'",
        "name_clean(name)",
    )


def _whitespace(source: str) -> str:
    return _per_name(
        "whitespace",
        source,
        "regexp_matches(name, '^ | $|  ')",
        "'leading, trailing or repeated spaces'",
        "name_clean(name)",
    )


def _case(source: str) -> str:
    # Only bicameral scripts: Georgian, for one, is stored as lower case letters.
    caps = f"""list_transform(list_filter(
        regexp_split_to_array(name, '[^\\p{{L}}]+'),
        lambda w: w = upper(w) AND w <> lower(w)
             AND NOT regexp_matches(w, '{ROMAN_NUMERAL_PATTERN}')),
        lambda w: length(w))"""
    return f"""--sql
        WITH c AS (
            SELECT *,
                   regexp_matches(name, '\\p{{Lu}}') AS has_upper,
                   regexp_matches(name, '\\p{{Ll}}') AS has_lower,
                   {caps} AS caps
            FROM {_named(source)} WHERE {_letters_in("name", CONFUSABLE_SCRIPTS)}
        ), o AS (
            -- An acronym is neither mixed case nor an outlier.
            SELECT *, has_upper <> has_lower
                      AND regexp_matches(name, '\\p{{L}}{{{CASE_SHORT_WORD + 1}}}')
                      AND (NOT has_upper
                           OR list_max(caps) >= {CASE_LONG_WORD}
                           OR len(list_filter(
                               caps, lambda n: n >= {CASE_SHORT_WORD})) >= 2)
                      AS outlier
            FROM c
        ), share AS (
            SELECT level, name_column, avg((NOT outlier)::INT) AS mixed
            FROM o WHERE (has_upper AND has_lower) OR outlier GROUP BY ALL
        )
        SELECT 'case-outlier', level, name_column, code, name, NULL, NULL, NULL,
               CASE WHEN has_upper THEN 'all capitals' ELSE 'all lower case' END
               || ' where the column is mixed case'
        FROM o JOIN share USING (level, name_column)
        WHERE share.mixed >= {MIXED_CASE_SHARE} AND outlier
    """


def _mixed_script(source: str) -> str:
    scripts = [f"regexp_matches(w, '\\p{{{s}}}')::INT" for s in CONFUSABLE_SCRIPTS]
    return f"""--sql
        SELECT DISTINCT 'mixed-script', level, name_column, code, name, NULL, NULL,
               NULL, 'a word mixes ' || '{"/".join(CONFUSABLE_SCRIPTS)}' || ' letters'
        FROM (
            SELECT *, unnest(regexp_split_to_array(name, '[^\\p{{L}}\\p{{M}}]+')) AS w
            FROM {_named(source)}
        )
        WHERE {" + ".join(scripts)} > 1
    """


def _code_in_name(source: str) -> str:
    def has(column: str) -> str:
        return (
            f"(length({column}) >= {CODE_IN_NAME_MIN_LENGTH} "
            f"AND regexp_matches({column}, '\\p{{L}}') "
            f"AND regexp_matches({column}, '\\p{{N}}') "
            f"AND contains(' ' || {_tokens('name')} || ' ', "
            f"' ' || {_tokens(column)} || ' '))"
        )

    return _per_name(
        "code-in-name",
        source,
        f"lower(trim(name)) <> lower(code) AND ({has('code')} OR {has('parent_code')})",
        "'the name contains a code'",
    )


def _tokens(text: str) -> str:
    return f"trim(regexp_replace(lower({text}), '[^\\p{{L}}\\p{{N}}]+', ' ', 'g'))"


_CHECKS = {
    "blank-name": _blank,
    "placeholder-name": _placeholder,
    "duplicate-name": _duplicate,
    "encoding-artifact": _encoding,
    "normalized-duplicate-name": _normalized_duplicate,
    "invisible-character": _invisible,
    "unnormalized-unicode": _unnormalized,
    "whitespace": _whitespace,
    "case-outlier": _case,
    "mixed-script": _mixed_script,
    "code-in-name": _code_in_name,
}


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Write `{name}_03`: every finding, one row per unit (or unit pair)."""
    source = f"{name}_02"
    create_macros(conn)
    tmps = []
    for i, (kind, build) in enumerate(_CHECKS.items(), start=1):
        tmp = f"{name}_03_tmp{i}"
        tmps.append(tmp)
        try:
            conn.execute(f'CREATE OR REPLACE TABLE "{tmp}" ({_COLUMNS})')
            conn.execute(f'INSERT INTO "{tmp}" {build(source)}')
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
    # One defect per unit: a blank name or an encoding error explains the rest.
    conn.execute(f"""--sql
        DELETE FROM "{name}_03" AS i USING "{name}_03" AS d
        WHERE i.code_a = d.code_a AND i.level = d.level
          AND i.name_column = d.name_column AND i.code_b IS NULL
          AND ((d.kind = 'blank-name' AND i.kind <> 'blank-name')
               OR (d.kind = 'encoding-artifact' AND i.kind = 'invisible-character'))
    """)
    if not debug:
        for tmp in tmps:
            conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
