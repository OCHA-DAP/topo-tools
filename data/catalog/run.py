"""Rebuild the local portolan catalog end to end, optionally pushing it to source.coop."""

import argparse
import hashlib
import json
import logging
from pathlib import Path
from subprocess import CalledProcessError

from annotate_nld import annotate_catalog
from build_demo import build as build_demo
from build_nld import YEARS
from build_nld import build as build_nld
from push import REMOTE, portolan, push

try:
    import build_sources
except ModuleNotFoundError:  # gitignored, only on the maintainer's machine
    build_sources = None

DEMO_DATETIME = "2025-01-01"
logger = logging.getLogger(__name__)


def drop_stale_tiles(collection: Path) -> None:
    # portolan 0.8.0 fails instead of replacing the tileset of a parquet whose bytes changed since its last add.
    for versions in collection.rglob("versions.json"):
        data = json.loads(versions.read_text())
        current = next(
            (
                v
                for v in data.get("versions", [])
                if v["version"] == data.get("current_version")
            ),
            None,
        )
        for name, asset in (current or {}).get("assets", {}).items():
            parquet = versions.parent / name
            tiles = parquet.with_suffix(".pmtiles")
            if parquet.suffix != ".parquet" or not (
                parquet.exists() and tiles.exists()
            ):
                continue
            with parquet.open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() != asset["sha256"]:
                    tiles.unlink()


def add(catalog: Path, collection: str, datetime: str) -> None:
    drop_stale_tiles(catalog / collection)
    portolan(["add", collection, "--pmtiles", "--datetime", datetime], cwd=catalog)


def add_all(catalog: Path, sources: list[str]) -> None:
    for year in YEARS:
        add(catalog, f"nld/{year}", f"{year}-01-01")
    for demo in sorted((catalog / "nld" / "demo").iterdir()):
        if (demo / ".portolan").is_dir():
            add(catalog, demo.relative_to(catalog).as_posix(), DEMO_DATETIME)
    for collection in sources:
        add(catalog, collection, build_sources.DATETIME)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=Path("portolan"), help="Catalog root directory"
    )
    parser.add_argument("--cache", type=Path, default=Path("tmp/catalog/cache"))
    parser.add_argument("--refetch", action="store_true")
    parser.add_argument(
        "--detect", action="store_true", help="Add topo-detect counts to the report"
    )
    parser.add_argument(
        "--explorer",
        type=Path,
        default=Path("../hdx-boundaries-explorer/static/parquet"),
        help="HDX Boundaries Explorer data for the sources extracts; skipped if missing",
    )
    parser.add_argument("--push", action="store_true", help=f"Push to {REMOTE}")
    args = parser.parse_args()
    catalog = args.catalog.resolve()

    build_nld(catalog, args.cache, refetch=args.refetch, detect_issues=args.detect)
    annotate_catalog(catalog, args.cache, metadata_only=True)
    build_demo(catalog, args.cache / "demo")
    explorer = args.explorer.resolve()
    sources = (
        build_sources.build(catalog, explorer)
        if build_sources and explorer.is_dir()
        else []
    )
    add_all(catalog, sources)
    annotate_catalog(catalog, args.cache)
    portolan(["readme"], cwd=catalog)
    # readme rewrites READMEs after add recorded their checksums; --fix exits 1 while unfixable issues remain.
    try:
        portolan(["check", "--fix", "--data-scope", "local"], cwd=catalog)
    except CalledProcessError:
        logger.warning("portolan check --fix left issues it can't repair (continuing)")
    if args.push:
        push(catalog)


if __name__ == "__main__":
    main()
