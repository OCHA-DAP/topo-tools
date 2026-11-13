"""Public API: clean, validate, then package polygons, points and lines in one call."""

from logging import getLogger
from pathlib import Path

from topo_tools.api._validate import validate as run_validation
from topo_tools.api.package_lines import package_lines
from topo_tools.api.package_points import package_points
from topo_tools.api.package_polygons import package_polygons
from topo_tools.core.duckdb_utils import pipeline_connection, resolve_tmp_dir
from topo_tools.core.io import (
    check_overwrite,
    default_output_path,
    export_geometry_table,
    input_basename,
    read_reproject_and_clean,
    resolve_input_path,
)
from topo_tools.core.validate._constants import STAGES

logger = getLogger(__name__)


def _substitute(output_path: str | Path | None, word: str) -> str | None:
    if output_path is None:
        return None
    text = str(output_path)
    if "{x}" not in text:
        msg = f"output_path given without a literal '{{x}}' placeholder: {output_path}"
        raise ValueError(msg)
    return text.replace("{x}", word)


def _clean(
    input_path: Path | str, work: Path, *, threads: int | None, debug: bool
) -> Path:
    """Write input_path, coverage-cleaned if it has defects, as GeoParquet in work."""
    name = input_basename(input_path).replace(".", "_") + "_package"
    dest = work / "package_clean" / f"{Path(input_basename(input_path)).stem}.parquet"
    with pipeline_connection(name, work, threads=threads, debug=debug) as conn:
        read_reproject_and_clean(conn, name, input_path)
        export_geometry_table(conn, f"{name}_01", dest)
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{name}_01"')
    return dest


def package(  # noqa: PLR0913
    input_path: str | Path,
    output_path: str | Path | None = None,
    name_field: str | None = None,
    code_field: str | None = None,
    *,
    output_name_field: str | None = None,
    output_code_field: str | None = None,
    aggregations: dict[str, str] | None = None,
    clean: bool = True,
    block: bool = True,
    threads: int | None = None,
    tmp_dir: str | Path | None = None,
    overwrite: bool = True,
    debug: bool = False,
) -> None:
    """Coverage-clean and validate one input, then package polygons, points, lines.

    Raises ValueError, before writing any output, if validation finds an error
    and block is set.
    """
    input_path = resolve_input_path(input_path)
    polygons = _substitute(output_path, "admin{n}") or str(
        default_output_path(input_path, "_admin{n}")
    )
    points = Path(
        _substitute(output_path, "points") or default_output_path(input_path, "_points")
    )
    lines = Path(
        _substitute(output_path, "lines") or default_output_path(input_path, "_lines")
    )
    report_dir = Path(polygons).parent
    stem = Path(input_basename(input_path)).stem
    reports = {s: report_dir / f"{stem}_{s}_issues.parquet" for s in STAGES}
    summary_path = report_dir / f"{stem}_validate_summary.csv"
    for path in [points, lines, summary_path, *reports.values()]:
        check_overwrite(path, overwrite=overwrite)

    with resolve_tmp_dir(tmp_dir, debug=debug) as work:
        source = (
            _clean(input_path, work, threads=threads, debug=debug)
            if clean
            else input_path
        )
        common = {"threads": threads, "tmp_dir": work, "debug": debug}
        errors = run_validation(
            source,
            reports,
            summary_path,
            skip_topo=not clean,
            name_field=name_field,
            code_field=code_field,
            **common,
        )
        if errors and block:
            msg = f"validation found errors, see {summary_path}"
            raise ValueError(msg)
        if errors:
            logger.warning(
                "validation found errors, packaging anyway: %s", summary_path
            )

        package_polygons(
            source,
            polygons,
            name_field=name_field,
            code_field=code_field,
            output_name_field=output_name_field,
            output_code_field=output_code_field,
            aggregations=aggregations,
            overwrite=overwrite,
            check_topology=clean,
            original_path=input_path,
            **common,
        )
        package_points(
            source,
            points,
            name_field,
            code_field,
            overwrite=overwrite,
            check_topology=clean,
            **common,
        )
        package_lines(
            source,
            lines,
            name_field,
            code_field,
            overwrite=overwrite,
            check_topology=clean,
            **common,
        )
        if clean and not debug:
            source.unlink()
