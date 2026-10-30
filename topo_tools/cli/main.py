"""topo-tools CLI: click entry point."""

import glob
from logging import INFO, basicConfig, getLogger
from pathlib import Path

import click

from topo_tools.api import change as _change
from topo_tools.api import code_detect as _code_detect
from topo_tools.api import edge_clip as _edge_clip
from topo_tools.api import edge_extend as _edge_extend
from topo_tools.api import edge_match as _edge_match
from topo_tools.api import edge_mosaic as _edge_mosaic
from topo_tools.api import edge_stitch as _edge_stitch
from topo_tools.api import name_clean as _name_clean
from topo_tools.api import name_detect as _name_detect
from topo_tools.api import package as _package
from topo_tools.api import schema_detect as _schema_detect
from topo_tools.api import schema_fill as _schema_fill
from topo_tools.api import schema_join as _schema_join
from topo_tools.api import schema_map as _schema_map
from topo_tools.api import topo_clean as _topo_clean
from topo_tools.api import topo_detect as _topo_detect
from topo_tools.api import validate as _validate
from topo_tools.api.code_create import code_create as _code_create
from topo_tools.api.code_update import code_update as _code_update
from topo_tools.api.package_lines import package_lines as _package_lines
from topo_tools.api.package_points import package_points as _package_points
from topo_tools.api.package_polygons import package_polygons as _package_polygons
from topo_tools.core.change._constants import TAU_MATCH_DEFAULT, TAU_SAME_DEFAULT
from topo_tools.core.schema_join._constants import MIN_OVERLAP_DEFAULT

