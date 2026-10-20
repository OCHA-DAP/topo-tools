"""Regression tests for the COD-AB metadata packaging script."""

import csv
import json
import runpy
import sys
from pathlib import Path

import duckdb
import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="The standalone metadata script requires Python 3.11+",
)

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "plugins/topo-tools/skills/cod-ab/scripts/metadata.py"
)


@pytest.fixture
def metadata_workspace(tmp_path, monkeypatch):
    """Provide synthetic packaged units and all required provenance fields."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "zzz", "v01"])
    working = tmp_path / "02_working/zzz/v01"
    packaging = working / "06_packaging"
    packaging.mkdir(parents=True)
    with duckdb.connect() as conn:
        conn.execute(
            f"COPY (SELECT * FROM (VALUES ('Z1'), ('Z2')) AS t(adm1_pcode)) "
            f"TO '{packaging / 'zzz_admin1.parquet'}' (FORMAT PARQUET)"
        )
    settings = '[tool.topo-tools-cod-ab]\ncontributor = "Synthetic review team"\n'
    (tmp_path / "pyproject.toml").write_text(settings, encoding="utf-8")
    record = {
        "country_name": "Synthetic test country",
        "country_iso2": "zz",
        "source": "Synthetic source ministry",
        "source_layers": [],
        "methodology_dataset": "Synthetic fixture; no real country data.",
    }
    return working, packaging, record


@pytest.mark.parametrize(
    ("layers", "expected_url"),
    [
        ([{"level": 1, "url": None}], ""),
        (
            [
                {"level": 3, "url": "https://example.org/local"},
                {"level": 1, "url": None},
                {"level": 2, "url": "https://example.org/regional"},
            ],
            "https://example.org/regional; https://example.org/local",
        ),
        (
            [{"level": 2, "url": None}, {"level": 1, "url": None}],
            "",
        ),
        (
            [
                {"level": 2, "url": "https://example.org/local"},
                {"level": 1, "url": "https://example.org/regional"},
            ],
            "https://example.org/regional; https://example.org/local",
        ),
    ],
    ids=["direct-handover", "mixed", "all-null", "existing-urls"],
)
def test_exports_available_source_urls(metadata_workspace, layers, expected_url):
    working, packaging, record = metadata_workspace
    record["source_layers"] = layers
    (working / "metadata.json").write_text(json.dumps(record), encoding="utf-8")

    runpy.run_path(str(_SCRIPT))["main"]()

    with (packaging / "zzz_metadata.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["source_url"] == expected_url
    assert rows[0]["source"] == record["source"]
    assert rows[0]["methodology_dataset"] == record["methodology_dataset"]
    assert rows[0]["admin_1_count"] == "2"
    with duckdb.connect() as conn:
        values = conn.execute(
            "SELECT source_url, source, admin_1_count FROM "
            f"'{packaging / 'zzz_metadata.parquet'}'"
        ).fetchall()
    assert values == [(expected_url or None, record["source"], 2)]


@pytest.mark.parametrize("missing", ["source", "contributor", "methodology_dataset"])
def test_still_requires_other_provenance(metadata_workspace, missing):
    working, _, record = metadata_workspace
    record["source_layers"] = [{"level": 1, "url": "https://example.org/source"}]
    if missing == "contributor":
        Path("pyproject.toml").write_text(
            "[tool.topo-tools-cod-ab]\n", encoding="utf-8"
        )
    else:
        record[missing] = ""
    (working / "metadata.json").write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SystemExit, match=f"missing required field\\(s\\): {missing}$"):
        runpy.run_path(str(_SCRIPT))["main"]()
