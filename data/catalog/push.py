"""Push the catalog to source.coop, with the portolan 0.8.0 workarounds from hdx-scraper-cod-ab-global."""

import json
import logging
import shutil
from pathlib import Path
from subprocess import CalledProcessError, run

logger = logging.getLogger(__name__)

REMOTE = "s3://hdx/topo-tools/"
WORKERS = "8"


def portolan(args: list[str], cwd: Path) -> None:
    run([shutil.which("portolan") or "portolan", *args], cwd=cwd, check=True)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _leaf_versions(catalog: Path) -> list[tuple[Path, dict]]:
    # Leaf collections carry a `versions` list; the root and intermediate versions.json don't.
    return [
        (path, data)
        for path in sorted(catalog.rglob("versions.json"))
        if "versions" in (data := _read(path))
    ]


def normalize_asset_hrefs(catalog: Path) -> int:
    """Rewrite hrefs `portolan add <subpath>` wrote relative to that subpath back to catalog-root-relative."""
    fixed = 0
    for versions_path, data in _leaf_versions(catalog):
        prefix = versions_path.parent.parent.relative_to(catalog).as_posix()
        changed = False
        for version in data["versions"]:
            for asset in version.get("assets", {}).values():
                href = asset.get("href")
                if not href or (catalog / href).exists():
                    continue
                candidate = f"{prefix}/{href}" if prefix != "." else href
                if (catalog / candidate).exists():
                    asset["href"] = candidate
                    changed = True
        if changed:
            versions_path.write_text(json.dumps(data, indent=2, sort_keys=True))
            fixed += 1
    return fixed


def push_catalog_files(catalog: Path, remote: str) -> None:
    """Sync intermediate catalog.json, README.md and AGENTS.md, which `portolan push` skips (portolan-cli#552)."""
    run(
        [
            "aws",
            "s3",
            "sync",
            str(catalog),
            remote.rstrip("/"),
            "--exclude",
            "*",
            "--include",
            "catalog.json",
            "--include",
            "*/catalog.json",
            "--include",
            "*/*/catalog.json",
            "--include",
            "*/README.md",
            "--include",
            "*/*/README.md",
            "--include",
            "*/AGENTS.md",
            "--include",
            "*/*/AGENTS.md",
            "--exclude",
            "*/*/*/*",
        ],
        check=True,
    )


def sync_deletions(catalog: Path, remote: str) -> None:
    """Mirror-delete remote objects removed locally; `portolan push` never deletes (portolan-cli#753)."""
    run(
        [
            "aws",
            "s3",
            "sync",
            str(catalog),
            remote.rstrip("/"),
            "--delete",
            "--exclude",
            ".portolan/*",
            "--exclude",
            "*/.portolan/*",
            "--exclude",
            ".state/*",
            "--exclude",
            "*/.state/*",
        ],
        check=True,
    )


def _remote_keys(remote: str) -> set[str]:
    listing = run(
        ["aws", "s3", "ls", "--recursive", remote],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line.split(maxsplit=3)[3] for line in listing.splitlines() if line.strip()}


def find_incomplete_collections(catalog: Path, remote: str) -> list[str]:
    """Return collection ids whose current version is missing from remote."""
    prefix = remote.removeprefix("s3://").split("/", 1)[1].strip("/")
    keys = _remote_keys(remote)
    incomplete = []
    for versions_path, data in _leaf_versions(catalog):
        collection = versions_path.parent.relative_to(catalog).as_posix()
        required = [f"{prefix}/{collection}/versions.json"]
        for version in data["versions"]:
            if version.get("version") == data.get("current_version"):
                required += [
                    f"{prefix}/{asset['href']}"
                    for asset in version.get("assets", {}).values()
                    if asset.get("href")
                ]
        if any(key not in keys for key in required):
            incomplete.append(collection)
    return incomplete


def repair_collections(catalog: Path, remote: str, collections: list[str]) -> None:
    """Re-push each incomplete collection individually."""
    for collection in collections:
        logger.warning("Repairing incomplete collection %s", collection)
        try:
            portolan(
                ["push", remote, "--collection", collection, "--force", "--verbose"],
                cwd=catalog,
            )
        except CalledProcessError:
            logger.warning("Repair push failed for %s", collection)


def push(catalog: Path, remote: str = REMOTE) -> None:
    normalize_asset_hrefs(catalog)
    try:
        portolan(["push", remote, "--workers", WORKERS, "--verbose"], cwd=catalog)
    except CalledProcessError:
        logger.warning("portolan push failed (verifying remote state)")
    push_catalog_files(catalog, remote)
    sync_deletions(catalog, remote)
    if incomplete := find_incomplete_collections(catalog, remote):
        logger.warning(
            "%d collection(s) missing from remote, repairing", len(incomplete)
        )
        repair_collections(catalog, remote, incomplete)
        incomplete = find_incomplete_collections(catalog, remote)
    try:
        portolan(["check", "--data-scope", "local", "--verbose"], cwd=catalog)
    except CalledProcessError:
        logger.warning("portolan check reported issues (continuing)")
    if incomplete:
        msg = f"Collections still missing from remote after repair: {incomplete}"
        raise RuntimeError(msg)
