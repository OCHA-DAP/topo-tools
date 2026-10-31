#!/usr/bin/env python3
"""Rewrite the `duckdb` resource block in a Homebrew formula to point at the
prebuilt macOS universal2 wheel for whichever duckdb version `brew
bump-formula-pr` most recently pinned as the sdist resource, instead of the
sdist tarball (which requires compiling duckdb's C++ core with cmake/ninja),
and repin the `on_linux` `duckdb-linux` blocks to that version's manylinux
x86_64/aarch64 wheels, which `brew bump-formula-pr` never rewrites.

Usage: sync_duckdb_wheel.py <path-to-formula.rb>

Exits non-zero (leaving the file untouched) if:
  - no `resource "duckdb" do ... end` block is found,
  - a duckdb version can't be parsed out of it,
  - either `duckdb-linux` block (on_intel, on_arm) is missing,
  - PyPI has no single cp314 (or abi3 at or below cp314) macosx universal2,
    manylinux x86_64 or manylinux aarch64 wheel for that exact version.

A non-zero exit is intentional and fails the CI job: it leaves the tap's
formula-bump PR open and unmerged for a human to look at, rather than
silently merging a formula that can't build (see homebrew-tap.yml, where the
subsequent "Merge Homebrew formula bump PR" step only runs on success).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

# Keep in sync with the formula's `depends_on "python@3.14"`.
PYTHON_TAG = "cp314"
# A version-specific wheel wins; else an abi3 wheel built for cp314 or older.
WHEEL_TAGS = (f"{PYTHON_TAG}-{PYTHON_TAG}", r"cp3(?:[0-9]|1[0-4])-abi3")

RESOURCE_RE = re.compile(r'(  resource "duckdb" do\n)(.*?\n)(  end\n)', re.DOTALL)
VERSION_RE = re.compile(r"duckdb-([0-9][\w.]*)\.tar\.gz")
LINUX_ARCHES = {"on_intel": "x86_64", "on_arm": "aarch64"}


def linux_block_re(arch_block: str) -> re.Pattern[str]:
    return re.compile(
        rf'(    {arch_block} do\n      resource "duckdb-linux" do\n)(.*?\n)(      end\n)',
        re.DOTALL,
    )


def fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


def find_wheel(
    urls: list[dict], pattern: str, label: str, hint: str
) -> tuple[str, str]:
    candidates = []
    for tag in WHEEL_TAGS:
        wheel_re = re.compile(pattern.replace("{tag}", tag))
        candidates = [
            entry
            for entry in urls
            if entry.get("packagetype") == "bdist_wheel"
            and wheel_re.match(entry.get("filename", ""))
        ]
        if candidates:
            break
    if not candidates:
        fail(f"no {label} found on PyPI. {hint}")
    if len(candidates) > 1:
        fail(f"multiple matching {label} found, refusing to guess: {candidates}")
    wheel = candidates[0]
    print(f"resolved {label}: {wheel['url']}")
    return wheel["url"], wheel["digests"]["sha256"]


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: sync_duckdb_wheel.py <path-to-formula.rb>")

    formula_path = sys.argv[1]
    with open(formula_path, encoding="utf-8") as f:
        contents = f.read()

    match = RESOURCE_RE.search(contents)
    if not match:
        fail('could not find a `resource "duckdb" do ... end` block in the formula')

    block_body = match.group(2)
    version_match = VERSION_RE.search(block_body)
    if not version_match:
        fail(
            f"could not parse a duckdb version out of the resource block:\n{block_body}"
        )
    version = version_match.group(1)
    print(f"duckdb version pinned by brew bump-formula-pr: {version}")

    api_url = f"https://pypi.org/pypi/duckdb/{version}/json"
    with urllib.request.urlopen(api_url, timeout=30) as response:
        data = json.load(response)

    urls = data.get("urls", [])
    wheel_url, wheel_sha256 = find_wheel(
        urls,
        rf"^duckdb-{re.escape(version)}-{{tag}}-macosx_[0-9_]+_universal2\.whl$",
        f"{PYTHON_TAG}-compatible macOS universal2 wheel for duckdb=={version}",
        "PyPI's duckdb wheel matrix may have changed shape (e.g. split arm64/x86_64 "
        "wheels instead of universal2); this script needs a human to update it. "
        "Leaving the sdist resource block in place -- `brew install` will now fail "
        "closed (no cmake/ninja) until this is fixed.",
    )

    new_block = f'  resource "duckdb" do\n    url "{wheel_url}"\n    sha256 "{wheel_sha256}"\n  end\n'
    new_contents = contents[: match.start()] + new_block + contents[match.end() :]

    for arch_block, arch in LINUX_ARCHES.items():
        linux_match = linux_block_re(arch_block).search(new_contents)
        if not linux_match:
            fail(
                f'could not find an `{arch_block} do resource "duckdb-linux" do ... end` block'
            )
        linux_url, linux_sha256 = find_wheel(
            urls,
            rf"^duckdb-{re.escape(version)}-{{tag}}-manylinux[\w.]*_{arch}\.whl$",
            f"{PYTHON_TAG}-compatible manylinux {arch} wheel for duckdb=={version}",
            "PyPI's duckdb Linux wheel tags may have changed; this script needs a human "
            "to update it.",
        )
        linux_block = (
            f'{linux_match.group(1)}        url "{linux_url}"\n'
            f'        sha256 "{linux_sha256}"\n{linux_match.group(3)}'
        )
        new_contents = (
            new_contents[: linux_match.start()]
            + linux_block
            + new_contents[linux_match.end() :]
        )

    if new_contents == contents:
        fail("rewrite produced no change; refusing to no-op silently")

    with open(formula_path, "w", encoding="utf-8") as f:
        f.write(new_contents)

    print(
        f"Patched {formula_path}: duckdb resources now point at the {PYTHON_TAG}-compatible "
        "universal2 and manylinux wheels."
    )


if __name__ == "__main__":
    main()
