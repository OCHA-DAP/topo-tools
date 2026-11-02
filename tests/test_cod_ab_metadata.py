"""Tests for the cod-ab skill's standalone metadata.py script."""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import pytest

_SCRIPT = (
    Path(__file__).parents[1] / "plugins/topo-tools/skills/cod-ab/scripts/metadata.py"
)


def _run(tmp_path, monkeypatch, layers, units, *args):
    """Write a one-layer workspace, run metadata.py on it and return the CSV row."""
    working = tmp_path / "02_working/zzz/v01"
    packaging = working / "06_packaging"
    packaging.mkdir(parents=True)
    columns = ", ".join(units[0])
    values = ", ".join(str(tuple(row.values())) for row in units)
    level = len(units[0])
    with duckdb.connect() as conn:
        conn.execute(
            f"COPY (SELECT * FROM (VALUES {values}) AS t({columns})) "
            f"TO '{packaging / f'zzz_admin{level}.parquet'}'"
        )
    record = {
        "source": "Survey office",
        "methodology_dataset": "Handed over by the survey office.",
        "source_layers": layers,
    }
    (working / "metadata.json").write_text(json.dumps(record))
    (tmp_path / "pyproject.toml").write_text(
        '[tool.topo-tools-cod-ab]\ncontributor = "OCHA"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["metadata.py", "zzz", "v01", *args])
    spec = importlib.util.spec_from_file_location("cod_ab_metadata", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    with (packaging / "zzz_metadata.csv").open(newline="") as f:
        return next(csv.DictReader(f))


@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        ([None, None], ""),
        (
            ["https://example.org/a1", None, "https://example.org/a3"],
            "https://example.org/a1; https://example.org/a3",
        ),
    ],
    ids=["all-null", "mixed"],
)
def test_source_url_skips_null_urls(tmp_path, monkeypatch, urls, expected):
    layers = [{"level": n, "url": url} for n, url in enumerate(urls, 1)][::-1]
    row = _run(tmp_path, monkeypatch, layers, [{"adm1_code": "Z1"}])
    assert row["source_url"] == expected


@pytest.mark.parametrize(
    ("args", "expected"), [((), "2"), (("--code-field", "adm{n}_pcode"), "1")]
)
def test_admin_level_full_follows_code_field(tmp_path, monkeypatch, args, expected):
    units = [
        {"adm1_pcode": "Z1", "adm2_pcode": "Z101"},
        {"adm1_pcode": "Z1", "adm2_pcode": ""},
    ]
    layers = [{"level": 2, "url": "https://example.org/a2"}]
    row = _run(tmp_path, monkeypatch, layers, units, *args)
    assert row["admin_level_full"] == expected