basicConfig(level=INFO, format="%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = getLogger(__name__)


def _split_commas(values: tuple[str, ...]) -> list[str]:
    """Flatten repeated --flag occurrences, each optionally comma-separated."""
    return [v for raw in values for v in raw.split(",")]


def _split_columns(value: str | None) -> list[str] | None:
    """Comma-split a column-list flag's raw value, or None if unset."""
    return value.split(",") if value is not None else None


def _parse_aggregations(values: tuple[str, ...]) -> dict[str, str] | None:
    """Parse repeated --aggregation 'column=function' values into a dict."""
    if not values:
        return None
    aggregations = {}
    for raw in values:
        column, sep, function = raw.partition("=")
        if not sep:
            msg = f"--aggregation must be 'column=function', got {raw!r}"
            raise click.BadParameter(msg)
        aggregations[column] = function
    return aggregations


_MERGE_OPTIONS = (
    click.option(
        "--merge",
        envvar="MERGE",
        is_flag=True,
        help=(
            "Copy the overlay's columns onto every matched input polygon, and "
            "keep unmatched overlay or input polygons in the output, unclipped, "
            "instead of dropping them. Choose columns with --overlay-include, "
            "--overlay-exclude, --input-include and --input-exclude. When both "
            "layers have a column with the same name, choose which one to keep "
            "with --prefer."
        ),
    ),
    click.option(
        "--overlay-include",
        envvar="OVERLAY_INCLUDE",
        default=None,
        help="Overlay columns to copy, comma-separated. Needs --merge.",
    ),
    click.option(
        "--overlay-exclude",
        envvar="OVERLAY_EXCLUDE",
        default=None,
        help="Overlay columns not to copy, comma-separated. Needs --merge.",
    ),
    click.option(
        "--input-include",
        envvar="INPUT_INCLUDE",
        default=None,
        help="Input columns to keep, comma-separated. Needs --merge.",
    ),
    click.option(
        "--input-exclude",
        envvar="INPUT_EXCLUDE",
        default=None,
        help="Input columns to drop, comma-separated. Needs --merge.",
    ),
    click.option(
        "--prefer",
        envvar="PREFER",
        type=click.Choice(["overlay", "input"]),
        default=None,
        help=(
            "When the overlay and input both have a column with the same name, "
            "keep this layer's column. Needs --merge. Can't be combined with "
            "--overlay-include, --overlay-exclude, --input-include or "
            "--input-exclude."
        ),
    ),
)


def _add_merge_options(f):  # noqa: ANN001, ANN202
    """Apply the shared --merge option set to a command function."""
    for option in reversed(_MERGE_OPTIONS):
        f = option(f)
    return f


_FILL_OPTIONS = (
    click.option(
        "--fill-schema",
        envvar="FILL_SCHEMA",
        is_flag=True,
        help=(
            "Before writing the output, fill each row's empty finer admin "
            "columns from its coarser ones and add a column with the row's own "
            "admin level. Set the columns with --name-field and --code-field, "
            "and the level column's name with --depth-column."
        ),
    ),
    click.option(
        "--name-field",
        envvar="NAME_FIELD",
        default=None,
        help="Name column of each level, with {n} for the level number, e.g. "
        "'adm{n}_name'. Needs --fill-schema and --code-field. Without both, levels "
        "are detected from the data.",
    ),
    click.option(
        "--code-field",
        envvar="CODE_FIELD",
        default=None,
        help="Code column of each level, with {n} for the level number, e.g. "
        "'adm{n}_code'. Needs --fill-schema and --name-field. Without both, levels "
        "are detected from the data.",
    ),
    click.option(
        "--depth-column",
        envvar="DEPTH_COLUMN",
        default="adm_lvl",
        show_default=True,
        help="Name of the added column holding each row's own admin level. "
        "Needs --fill-schema.",
    ),
)


def _add_fill_options(f):  # noqa: ANN001, ANN202
    """Apply the shared --fill-schema option set to a command function."""
    for option in reversed(_FILL_OPTIONS):
        f = option(f)
    return f


@click.group()
@click.version_option(package_name="topo-tools", prog_name="topo-tools")
def cli() -> None:
    """topo-tools: tools for cleaning and packaging administrative boundaries."""


@cli.command(name="edge-extend")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "lines", "attempt", "merge", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def edge_extend(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Extend polygons outward to fill the gaps around and between them.

    Each polygon grows into the empty space next to it (coastlines, water
    bodies, disputed areas) using Voronoi diagrams, so the layer covers a
    continuous area. OUTPUT_FILE defaults to INPUT_FILE with an "_extended"
    suffix.

    \b
    Examples:
      # Basic run, output name chosen automatically
      topo-tools edge-extend example.geojson

    \b
      # Explicit output
      topo-tools edge-extend example.gpkg example_extended.gpkg

    \b
      # Stop with an error if the output already exists
      topo-tools edge-extend example.parquet example_extended.parquet --overwrite=false
    """
    logger.info("--debug=%s", debug)
    try:
        _edge_extend(
            input_file,
            Path(output_file) if output_file is not None else None,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="topo-detect")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "issues", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def topo_detect(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Find gaps and overlaps between the polygons of one layer.

    Writes the problems found without changing the layer. OUTPUT_FILE
    defaults to INPUT_FILE with an "_issues" suffix.

    \b
    Examples:
      # Basic run, output name chosen automatically
      topo-tools topo-detect example.geojson

    \b
      # Explicit output
      topo-tools topo-detect example.gpkg example_issues.gpkg
    """
    logger.info("--debug=%s", debug)
    try:
        _topo_detect(
            input_file,
            Path(output_file) if output_file is not None else None,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="name-detect")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("issues_file", envvar="ISSUES_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "checks", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def name_detect(  # noqa: PLR0913, PLR0917
    input_file: str,
    issues_file: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Find problems in the unit names of one coded layer.

    Checks every name column of every level: blank and placeholder names,
    duplicates under the same parent, encoding errors, invisible
    characters, spacing, case and mixed scripts. Writes the problems
    found without changing the layer, even when there are none.
    ISSUES_FILE defaults to INPUT_FILE with a "_name_issues" suffix, as
    Parquet with each unit's geometry, or CSV for a .csv name.

    \b
    Examples:
      # Basic run, report named automatically
      topo-tools name-detect admin3.parquet

    \b
      # Explicit level columns, CSV report for a spreadsheet
      topo-tools name-detect admin3.parquet admin3_name_issues.csv \\
        --name-field adm{n}_name --code-field adm{n}_code
    """
    logger.info("--debug=%s", debug)
    try:
        _name_detect(
            input_file,
            Path(issues_file) if issues_file is not None else None,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="name-clean")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.argument("issues_file", envvar="ISSUES_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "checks", "fix", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def name_clean(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    issues_file: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Fix the safe problems in the unit names of one coded layer.

    Runs the same checks as name-detect, then fixes only what can't change
    a name's meaning: spacing, invisible characters, accents stored as
    separate characters, and text read with the wrong encoding when the
    repair is certain. Everything else stays as it is, for review.
    OUTPUT_FILE defaults to INPUT_FILE with a "_cleaned" suffix; ISSUES_FILE
    defaults to INPUT_FILE with a "_name_issues" suffix, as Parquet with
    each unit's geometry, with a "fixed" column marking what was fixed.

    \b
    Examples:
      # Basic run, output and report named automatically
      topo-tools name-clean admin3.parquet

    \b
      # Explicit level columns and output names
      topo-tools name-clean admin3.parquet admin3_clean.parquet \\
        admin3_name_issues.parquet --name-field adm{n}_name --code-field adm{n}_code
    """
    logger.info("--debug=%s", debug)
    try:
        _name_clean(
            input_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="package-polygons")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help='Path for the issues report of each level. It must contain "{n}", '
    "replaced by each level number. Defaults to each level's output with an "
    '"_issues" suffix.',
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--output-name-field",
    envvar="OUTPUT_NAME_FIELD",
    default=None,
    help="Rename the --name-field columns to this pattern in every output, "
    "e.g. 'adm{n}_label'. Needs --name-field and --code-field.",
)
@click.option(
    "--output-code-field",
    envvar="OUTPUT_CODE_FIELD",
    default=None,
    help="Rename the --code-field columns to this pattern in every output, "
    "e.g. 'adm{n}_pcode'. Needs --name-field and --code-field.",
)
@click.option(
    "--aggregation",
    "aggregations",
    envvar="AGGREGATIONS",
    multiple=True,
    help="How to combine a column whose values differ inside one unit, as "
    "'column=function', where function is sum, min, max, avg or first. By "
    "default numbers are summed and other columns dropped. Repeat for more "
    "columns.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "dissolve", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def package_polygons(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    issues_file: str | None,
    name_field: str | None,
    code_field: str | None,
    output_name_field: str | None,
    output_code_field: str | None,
    aggregations: tuple[str, ...],
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Merge a polygon layer into one layer for each coarser admin level.

    Units are dissolved by their code at every admin level found in the
    file. OUTPUT_FILE must contain "{n}", replaced by each level number.
    Without it, each level is written to INPUT_FILE with an "_admin{n}"
    suffix. The finest level is skipped when its path would be INPUT_FILE
    itself.

    \b
    Examples:
      # Default naming: input_admin1.geojson, input_admin2.geojson, ...
      topo-tools package-polygons admin3.geojson

    \b
      # Choose the output names and the level columns
      topo-tools package-polygons admin3.geojson "level_{n}.geojson" \\
        --name-field adm{n}_name --code-field adm{n}_pcode
    """
    logger.info("--debug=%s", debug)
    try:
        _package_polygons(
            input_file,
            output_file,
            issues_file,
            name_field=name_field,
            code_field=code_field,
            output_name_field=output_name_field,
            output_code_field=output_code_field,
            aggregations=_parse_aggregations(aggregations),
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="package-points")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--depth-column",
    envvar="DEPTH_COLUMN",
    default="adm_lvl",
    show_default=True,
    help="Name of the added column holding each point's admin level.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "points", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def package_points(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Make one label point for each admin unit, at every level, in one file.

    Each point is the spot inside the unit farthest from its edges, so it
    always falls inside the unit, unlike a centroid. OUTPUT_FILE defaults to
    INPUT_FILE with a "_points" suffix.

    \b
    Examples:
      topo-tools package-points admin3.geojson
    """
    logger.info("--debug=%s", debug)
    try:
        _package_points(
            input_file,
            Path(output_file) if output_file is not None else None,
            name_field,
            code_field,
            depth_column=depth_column,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="package-lines")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--depth-column",
    envvar="DEPTH_COLUMN",
    default="adm_lvl",
    show_default=True,
    help="Name of the added column holding the coarsest admin level each "
    "boundary line belongs to.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "boundaries", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def package_lines(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Make one line layer of admin boundaries, each line drawn once.

    Lines between two units and along the outer edge are included, each
    tagged with the coarsest admin level it belongs to. OUTPUT_FILE defaults
    to INPUT_FILE with a "_lines" suffix.

    \b
    Examples:
      topo-tools package-lines admin3.geojson
    """
    logger.info("--debug=%s", debug)
    try:
        _package_lines(
            input_file,
            Path(output_file) if output_file is not None else None,
            name_field,
            code_field,
            depth_column=depth_column,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command()
@click.argument("input_file", envvar="INPUT_FILE")
@click.option(
    "--output",
    "output",
    envvar="OUTPUT",
    default=None,
    help='Output path containing "{x}", replaced by "admin{n}", "points" or '
    '"lines" for each output. Without it, each tool\'s default name is used.',
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--output-name-field",
    envvar="OUTPUT_NAME_FIELD",
    default=None,
    help="Rename the --name-field columns to this pattern in every output, "
    "e.g. 'adm{n}_label'. Needs --name-field and --code-field.",
)
@click.option(
    "--output-code-field",
    envvar="OUTPUT_CODE_FIELD",
    default=None,
    help="Rename the --code-field columns to this pattern in every output, "
    "e.g. 'adm{n}_pcode'. Needs --name-field and --code-field.",
)
@click.option(
    "--aggregation",
    "aggregations",
    envvar="AGGREGATIONS",
    multiple=True,
    help="How package-polygons combines a column whose values differ inside "
    "one unit, as 'column=function', where function is sum, min, max, avg or "
    "first. By default numbers are summed and other columns dropped. Repeat "
    "for more columns.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
def package(  # noqa: PLR0913, PLR0917
    input_file: str,
    output: str | None,
    name_field: str | None,
    code_field: str | None,
    output_name_field: str | None,
    output_code_field: str | None,
    aggregations: tuple[str, ...],
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
) -> None:
    """Run package-polygons, package-points and package-lines on one input.

    \b
    Examples:
      # Defaults for all three outputs
      topo-tools package admin3.geojson

    \b
      # Choose where the outputs go
      topo-tools package admin3.geojson --output "web/{x}.geojson"
    """
    logger.info("--debug=%s", debug)
    try:
        _package(
            input_file,
            output,
            name_field,
            code_field,
            output_name_field=output_name_field,
            output_code_field=output_code_field,
            aggregations=_parse_aggregations(aggregations),
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command()
@click.argument("input_file", envvar="INPUT_FILE")
@click.option(
    "--output-dir",
    envvar="OUTPUT_DIR",
    default=None,
    help="Folder for the reports and the summary (default: beside INPUT_FILE).",
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
def validate(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_dir: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
) -> None:
    """Check one layer with every detect tool and summarize the results.

    Runs schema-detect, topo-detect, code-detect and name-detect on
    INPUT_FILE, each writing its own report, then writes a summary with one
    row per stage and kind. A stage that fails is recorded in the summary
    and the others still run; code-detect and name-detect are skipped when
    schema-detect cannot detect the levels. Exits with status 1 when any
    report has an error or a stage fails; warnings alone exit 0.

    \b
    Examples:
      # Reports and summary beside the input
      topo-tools validate admin3.parquet

    \b
      # Reports in their own folder, explicit level columns
      topo-tools validate admin3.parquet --output-dir checks \\
        --name-field adm{n}_name --code-field adm{n}_pcode
    """
    logger.info("--debug=%s", debug)
    try:
        errors = _validate(
            input_file,
            output_dir,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    if errors:
        raise SystemExit(1)


@cli.command(name="topo-clean")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help="Path for the issues report. Defaults to OUTPUT_FILE with an "
    '"_issues" suffix.',
)
@click.option(
    "--maximum-gap-width",
    envvar="MAXIMUM_GAP_WIDTH",
    type=str,
    default=None,
    help="Which gaps to fill: 'thin' for thin, sliver-shaped gaps of any "
    "width, 'all' for every gap found, or a width in decimal degrees (not "
    "meters). By default only tiny gaps from rounding errors are filled.",
)
@click.option(
    "--snapping-distance",
    envvar="SNAPPING_DISTANCE",
    type=str,
    default=None,
    help="Distance in decimal degrees within which nearby vertices are "
    "joined (default: 0.00000001). Only change it if cleaning fails on "
    "nearly touching edges.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "issues", "clean", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def topo_clean(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    issues_file: str | None,
    maximum_gap_width: str | None,
    snapping_distance: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Find and fix gaps and overlaps between the polygons of one layer.

    The fixes made are listed in an issues report for review. OUTPUT_FILE
    defaults to INPUT_FILE with a "_cleaned" suffix.

    \b
    Examples:
      # Basic run: fills only tiny gaps from rounding errors
      topo-tools topo-clean example.geojson

    \b
      # Fill thin, sliver-shaped gaps of any width
      topo-tools topo-clean example.gpkg --maximum-gap-width thin

    \b
      # Fill every detected gap, not just slivers
      topo-tools topo-clean example.gpkg --maximum-gap-width all

    \b
      # Fill gaps up to about 0.0001 degrees wide (about 11 m at the equator)
      topo-tools topo-clean example.parquet --maximum-gap-width 0.0001

    \b
      # Choose the output and the issues report, which lists how each problem
      # was fixed
      topo-tools topo-clean admin2.geojson admin2_cleaned.geojson \\
        --issues-file admin2_cleaned_issues.geojson
    """
    logger.info(
        "--maximum-gap-width=%s --snapping-distance=%s --debug=%s",
        maximum_gap_width,
        snapping_distance,
        debug,
    )
    try:
        _topo_clean(
            input_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            maximum_gap_width=maximum_gap_width,
            snapping_distance=snapping_distance,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command()
@click.argument("old_file", envvar="OLD_FILE")
@click.argument("new_file", envvar="NEW_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--overlay-file",
    envvar="OVERLAY_FILE",
    default=None,
    help="Path for the map layer of changes. Defaults to OUTPUT_FILE with an "
    '"_overlay" suffix.',
)
@click.option(
    "--tau-match",
    envvar="TAU_MATCH",
    type=float,
    default=TAU_MATCH_DEFAULT,
    show_default=True,
    help="Smallest share of a unit's area that must overlap another unit for "
    "the two to count as related.",
)
@click.option(
    "--tau-same",
    envvar="TAU_SAME",
    type=float,
    default=TAU_SAME_DEFAULT,
    show_default=True,
    help="Smallest overlap, as shared area divided by combined area, for a "
    "matched pair to count as the same shape rather than modified.",
)
@click.option(
    "--link-by-code",
    envvar="LINK_BY_CODE",
    is_flag=True,
    help="Also match units that have the same code in both versions, when "
    "that code is unique.",
)
@click.option(
    "--link-by-name",
    envvar="LINK_BY_NAME",
    is_flag=True,
    help="Also match units that have the same name in both versions, when "
    "that name is unique.",
)
@click.option(
    "--link-mode",
    envvar="LINK_MODE",
    type=click.Choice(["either", "both"]),
    default="either",
    show_default=True,
    help="With both --link-by-code and --link-by-name, match on either one "
    "or only on both.",
)
@click.option(
    "--code-column-a",
    envvar="CODE_COLUMN_A",
    default=None,
    help="Code column in OLD_FILE. Detected from the data if omitted.",
)
@click.option(
    "--code-column-b",
    envvar="CODE_COLUMN_B",
    default=None,
    help="Code column in NEW_FILE. Detected from the data if omitted.",
)
@click.option(
    "--name-column-a",
    envvar="NAME_COLUMN_A",
    default=None,
    help="Name column in OLD_FILE. Detected from the data if omitted.",
)
@click.option(
    "--name-column-b",
    envvar="NAME_COLUMN_B",
    default=None,
    help="Name column in NEW_FILE. Detected from the data if omitted.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "overlap", "classify", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def change(  # noqa: PLR0913, PLR0917
    old_file: str,
    new_file: str,
    output_file: str | None,
    overlay_file: str | None,
    tau_match: float,
    tau_same: float,
    link_by_code: bool,  # noqa: FBT001
    link_by_name: bool,  # noqa: FBT001
    link_mode: str,
    code_column_a: str | None,
    code_column_b: str | None,
    name_column_a: str | None,
    name_column_b: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Compare two versions of a polygon layer and list what changed.

    Each unit is classed as unchanged, renamed, modified, relocated, split,
    merged, complex, created or removed. OLD_FILE is the previous version and
    NEW_FILE the new one. OUTPUT_FILE, the changelog table (CSV or Parquet),
    defaults to both file names combined with a "_changelog" suffix. A map
    layer colored by type of change is written next to it.

    \b
    Examples:
      # Basic run, matching units by overlap only
      topo-tools change admin2_2020.geojson admin2_2024.geojson

    \b
      # Also match units that keep the same p-code
      topo-tools change old.gpkg new.gpkg --link-by-code \\
        --code-column-a adm2_pcode --code-column-b adm2_pcode

    \b
      # Accept less overlap, for heavily redrawn boundaries
      topo-tools change old.parquet new.parquet --tau-match 0.6

    \b
      # Choose the changelog and the map layer of changes, for review
      topo-tools change old.gpkg new.gpkg changelog.csv --overlay-file overlay.gpkg
    """
    logger.info("--tau-match=%s --tau-same=%s --debug=%s", tau_match, tau_same, debug)
    try:
        _change(
            old_file,
            new_file,
            Path(output_file) if output_file is not None else None,
            Path(overlay_file) if overlay_file is not None else None,
            tau_match=tau_match,
            tau_same=tau_same,
            link_by_code=link_by_code,
            link_by_name=link_by_name,
            link_mode=link_mode,
            code_column_a=code_column_a,
            code_column_b=code_column_b,
            name_column_a=name_column_a,
            name_column_b=name_column_b,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="code-detect")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("issues_file", envvar="ISSUES_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "checks", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def code_detect(  # noqa: PLR0913, PLR0917
    input_file: str,
    issues_file: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Find problems in the unit codes of one coded layer.

    Checks every level's codes: blank codes, a code with more than one
    name, a finest-level code on more than one polygon, a code that does
    not start with its parent's code, and a code shaped unlike the rest of
    its level. Writes the problems found without changing the layer, even
    when there are none. ISSUES_FILE defaults to INPUT_FILE with a
    "_code_issues" suffix, as Parquet with each unit's geometry, or CSV for
    a .csv name.

    \b
    Examples:
      # Basic run, report named automatically
      topo-tools code-detect admin3.parquet

    \b
      # Explicit level columns
      topo-tools code-detect admin3.parquet admin3_code_issues.parquet \\
        --name-field adm{n}_name --code-field adm{n}_pcode
    """
    logger.info("--debug=%s", debug)
    try:
        _code_detect(
            input_file,
            Path(issues_file) if issues_file is not None else None,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="code-create")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.argument("issues_file", envvar="ISSUES_FILE", required=False, default=None)
@click.option(
    "--root-code",
    envvar="ROOT_CODE",
    required=True,
    help=(
        "Code at the start of every code, e.g. a country code. With "
        "--code-field, also written to level 0's code column."
    ),
)
@click.option(
    "--delimiter",
    envvar="DELIMITER",
    required=True,
    help="One character between the parts of a code, or '' for none.",
)
@click.option(
    "--min-width",
    envvar="MIN_WIDTH",
    required=True,
    help="How many digits each level's number is padded to with zeros: one "
    "width for all levels (3), one per level from coarsest (2,2,4), or auto "
    "for as many as each level needs.",
)
@click.option(
    "--source-codes",
    envvar="SOURCE_CODES",
    type=click.Choice(["replace", "embed", "copy"]),
    default="replace",
    show_default=True,
    help="What to do with each level's existing code: replace it with a new "
    "number, embed it as that level's part of the new code (with no "
    "delimiter, whole-number codes are zero-padded to --min-width or the "
    "widest code), or copy it to a new column (adm1_code to adm1_code1) and "
    "then replace it.",
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "assign", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def code_create(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    issues_file: str | None,
    root_code: str,
    delimiter: str,
    min_width: str,
    source_codes: str,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Give every unit a new hierarchical code, numbered within its parent.

    Use it when there is no previous release to keep codes from, or with
    --source-codes embed to keep the source codes inside the new codes. The new
    codes replace each level's code column. OUTPUT_FILE defaults to
    INPUT_FILE with a "_coded" suffix. ISSUES_FILE lists parents with more
    children than the code width allows. It defaults to OUTPUT_FILE with an
    "_issues" suffix and is only written when there are any.

    \b
    Examples:
      # Basic run, levels detected from the data
      topo-tools code-create admin2.geojson --root-code AFG --delimiter . \\
        --min-width 3

    \b
      # Name the level columns, when detection is unsure
      topo-tools code-create admin2.geojson --root-code AFG --delimiter . \\
        --min-width 3 --code-field adm{n}_code --name-field adm{n}_name

    \b
      # Keep existing codes inside the new code, no delimiter (AF01, AF0101, ...)
      topo-tools code-create admin2.geojson --root-code AF --delimiter '' \\
        --min-width 2 --source-codes embed --code-field adm{n}_code \\
        --name-field adm{n}_name
    """
    logger.info("--debug=%s", debug)
    try:
        _code_create(
            input_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            root_code=root_code,
            delimiter=delimiter,
            min_width=min_width,
            source_codes=source_codes,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="code-update")
@click.argument("old_file", envvar="OLD_FILE")
@click.argument("new_file", envvar="NEW_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.argument("changelog_file", envvar="CHANGELOG_FILE", required=False, default=None)
@click.option(
    "--root-code",
    envvar="ROOT_CODE",
    default=None,
    help="Code at the start of every code. Detected from OLD_FILE's codes if omitted.",
)
@click.option(
    "--delimiter",
    envvar="DELIMITER",
    default=None,
    help="One character between the parts of a code, or '' for none. "
    "Detected from OLD_FILE's codes if omitted.",
)
@click.option(
    "--min-width",
    envvar="MIN_WIDTH",
    default=None,
    help="How many digits each level's number is padded to with zeros: 3, "
    "2,2,4 or auto. Detected from OLD_FILE's codes if omitted.",
)
@click.option(
    "--name-field-a",
    envvar="NAME_FIELD_A",
    default=None,
    help="Name column of each level in OLD_FILE, with {n} for the level "
    "number, e.g. 'adm{n}_name'. Give it with --code-field-a. Without both, "
    "levels are detected from the data.",
)
@click.option(
    "--code-field-a",
    envvar="CODE_FIELD_A",
    default=None,
    help="Code column of each level in OLD_FILE, with {n} for the level "
    "number, e.g. 'adm{n}_pcode'. Give it with --name-field-a. Without both, "
    "levels are detected from the data.",
)
@click.option(
    "--name-field-b",
    envvar="NAME_FIELD_B",
    default=None,
    help="Name column of each level in NEW_FILE, with {n} for the level "
    "number. Give it with --code-field-b. Without both, levels are detected "
    "from the data.",
)
@click.option(
    "--code-field-b",
    envvar="CODE_FIELD_B",
    default=None,
    help="Code column of each level in NEW_FILE, with {n} for the level "
    "number. A level with names but no code column gets codes made from its "
    "names. Give it with --name-field-b. Without both, levels are detected "
    "from the data.",
)
@click.option(
    "--code-column-a",
    envvar="CODE_COLUMN_A",
    default=None,
    help="Code column in OLD_FILE used by --link-by-code. Defaults to "
    "each level's code column.",
)
@click.option(
    "--code-column-b",
    envvar="CODE_COLUMN_B",
    default=None,
    help="Code column in NEW_FILE used by --link-by-code. Defaults to "
    "each level's code column.",
)
@click.option(
    "--name-column-a",
    envvar="NAME_COLUMN_A",
    default=None,
    help="Name column in OLD_FILE used by --link-by-name. Defaults to "
    "each level's name column.",
)
@click.option(
    "--name-column-b",
    envvar="NAME_COLUMN_B",
    default=None,
    help="Name column in NEW_FILE used by --link-by-name. Defaults to "
    "each level's name column.",
)
@click.option(
    "--tau-match",
    envvar="TAU_MATCH",
    type=float,
    default=TAU_MATCH_DEFAULT,
    show_default=True,
    help="Smallest share of a unit's area that must overlap another unit for "
    "the two to count as related.",
)
@click.option(
    "--tau-same",
    envvar="TAU_SAME",
    type=float,
    default=TAU_SAME_DEFAULT,
    show_default=True,
    help="Smallest overlap, as shared area divided by combined area, for a "
    "matched pair to count as the same shape rather than modified.",
)
@click.option(
    "--link-by-code",
    envvar="LINK_BY_CODE",
    is_flag=True,
    help="Also match units that have the same code in both versions, when "
    "that code is unique.",
)
@click.option(
    "--link-by-name",
    envvar="LINK_BY_NAME",
    is_flag=True,
    help="Also match units that have the same name in both versions, when "
    "that name is unique.",
)
@click.option(
    "--link-mode",
    envvar="LINK_MODE",
    type=click.Choice(["either", "both"]),
    default="either",
    show_default=True,
    help="With both --link-by-code and --link-by-name, match on either one "
    "or only on both.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "process", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def code_update(  # noqa: PLR0913, PLR0917
    old_file: str,
    new_file: str,
    output_file: str | None,
    changelog_file: str | None,
    root_code: str | None,
    delimiter: str | None,
    min_width: str | None,
    name_field_a: str | None,
    code_field_a: str | None,
    name_field_b: str | None,
    code_field_b: str | None,
    code_column_a: str | None,
    code_column_b: str | None,
    name_column_a: str | None,
    name_column_b: str | None,
    tau_match: float,
    tau_same: float,
    link_by_code: bool,  # noqa: FBT001
    link_by_name: bool,  # noqa: FBT001
    link_mode: str,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Code a new release so units that carry over keep their previous codes.

    OLD_FILE is the previous release, already coded. NEW_FILE is the new
    release. Unchanged units keep their codes, changed units get new ones,
    and no code is ever given to a different unit. OUTPUT_FILE, the new
    release with codes, defaults to NEW_FILE with a "_coded" suffix.
    CHANGELOG_FILE, a table of what happened to each code, defaults to
    OUTPUT_FILE with a "_changelog" suffix and is always written.

    \b
    Examples:
      # Basic run, code format and levels detected from OLD_FILE
      topo-tools code-update admin1_old.geojson admin1_new.geojson

    \b
      # Also match units by a shared source ID, for units that moved
      topo-tools code-update old.gpkg new.gpkg --link-by-code \\
        --code-column-a srcid --code-column-b srcid
    """
    logger.info("--debug=%s", debug)
    try:
        _code_update(
            old_file,
            new_file,
            Path(output_file) if output_file is not None else None,
            Path(changelog_file) if changelog_file is not None else None,
            root_code=root_code,
            delimiter=delimiter,
            min_width=min_width,
            name_field_a=name_field_a,
            code_field_a=code_field_a,
            name_field_b=name_field_b,
            code_field_b=code_field_b,
            code_column_a=code_column_a,
            code_column_b=code_column_b,
            name_column_a=name_column_a,
            name_column_b=name_column_b,
            tau_match=tau_match,
            tau_same=tau_same,
            link_by_code=link_by_code,
            link_by_name=link_by_name,
            link_mode=link_mode,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="edge-match")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("overlay_file", envvar="OVERLAY_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--input",
    "extra_inputs",
    envvar="EXTRA_INPUTS",
    multiple=True,
    help=(
        "Another input file to process together with INPUT_FILE. Repeat it "
        "or separate files with commas."
    ),
)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help="Path for the issues report. Defaults to OUTPUT_FILE with an "
    '"_issues" suffix.',
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "assign", "groups", "clip", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
@click.option(
    "--match-column",
    envvar="MATCH_COLUMN",
    default=None,
    help=(
        "Column in both layers, such as a p-code, used to match input "
        "polygons to overlay polygons. It wins over overlap where the two "
        "disagree. Can't be combined with --overlay-match-column or "
        "--input-match-column."
    ),
)
@click.option(
    "--overlay-match-column",
    envvar="OVERLAY_MATCH_COLUMN",
    default=None,
    help="Matching column in the overlay, when its name differs from the "
    "input's. Give it with --input-match-column.",
)
@click.option(
    "--input-match-column",
    envvar="INPUT_MATCH_COLUMN",
    default=None,
    help="Matching column in the input, when its name differs from the "
    "overlay's. Give it with --overlay-match-column.",
)
@_add_merge_options
@click.option(
    "--assign",
    envvar="ASSIGN",
    type=click.Choice(["auto", "one", "many"]),
    default="auto",
    show_default=True,
    help=(
        "How input polygons are assigned to overlay polygons. one: the whole "
        "input file goes to the overlay polygon most of its polygons overlap. "
        "many: each input polygon goes to the overlay polygon it overlaps "
        "most, for an input file spanning many overlay polygons, e.g. an admin4 "
        "layer fitted into many admin3 units. auto: one, switching to many "
        "when fewer than half the input polygons overlap the winner. The mode "
        "used is always logged. many only works with a single input file."
    ),
)
@_add_fill_options
def edge_match(  # noqa: PLR0913, PLR0917
    input_file: str,
    overlay_file: str,
    output_file: str | None,
    extra_inputs: tuple[str, ...],
    issues_file: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
    match_column: str | None,
    overlay_match_column: str | None,
    input_match_column: str | None,
    merge: bool,  # noqa: FBT001
    overlay_include: str | None,
    overlay_exclude: str | None,
    input_include: str | None,
    input_exclude: str | None,
    prefer: str | None,
    assign: str,
    fill_schema: bool,  # noqa: FBT001
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
) -> None:
    """Fit an input layer into the polygons of a coarser overlay layer.

    Each input file (or, with --assign many, each input polygon) is
    matched to the overlay polygon it overlaps most, extended to fill gaps,
    clipped to that polygon, then stitched so the edges line up.
    OUTPUT_FILE defaults to INPUT_FILE with a "_matched" suffix. It's
    required when INPUT_FILE is a pattern matching more than one file, or
    when --input is given.

    \b
    Examples:
      # Fit an admin4 layer into a single country boundary
      topo-tools edge-match adm4.geojson adm0.geojson

    \b
      # Fit admin3 into admin2, each group of units into its own admin2 unit
      topo-tools edge-match adm3.gpkg adm2.gpkg adm3_matched.gpkg

    \b
      # Fit several countries' admin1 layers into one shared overlay together
      topo-tools edge-match sen_adm1.parquet world_adm0.geojson out.parquet \\
        --input gmb_adm1.parquet,gnb_adm1.parquet

    \b
      # Match on a shared p-code column, overriding overlap where they disagree
      topo-tools edge-match adm3.gpkg adm2.gpkg --match-column pcode

    \b
      # Copy only iso_3 and adm0_name onto every matched input polygon
      topo-tools edge-match adm3.gpkg adm2.gpkg \\
        --merge --overlay-include iso_3,adm0_name

    \b
      # Keep the overlay's column when both layers have one with the same name
      topo-tools edge-match adm3.gpkg adm2.gpkg --merge --prefer overlay

    \b
      # An admin4 layer whose units fall in many different admin3 units
      topo-tools edge-match adm4.gpkg adm3.gpkg --assign many

    \b
      # Choose the output and the issues report
      topo-tools edge-match adm3.gpkg adm0.gpkg adm3_matched.gpkg \\
        --issues-file match_report.gpkg

    \b
      # Also fill empty finer admin columns from coarser ones before writing
      topo-tools edge-match adm3.gpkg adm0.gpkg adm3_matched.gpkg --fill-schema
    """
    logger.info("--debug=%s", debug)
    if any(ch in input_file for ch in "*?["):
        matches = sorted(glob.glob(input_file, recursive=True))  # noqa: PTH207 (arbitrary pattern, not anchored to one Path)
        if not matches:
            msg = f"no files matched: {input_file}"
            raise click.ClickException(msg)
        base_inputs = [Path(p) for p in matches]
    else:
        base_inputs = [input_file]
    all_inputs = base_inputs + list(_split_commas(extra_inputs))
    resolved_input: str | Path | list[str | Path] = (
        all_inputs[0] if len(all_inputs) == 1 else all_inputs
    )
    try:
        _edge_match(
            resolved_input,
            overlay_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
            match_column=match_column,
            overlay_match_column=overlay_match_column,
            input_match_column=input_match_column,
            merge=merge,
            overlay_include=_split_columns(overlay_include),
            overlay_exclude=_split_columns(overlay_exclude),
            input_include=_split_columns(input_include),
            input_exclude=_split_columns(input_exclude),
            prefer=prefer,
            assign=assign,
            fill_schema=fill_schema,
            name_field=name_field,
            code_field=code_field,
            depth_column=depth_column,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="edge-mosaic")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("overlay_file", envvar="OVERLAY_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--input",
    "extra_inputs",
    envvar="EXTRA_INPUTS",
    multiple=True,
    help=(
        "Another input file to process together with INPUT_FILE. Repeat it "
        "or separate files with commas."
    ),
)
@click.option(
    "--original",
    "original_files",
    envvar="ORIGINAL_FILES",
    multiple=True,
    help=(
        "The original layer before extension. It decides whether a piece "
        "cut off by clipping is merged into a neighbor. Without it, such pieces "
        "are only reported. Repeat it or separate files with commas."
    ),
)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help="Path for the issues report. Defaults to OUTPUT_FILE with an "
    '"_issues" suffix.',
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "assign", "clip", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
@click.option(
    "--match-column",
    envvar="MATCH_COLUMN",
    default=None,
    help=(
        "Column in both layers, such as a p-code, used to match input "
        "polygons to overlay polygons. It wins over overlap where the two "
        "disagree. Can't be combined with --overlay-match-column or "
        "--input-match-column."
    ),
)
@click.option(
    "--overlay-match-column",
    envvar="OVERLAY_MATCH_COLUMN",
    default=None,
    help="Matching column in the overlay, when its name differs from the "
    "input's. Give it with --input-match-column.",
)
@click.option(
    "--input-match-column",
    envvar="INPUT_MATCH_COLUMN",
    default=None,
    help="Matching column in the input, when its name differs from the "
    "overlay's. Give it with --overlay-match-column.",
)
@_add_merge_options
@_add_fill_options
def edge_mosaic(  # noqa: PLR0913, PLR0917
    input_file: str,
    overlay_file: str,
    output_file: str | None,
    extra_inputs: tuple[str, ...],
    original_files: tuple[str, ...],
    issues_file: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
    match_column: str | None,
    overlay_match_column: str | None,
    input_match_column: str | None,
    merge: bool,  # noqa: FBT001
    overlay_include: str | None,
    overlay_exclude: str | None,
    input_include: str | None,
    input_exclude: str | None,
    prefer: str | None,
    fill_schema: bool,  # noqa: FBT001
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
) -> None:
    """Fit an already-extended input layer into a new overlay layer.

    Like edge-match, but skips the extending step, for input already run
    through edge-extend. OUTPUT_FILE defaults to INPUT_FILE with a
    "_mosaicked" suffix. It's required when INPUT_FILE is a pattern matching
    more than one file, or when --input is given.

    \b
    Examples:
      # Clip an extended admin3 layer to a new admin0 boundary
      topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson

    \b
      # Clip every country's extended layer to a world admin0 layer
      topo-tools edge-mosaic "*/latest/adm2/extended.parquet" world_adm0.geojson \\
        out.parquet

    \b
      # List files instead of a pattern (repeat --input or use commas)
      topo-tools edge-mosaic afg.parquet world_adm0.geojson out.parquet \\
        --input ago.parquet,are.parquet

    \b
      # Match on a shared p-code column, overriding overlap where they disagree
      topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson --match-column pcode

    \b
      # Keep an overlay polygon's own shape where no input file covers it
      topo-tools edge-mosaic "*/latest/adm4/extended.parquet" world_adm0.geojson \\
        out.parquet --merge

    \b
      # Keep the overlay's column when both layers have one with the same name
      topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson \\
        --merge --prefer overlay

    \b
      # Re-clip an extended layer to a new overlay, with an issues report
      topo-tools edge-mosaic adm3_extended.parquet adm0_new.geojson \\
        adm3_mosaicked.parquet --issues-file mosaic_report.parquet
    """
    logger.info("--debug=%s", debug)
    if any(ch in input_file for ch in "*?["):
        matches = sorted(glob.glob(input_file, recursive=True))  # noqa: PTH207 (arbitrary pattern, not anchored to one Path)
        if not matches:
            msg = f"no files matched: {input_file}"
            raise click.ClickException(msg)
        base_inputs = [Path(p) for p in matches]
    else:
        base_inputs = [input_file]
    all_inputs = base_inputs + list(_split_commas(extra_inputs))
    resolved_input: str | Path | list[str | Path] = (
        all_inputs[0] if len(all_inputs) == 1 else all_inputs
    )
    try:
        _edge_mosaic(
            resolved_input,
            overlay_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
            match_column=match_column,
            overlay_match_column=overlay_match_column,
            input_match_column=input_match_column,
            merge=merge,
            overlay_include=_split_columns(overlay_include),
            overlay_exclude=_split_columns(overlay_exclude),
            input_include=_split_columns(input_include),
            input_exclude=_split_columns(input_exclude),
            prefer=prefer,
            fill_schema=fill_schema,
            name_field=name_field,
            code_field=code_field,
            depth_column=depth_column,
            original_paths=_split_commas(original_files) or None,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="edge-stitch")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--input",
    "extra_inputs",
    envvar="EXTRA_INPUTS",
    multiple=True,
    help=(
        "Another clipped file to stitch together with INPUT_FILE. Repeat it "
        "or separate files with commas."
    ),
)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help="Path for the issues report. Defaults to OUTPUT_FILE with an "
    '"_issues" suffix.',
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "clean", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
@_add_fill_options
def edge_stitch(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    extra_inputs: tuple[str, ...],
    issues_file: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
    fill_schema: bool,  # noqa: FBT001
    name_field: str | None,
    code_field: str | None,
    depth_column: str,
) -> None:
    """Close the seams between pieces of a layer that was clipped in parts.

    Removes the slivers and overlaps left where clipped pieces meet.
    OUTPUT_FILE defaults to INPUT_FILE with a "_stitched" suffix. It's
    required when INPUT_FILE is a pattern matching more than one file, or
    when --input is given.

    \b
    Examples:
      # Basic run, output name chosen automatically
      topo-tools edge-stitch tiled.geojson

    \b
      # Explicit output
      topo-tools edge-stitch tiled.gpkg stitched.gpkg

    \b
      # Stitch every clipped file into one output
      topo-tools edge-stitch "tmp/clipped/*.parquet" stitched.parquet

    \b
      # List files instead of a pattern (repeat --input or use commas)
      topo-tools edge-stitch afg.parquet stitched.parquet \\
        --input ago.parquet,are.parquet

    \b
      # Stop with an error if the output already exists
      topo-tools edge-stitch tiled.parquet stitched.parquet --overwrite=false
    """
    logger.info("--debug=%s", debug)
    if any(ch in input_file for ch in "*?["):
        matches = sorted(glob.glob(input_file, recursive=True))  # noqa: PTH207 (arbitrary pattern, not anchored to one Path)
        if not matches:
            msg = f"no files matched: {input_file}"
            raise click.ClickException(msg)
        base_inputs = [Path(p) for p in matches]
    else:
        base_inputs = [input_file]
    all_inputs = base_inputs + list(_split_commas(extra_inputs))
    resolved_input: str | Path | list[str | Path] = (
        all_inputs[0] if len(all_inputs) == 1 else all_inputs
    )
    try:
        _edge_stitch(
            resolved_input,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
            fill_schema=fill_schema,
            name_field=name_field,
            code_field=code_field,
            depth_column=depth_column,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="schema-detect")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("issues_file", envvar="ISSUES_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "levels", "checks", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def schema_detect(  # noqa: PLR0913, PLR0917
    input_file: str,
    issues_file: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Find problems in the column schema and hierarchy of one admin layer.

    Checks that admin levels can be detected with none skipped, that every
    level has the same set of columns named the same way, that each code
    sits under exactly one parent code, and that no code has a blank
    parent. Writes the problems found without changing the layer, even
    when there are none. ISSUES_FILE defaults to INPUT_FILE with a
    "_schema_issues" suffix, as Parquet, or CSV for a .csv name.

    \b
    Examples:
      # Basic run, report named automatically
      topo-tools schema-detect admin3.parquet

    \b
      # Explicit level columns
      topo-tools schema-detect admin3.parquet admin3_schema_issues.parquet \\
        --name-field adm{n}_name --code-field adm{n}_pcode
    """
    logger.info("--debug=%s", debug)
    try:
        _schema_detect(
            input_file,
            Path(issues_file) if issues_file is not None else None,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="schema-fill")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "fill", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
@click.option(
    "--depth-column",
    envvar="DEPTH_COLUMN",
    default="adm_lvl",
    show_default=True,
    help="Name of the added column holding each row's own admin level, "
    "taken before filling.",
)
def schema_fill(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    name_field: str | None,
    code_field: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
    depth_column: str,
) -> None:
    """Fill each row's empty finer admin columns from its coarser ones.

    A row for an admin2 unit in an admin4 file gets its admin2 code and
    name copied into the admin3 and admin4 columns. A column for the row's
    own admin level ("adm_lvl") is added first, so filled rows can be told
    apart. A missing value at a row's own level is left empty.

    \b
    Examples:
      # Basic run, levels detected from the data
      topo-tools schema-fill admin4.geojson
    """
    logger.info("--debug=%s", debug)
    try:
        _schema_fill(
            input_file,
            Path(output_file) if output_file is not None else None,
            name_field=name_field,
            code_field=code_field,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
            depth_column=depth_column,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="schema-join")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("join_file", envvar="JOIN_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--issues-output",
    envvar="ISSUES_OUTPUT",
    default=None,
    help="Path for the issues report. Defaults to OUTPUT_FILE with an "
    '"_issues" suffix.',
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Name column of each level, with {n} for the level number, e.g. "
    "'adm{n}_name'. Give it with --code-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Code column of each level, with {n} for the level number, e.g. "
    "'adm{n}_code'. Give it with --name-field. Without both, levels are "
    "detected from the data.",
)
@click.option(
    "--min-overlap",
    envvar="MIN_OVERLAP",
    type=float,
    default=MIN_OVERLAP_DEFAULT,
    show_default=True,
    help=(
        "Report an input polygon when its best-matching join polygon covers "
        "less than this share of its area."
    ),
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "assign", "join", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def schema_join(  # noqa: PLR0913, PLR0917
    input_file: str,
    join_file: str,
    output_file: str | None,
    issues_output: str | None,
    name_field: str | None,
    code_field: str | None,
    min_overlap: float,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Copy admin columns from a join layer onto the input polygons they overlap.

    Each input polygon takes the columns of the join polygon it overlaps
    most. Geometry is not changed. When a column already has a different
    value, both are kept: the join layer's value goes in a new numbered
    column (adm2_name1).

    \b
    Examples:
      # Copy admin2 columns onto an admin3 layer
      topo-tools schema-join admin3.geojson admin2.geojson

    \b
      # Build up a full hierarchy one level at a time, coarsest first
      topo-tools schema-join admin2.parquet admin1.parquet admin2_join.parquet
      topo-tools schema-join admin3.parquet admin2_join.parquet admin3_join.parquet
    """
    logger.info("--debug=%s", debug)
    try:
        _schema_join(
            input_file,
            join_file,
            Path(output_file) if output_file is not None else None,
            issues_path=Path(issues_output) if issues_output is not None else None,
            name_field=name_field,
            code_field=code_field,
            min_overlap=min_overlap,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="schema-map")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--csv",
    "csv_input",
    envvar="CSV",
    default=None,
    help="Apply this crosswalk CSV, usually an edited one, instead of making "
    "one. Output columns follow its row order.",
)
@click.option(
    "--csv-output",
    envvar="CSV_OUTPUT",
    default=None,
    help="Path for the crosswalk CSV. Defaults to INPUT_FILE with a "
    '"_crosswalk.csv" ending.',
)
@click.option(
    "--map-only",
    envvar="MAP_ONLY",
    is_flag=True,
    help="Write only the crosswalk CSV, not the renamed layer.",
)
@click.option(
    "--name-field",
    envvar="NAME_FIELD",
    default=None,
    help="Target name for each level's name column, with {n} for the level "
    "number. Give it with --code-field. Default: 'adm{n}_name'.",
)
@click.option(
    "--code-field",
    envvar="CODE_FIELD",
    default=None,
    help="Target name for each level's code column, with {n} for the level "
    "number. Give it with --name-field. Default: 'adm{n}_code'.",
)
@click.option(
    "--level",
    envvar="LEVEL",
    type=click.IntRange(min=0),
    default=None,
    help="Admin level of the file's finest units, used to number the levels. "
    "By default the coarsest level is 1, or 0 for a column with a single "
    "country value.",
)
@click.option(
    "--layer",
    envvar="LAYER",
    default=None,
    help="Layer to read from a file with several layers, such as a FileGDB. "
    "Needed only when the file has more than one layer with geometry.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "map", "apply", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
def schema_map(  # noqa: PLR0913, PLR0917
    input_file: str,
    output_file: str | None,
    csv_input: str | None,
    csv_output: str | None,
    map_only: bool,  # noqa: FBT001
    name_field: str | None,
    code_field: str | None,
    level: int | None,
    layer: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
) -> None:
    """Rename a layer's admin columns to a standard schema, using a crosswalk.

    The admin levels and which columns hold codes or names are worked out
    from the data, not the column names. The result is written as a
    crosswalk CSV and applied. To adjust it, edit the CSV (change a target,
    blank it to drop the column, move rows to reorder) and run again with
    --csv. OUTPUT_FILE defaults to INPUT_FILE with a "_mapped" suffix.

    \b
    Examples:
      # Map and apply, output names chosen automatically
      topo-tools schema-map example.geojson

    \b
      # Apply an edited crosswalk
      topo-tools schema-map example.geojson --csv example_crosswalk.csv

    \b
      # Only write the crosswalk, for a file whose finest level is admin3
      topo-tools schema-map admin3.geojson --map-only --level 3

    \b
      # Use different target column names
      topo-tools schema-map example.geojson --name-field adm{n}_name \\
        --code-field adm{n}_pcode
    """
    logger.info("--debug=%s", debug)
    try:
        _schema_map(
            input_file,
            Path(output_file) if output_file is not None else None,
            csv_input=csv_input,
            csv_output=csv_output,
            map_only=map_only,
            name_field=name_field,
            code_field=code_field,
            level=level,
            layer=layer,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e


@cli.command(name="edge-clip")
@click.argument("input_file", envvar="INPUT_FILE")
@click.argument("overlay_file", envvar="OVERLAY_FILE")
@click.argument("output_file", envvar="OUTPUT_FILE", required=False, default=None)
@click.option(
    "--issues-file",
    envvar="ISSUES_FILE",
    default=None,
    help=(
        "Path for the issues report, written only with --match-column or "
        '--overlay-match-column. Defaults to OUTPUT_FILE with an "_issues" '
        "suffix."
    ),
)
@click.option(
    "--name",
    envvar="NAME",
    default=None,
    help="Name used for the run's working tables and temporary files.",
)
@click.option(
    "--overwrite",
    envvar="OVERWRITE",
    type=bool,
    default=True,
    show_default=True,
    help="Replace output files that already exist. Pass --overwrite=false to "
    "stop with an error instead.",
)
@click.option(
    "--threads",
    envvar="THREADS",
    type=int,
    default=None,
    help="Number of threads DuckDB uses (default: all CPU cores).",
)
@click.option(
    "--debug",
    envvar="DEBUG",
    is_flag=True,
    help="Keep intermediate tables, export them to Parquet, and log the time "
    "and memory each query takes.",
)
@click.option(
    "--tmp-dir",
    envvar="TMP_DIR",
    default=None,
    help="Folder for the working DuckDB database and intermediate files "
    "(default: a new temporary folder, deleted afterwards unless --debug is set).",
)
@click.option(
    "--step",
    envvar="STEP",
    type=click.Choice(["inputs", "assign", "clip", "outputs"]),
    default=None,
    help="Run only this step of the tool, for debugging.",
)
@click.option(
    "--match-column",
    envvar="MATCH_COLUMN",
    default=None,
    help=(
        "Column in both layers, such as a p-code, used to match input "
        "polygons to overlay polygons. It wins over overlap where the two "
        "disagree. Can't be combined with --overlay-match-column or "
        "--input-match-column."
    ),
)
@click.option(
    "--overlay-match-column",
    envvar="OVERLAY_MATCH_COLUMN",
    default=None,
    help="Matching column in the overlay, when its name differs from the "
    "input's. Give it with --input-match-column.",
)
@click.option(
    "--input-match-column",
    envvar="INPUT_MATCH_COLUMN",
    default=None,
    help="Matching column in the input, when its name differs from the "
    "overlay's. Give it with --overlay-match-column.",
)
@click.option(
    "--carry-column",
    "carry_columns",
    envvar="CARRY_COLUMNS",
    multiple=True,
    help=(
        "Overlay column to copy onto each matched input polygon. Repeat it or "
        "separate columns with commas."
    ),
)
@click.option(
    "--original",
    "original_file",
    envvar="ORIGINAL_FILE",
    default=None,
    help=(
        "The original layer before extension. It decides whether a piece "
        "cut off by clipping is merged into a neighbor. Without it, such pieces "
        "are only reported."
    ),
)
def edge_clip(  # noqa: PLR0913, PLR0917
    input_file: str,
    overlay_file: str,
    output_file: str | None,
    issues_file: str | None,
    name: str | None,
    overwrite: bool,  # noqa: FBT001
    threads: int | None,
    debug: bool,  # noqa: FBT001
    tmp_dir: str | None,
    step: str | None,
    match_column: str | None,
    overlay_match_column: str | None,
    input_match_column: str | None,
    carry_columns: tuple[str, ...],
    original_file: str | None,
) -> None:
    """Clip an input layer to the overlay polygon it overlaps most.

    The whole input file is matched to the one overlay polygon most of it
    falls in, then clipped to that polygon's shape. OUTPUT_FILE defaults to
    INPUT_FILE with a "_clipped" suffix.

    \b
    Examples:
      # Clip an input layer against an overlay layer
      topo-tools edge-clip input.parquet adm1.geojson

    \b
      # Explicit output
      topo-tools edge-clip input.parquet adm1.geojson clipped.parquet

    \b
      # Match on a shared p-code column, overriding overlap where they disagree
      topo-tools edge-clip input.parquet adm1.geojson --match-column pcode

    \b
      # Copy overlay columns onto every matched input polygon
      topo-tools edge-clip input.parquet adm1.geojson --carry-column iso_3,adm0_name
    """
    logger.info("--debug=%s", debug)

    try:
        _edge_clip(
            input_file,
            overlay_file,
            Path(output_file) if output_file is not None else None,
            Path(issues_file) if issues_file is not None else None,
            name=name,
            threads=threads,
            tmp_dir=tmp_dir,
            overwrite=overwrite,
            debug=debug,
            step=step,
            match_column=match_column,
            overlay_match_column=overlay_match_column,
            input_match_column=input_match_column,
            carry_columns=_split_commas(carry_columns) or None,
            original_path=original_file,
        )
    except (FileExistsError, RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e
