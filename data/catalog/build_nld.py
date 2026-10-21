"""Build the hdx/topo-tools NLD layers: CBS buurten/wijken/gemeenten 2021-2025 from the PDOK GeoPackages."""

import argparse
import json
import re
import subprocess
import urllib.request
from itertools import pairwise
from pathlib import Path

import duckdb
from outputs import read_state, write_parquet, write_state

from topo_tools.api.topo_detect import detect

PDOK = "https://service.pdok.nl/cbs/wijkenbuurten"
YEARS = (2021, 2022, 2023, 2024, 2025)
LEVELS = ("buurten", "wijken", "gemeenten")
CODE = {"buurten": "buurtcode", "wijken": "wijkcode", "gemeenten": "gemeentecode"}
ADMIN = {"buurten": "nld_admin4", "wijken": "nld_admin3", "gemeenten": "nld_admin2"}
COLUMNS = {
    "buurten": [
        "buurtcode",
        "buurtnaam",
        "wijkcode",
        "gemeentecode",
        "gemeentenaam",
        "water",
        "jaar",
    ],
    "wijken": ["wijkcode", "wijknaam", "gemeentecode", "gemeentenaam", "water", "jaar"],
    "gemeenten": ["gemeentecode", "gemeentenaam", "water", "jaar"],
}
KNOWN_GEMEENTEN = {"Voorne aan Zee": range(2023, 2026), "Weesp": range(2021, 2023)}


def get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
        return response.read().decode()


def dataset_feed(year: int) -> str:
    index = get(f"{PDOK}/{year}/atom/index.xml")
    return get(re.search(r'<link href="([^"]+\.xml)" rel="alternate"', index).group(1))


def gpkg_link(year: int) -> tuple[str, int]:
    link = re.search(
        r'<link href="([^"]+\.gpkg)"[^>]*length="(\d+)"', dataset_feed(year)
    )
    return link.group(1), int(link.group(2))


def fetch(year: int, link: tuple[str, int], cache: Path, *, refetch: bool) -> Path:
    url, length = link
    path = cache / f"wijkenbuurten_{year}.gpkg"
    if path.exists() and path.stat().st_size == length and not refetch:
        return path
    partial = path.with_suffix(".partial.gpkg")
    subprocess.run(
        [
            "curl",
            "-fsSL",
            "--retry",
            "5",
            "--retry-delay",
            "10",
            "-o",
            str(partial),
            url,
        ],
        check=True,
    )
    if (size := partial.stat().st_size) != length:
        msg = f"{url}: downloaded {size} bytes, Atom feed reports {length}"
        raise SystemExit(msg)
    partial.rename(path)
    return path


def select_sql(level: str, gpkg: Path) -> str:
    return f"SELECT geom AS geometry, {', '.join(COLUMNS[level])} FROM ST_Read('{gpkg}', layer='{level}')"


def validate(con: duckdb.DuckDBPyConnection, year: int) -> list[str]:
    errors = []
    for level in LEVELS:
        table, code = f"{level}_{year}", CODE[level]
        bad = con.execute(
            f"SELECT count(*) FILTER (WHERE {code} IS NULL), count(*) - count(DISTINCT ({code}, water)), "
            f"count(*) FILTER (WHERE jaar <> {year}) FROM {table}"
        ).fetchone()
        if any(bad):
            errors.append(
                f"{table}: {bad[0]} null codes, {bad[1]} duplicate (code, water) pairs, {bad[2]} rows with another jaar"
            )
    for child, parent, key in (
        ("buurten", "wijken", "wijkcode"),
        ("wijken", "gemeenten", "gemeentecode"),
    ):
        orphans = con.execute(
            f"SELECT count(*) FROM {child}_{year} WHERE {key} NOT IN (SELECT {key} FROM {parent}_{year})"
        ).fetchone()[0]
        if orphans:
            errors.append(
                f"{child}_{year}: {orphans} rows with a {key} missing from {parent}_{year}"
            )
    for name, years in KNOWN_GEMEENTEN.items():
        present = con.execute(
            f"SELECT count(*) > 0 FROM gemeenten_{year} WHERE gemeentenaam = ?", [name]
        ).fetchone()[0]
        if present != (year in years):
            errors.append(
                f"gemeenten_{year}: {name} {'present' if present else 'absent'}, expected otherwise"
            )
    return errors


def churn(con: duckdb.DuckDBPyConnection) -> dict:
    report = {}
    for old, new in pairwise(YEARS):
        for level in LEVELS:
            code = CODE[level]
            only_old, only_new = con.execute(
                f"""
                WITH o AS (SELECT {code} c FROM {level}_{old} WHERE water = 'NEE'),
                     n AS (SELECT {code} c FROM {level}_{new} WHERE water = 'NEE')
                SELECT (SELECT count(*) FROM o WHERE c NOT IN (SELECT c FROM n)),
                       (SELECT count(*) FROM n WHERE c NOT IN (SELECT c FROM o))
                """
            ).fetchone()
            report[f"{old}-{new}/{level}"] = {
                "only_old": only_old,
                "only_new": only_new,
            }
    return report


def detect_counts(outputs: list[Path], cache: Path) -> dict:
    report = {}
    for path in outputs:
        issues = cache / f"{path.parent.parent.name}_{path.stem}_issues.parquet"
        detect(path, issues, tmp_dir=cache / "tmp")
        rows = duckdb.sql(
            f"SELECT kind, count(*) FROM read_parquet('{issues}') GROUP BY kind"
        ).fetchall()
        report[f"{path.parent.parent.name}/{path.stem}"] = dict(rows)
    return report


def outputs(out: Path) -> list[Path]:
    return [
        out / "nld" / str(year) / ADMIN[level] / f"{ADMIN[level]}.parquet"
        for year in YEARS
        for level in LEVELS
    ]


def build(
    out: Path, cache: Path, *, refetch: bool = False, detect_issues: bool = False
) -> bool:
    """Write every year's layers, skipped when no PDOK GeoPackage changed. Returns True when built."""
    cache.mkdir(parents=True, exist_ok=True)
    state_path = out / ".state" / "fingerprints.json"
    state = read_state(state_path)
    links = {str(year): list(gpkg_link(year)) for year in YEARS}
    paths = outputs(out)
    if not refetch and state.get("nld") == links and all(p.exists() for p in paths):
        return False

    con = duckdb.connect()
    con.execute("LOAD spatial")
    errors = []
    for year in YEARS:
        gpkg = fetch(year, tuple(links[str(year)]), cache, refetch=refetch)
        for level in LEVELS:
            con.execute(f"CREATE TABLE {level}_{year} AS {select_sql(level, gpkg)}")
        errors += validate(con, year)
    if errors:
        raise SystemExit("Validation failed:\n" + "\n".join(errors))

    for year in YEARS:
        for level in LEVELS:
            write_parquet(
                con,
                f"SELECT * FROM {level}_{year} ORDER BY {CODE[level]}, water",
                out / "nld" / str(year) / ADMIN[level] / f"{ADMIN[level]}.parquet",
            )

    report = {"churn": churn(con)}
    if detect_issues:
        report["detect"] = detect_counts(paths, cache)
    (cache.parent / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    write_state(state_path, {**state, "nld": links})
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("portolan"), help="Catalog root directory"
    )
    parser.add_argument("--cache", type=Path, default=Path("tmp/catalog/cache"))
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument(
        "--detect", action="store_true", help="Add topo-detect counts to the report"
    )
    args = parser.parse_args()
    build(args.out, args.cache, refetch=args.refetch, detect_issues=args.detect)


if __name__ == "__main__":
    main()
