# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb",
# ]
# ///
"""Write the admin0 name columns into the stage 5 name-checked file, in place.

Run from the workspace root after the final name-detect:

    uv run <skill-dir>/scripts/adm0.py <iso3> <version> \
        [--code-field adm{n}_code] [--name-field adm{n}_name]

Names come from data/m49.csv for the six UN languages, and from
metadata.json's `adm0_names` for any other language or an area not in M49.
"""

import argparse
import csv
import io
import json
import logging
import re
import sys
import urllib.request
from pathlib import Path

import duckdb

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_M49_LOCAL = Path(__file__).resolve().parent.parent / "data" / "m49.csv"
_M49_URL = (
    "https://raw.githubusercontent.com/OCHA-DAP/topo-tools/main/"
    "plugins/topo-tools/skills/cod-ab/data/m49.csv"
)
_UN_LANGUAGES = ("ar", "zh", "en", "fr", "ru", "es")
_COPY = (
    "FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 15, GEOPARQUET_VERSION 'V2'"
)


def m49_row(iso3: str) -> dict[str, str] | None:
    """Return the M49 row for iso3, or None for an area not in M49."""
    if _M49_LOCAL.exists():
        text = _M49_LOCAL.read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(_M49_URL, timeout=60) as response:
            text = response.read().decode("utf-8")
    rows = csv.DictReader(io.StringIO(text))
    return next((r for r in rows if r["iso3"] == iso3.upper()), None)


def named_file(working: Path, iso3: str) -> Path:
    """Return the one {iso3}_admin{n}.parquet in 05_names/."""
    folder = working / "05_names"
    pattern = rf"{re.escape(iso3)}_admin\d+\.parquet"
    files = [p for p in folder.glob("*.parquet") if re.fullmatch(pattern, p.name)]
    if len(files) != 1:
        sys.exit(f"expected one {iso3}_admin{{n}}.parquet in {folder}, got {files}")
    return files[0]


def adm0_names(
    languages: list[str], m49: dict[str, str] | None, extra: dict[str, str]
) -> list[str]:
    """One admin0 name per language: M49 for UN languages, else metadata's own."""
    names = []
    for lang in languages:
        if m49 is not None and lang in _UN_LANGUAGES:
            if lang in extra:
                sys.exit(f"adm0_names[{lang!r}] is set, but M49 names {lang!r}")
            names.append(m49[f"name_{lang}"])
        elif extra.get(lang, "").strip():
            names.append(extra[lang].strip())
        else:
            sys.exit(f"no admin0 name for {lang!r}: add it to adm0_names")
    return names


def main() -> None:
    """Fill or add every admin0 name column, exiting non-zero on any mismatch."""
    parser = argparse.ArgumentParser(description="Write admin0 names from M49.")
    parser.add_argument("iso3", help="Country ISO3 code (e.g. syr)")
    parser.add_argument("version", help="Dataset version (e.g. v01)")
    parser.add_argument("--code-field", default="adm{n}_code")
    parser.add_argument("--name-field", default="adm{n}_name")
    args = parser.parse_args()
    iso3 = args.iso3.lower()
    working = Path("02_working") / iso3 / args.version
    record = json.loads((working / "metadata.json").read_text(encoding="utf-8"))
    path = named_file(working, iso3)

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    columns = [r[0] for r in conn.execute(f"DESCRIBE FROM '{path}'").fetchall()]
    name1 = args.name_field.format(n=1)
    level1_names = [c for c in columns if re.fullmatch(rf"{re.escape(name1)}\d*", c)]
    languages = record.get("name_languages", [])
    if len(languages) != len(level1_names):
        sys.exit(
            f"name_languages has {len(languages)} entries but {path.name} has "
            f"{len(level1_names)} level 1 name columns {level1_names}"
        )

    code0 = args.code_field.format(n=0)
    if code0 not in columns:
        sys.exit(f"{path.name} has no {code0!r}; run code-create with --code-field")
    m49 = m49_row(iso3)
    codes = {
        r[0]
        for r in conn.execute(f"SELECT DISTINCT \"{code0}\" FROM '{path}'").fetchall()
    }
    allowed = {iso3.upper(), record.get("country_iso2", "").upper()}
    if m49 is not None:
        allowed |= {m49["iso2"], m49["iso3"]}
    if len(codes) != 1 or not codes <= allowed - {""}:
        sys.exit(f"{code0} holds {sorted(codes, key=str)}, expected one of {allowed}")

    name0 = args.name_field.format(n=0)
    targets = [name0 + c.removeprefix(name1) for c in level1_names]
    values = dict(
        zip(
            targets,
            adm0_names(languages, m49, record.get("adm0_names", {})),
            strict=True,
        )
    )
    # Missing name columns go right before the code column, names before codes.
    select, params = [], []
    for column in columns:
        added = [t for t in targets if t not in columns] if column == code0 else []
        for name in [*added, column]:
            if name in values:
                select.append(f'?::VARCHAR AS "{name}"')
                params.append(values[name])
            else:
                select.append(f'"{name}"')
    tmp = path.with_suffix(".tmp.parquet")
    conn.execute(
        f"COPY (SELECT {', '.join(select)} FROM '{path}') TO '{tmp}' ({_COPY})",
        params,
    )
    tmp.replace(path)
    for target in targets:
        log.info("%s = %s", target, values[target])


if __name__ == "__main__":
    main()
