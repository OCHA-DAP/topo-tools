# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb",
# ]
# ///
"""Write 06_packaging/{iso3}_metadata.csv and .parquet in COD-AB metadata order.

Run from the workspace root: uv run <skill-dir>/scripts/metadata.py <iso3> <version>
"""

import argparse
import csv
import json
import logging
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import duckdb

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_MAX_LEVEL = 5
_COLUMNS = [
    "country_name",
    "country_iso2",
    "country_iso3",
    "version",
    "admin_level_full",
    "admin_level_max",
    *[f"admin_{n}_name" for n in range(1, _MAX_LEVEL + 1)],
    *[f"admin_{n}_count" for n in range(1, _MAX_LEVEL + 1)],
    "admin_notes",
    "date_source",
    "date_updated",
    "date_reviewed",
    "date_metadata",
    "date_valid_on",
    "date_valid_to",
    "update_frequency",
    "update_type",
    "source",
    "source_url",
    "contributor",
    "methodology_dataset",
    "methodology_pcodes",
    "caveats",
]
_INTEGERS = {"admin_level_full", "admin_level_max", "update_frequency"}
_REQUIRED = ["source", "source_url", "contributor", "methodology_dataset"]


def _type(column: str) -> str:
    if column.startswith("date_"):
        return "DATE"
    if column.endswith("_count") or column in _INTEGERS:
        return "INTEGER"
    return "VARCHAR"


def level_counts(packaging: Path, iso3: str) -> dict[int, int]:
    """Count units in each packaged {iso3}_admin{n}.parquet."""
    counts = {}
    for n in range(1, _MAX_LEVEL + 1):
        path = packaging / f"{iso3}_admin{n}.parquet"
        if path.exists():
            counts[n] = duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchone()[0]
    return counts


def full_level(packaging: Path, iso3: str, max_level: int) -> int:
    """Return the deepest level whose code is set on every unit of the finest layer."""
    path = packaging / f"{iso3}_admin{max_level}.parquet"
    columns = {r[0] for r in duckdb.sql(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    for n in range(max_level, 1, -1):
        code = f"adm{n}_pcode"
        if code not in columns:
            return max_level
        blank = f"SELECT count(*) FROM '{path}' WHERE coalesce(trim({code}), '') = ''"
        if duckdb.sql(blank).fetchone()[0] == 0:
            return n
    return 1


def build_record(iso3: str, version: str, record: dict, settings: dict) -> dict:
    """Merge the working record, workspace settings and packaged layers into one row."""
    packaging = Path("02_working") / iso3 / version / "06_packaging"
    counts = level_counts(packaging, iso3)
    if not counts:
        sys.exit(f"no {iso3}_admin{{n}}.parquet in {packaging}")
    max_level = max(counts)
    layers = sorted(record.get("source_layers", []), key=lambda layer: layer["level"])
    source_dates = [
        layer["date_updated"] for layer in layers if layer.get("date_updated")
    ]
    names = record.get("admin_names", {})
    row = {
        "country_name": record.get("country_name", ""),
        "country_iso2": record.get("country_iso2", "").upper(),
        "country_iso3": iso3.upper(),
        "version": version,
        "admin_level_full": full_level(packaging, iso3, max_level),
        "admin_level_max": max_level,
        "admin_notes": " ".join(record.get("admin_notes", [])),
        "date_source": max(source_dates, default=""),
        "date_updated": record.get("date_updated", ""),
        "date_reviewed": record.get("date_reviewed", ""),
        "date_metadata": datetime.now(UTC).date().isoformat(),
        "date_valid_on": record.get("date_valid_on", ""),
        "date_valid_to": "",
        "update_frequency": settings.get("update_frequency", 1),
        "update_type": record.get("update_type", "major"),
        "source": record.get("source", ""),
        "source_url": "; ".join(layer["url"] for layer in layers),
        "contributor": settings.get("contributor", ""),
        "methodology_dataset": record.get("methodology_dataset", ""),
        "methodology_pcodes": record.get("methodology_pcodes", ""),
        "caveats": " ".join(record.get("caveats", [])),
    }
    for n in range(1, _MAX_LEVEL + 1):
        row[f"admin_{n}_name"] = names.get(str(n), "")
        row[f"admin_{n}_count"] = counts.get(n, "")
    return row


def main() -> None:
    """Write the metadata CSV, exiting non-zero when a required field is empty."""
    parser = argparse.ArgumentParser(description="Write the COD-AB metadata CSV.")
    parser.add_argument("iso3", help="Country ISO3 code (e.g. syr)")
    parser.add_argument("version", help="Dataset version (e.g. v01)")
    args = parser.parse_args()
    iso3 = args.iso3.lower()

    working = Path("02_working") / iso3 / args.version
    record = json.loads((working / "metadata.json").read_text(encoding="utf-8"))
    with Path("pyproject.toml").open("rb") as f:
        settings = tomllib.load(f).get("tool", {}).get("topo-tools-cod-ab", {})
    row = build_record(iso3, args.version, record, settings)

    out = working / "06_packaging" / f"{iso3}_metadata.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
    types = {c: _type(c) for c in _COLUMNS}
    duckdb.sql(f"""
        COPY (SELECT * FROM read_csv('{out}', header = true, columns = {types}))
        TO '{out.with_suffix(".parquet")}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    for column in _COLUMNS:
        log.info("%-20s %s", column, row[column])
    if missing := [c for c in _REQUIRED if not str(row[c]).strip()]:
        sys.exit(f"missing required field(s): {', '.join(missing)}")


if __name__ == "__main__":
    main()
