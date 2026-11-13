"""Runs every single-layer detect tool on one layer and summarizes their reports."""

from logging import getLogger
from pathlib import Path

import duckdb

from topo_tools.api.code_detect import detect as code_detect
from topo_tools.api.name_detect import detect as name_detect
from topo_tools.api.schema_detect import detect as schema_detect
from topo_tools.api.topo_detect import detect as topo_detect
from topo_tools.core.validate import _summary as summary
from topo_tools.core.validate._constants import BLOCKING_KINDS, STAGES

logger = getLogger(__name__)


def validate(  # noqa: PLR0913
    input_path: Path | str,
    reports: dict[str, Path],
    summary_path: Path,
    *,
    skip_topo: bool = False,
    name_field: str | None = None,
    code_field: str | None = None,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    debug: bool = False,
) -> bool:
    """Run schema-, topo-, code- and name-detect into reports, then summarize.

    Returns True if any report has an error row or a stage failed.
    """
    common = {"threads": threads, "tmp_dir": tmp_dir, "debug": debug}
    fields = {"name_field": name_field, "code_field": code_field}
    runs = {
        "schema": lambda p: schema_detect(input_path, p, **fields, **common),
        "topo": lambda p: topo_detect(input_path, p, **common),
        "code": lambda p: code_detect(input_path, p, **fields, **common),
        "name": lambda p: name_detect(input_path, p, **fields, **common),
    }
    with duckdb.connect() as conn:
        summary.create(conn)
        blocked = False
        for stage in STAGES:
            if stage == "topo" and skip_topo:
                reason = "not checked: package ran with --no-clean"
                summary.add_outcome(conn, stage, "skipped", "warn", reason)
                continue
            if blocked and stage in {"code", "name"}:
                reason = "levels could not be detected; see the schema report"
                summary.add_outcome(conn, stage, "skipped", "warn", reason)
                continue
            try:
                runs[stage](reports[stage])
            except Exception as e:  # noqa: BLE001 (one failing stage must not hide the rest)
                logger.warning("%s-detect failed: %s", stage, e)
                summary.add_outcome(conn, stage, "failed", "error", str(e))
                continue
            summary.add_report(conn, stage, reports[stage])
            if stage == "schema":
                blocked = summary.has_kind(conn, stage, BLOCKING_KINDS)
        rows = summary.write(conn, summary_path)
    for stage, kind, severity, count, _, reason in rows:
        logger.info(
            "%-6s %-8s %-24s %s",
            stage,
            severity or "",
            kind or "",
            count if count is not None else reason,
        )
    logger.info("summary: %s", summary_path)
    return any(row[2] == "error" for row in rows)
