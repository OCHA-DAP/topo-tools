"""Retries points + voronoi stages with doubling distance on failure."""

from decimal import Decimal
from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import Error as DuckDBError

from . import _03_points as points
from . import _04_voronoi as voronoi
from ._constants import DEFAULT_DISTANCE, MAX_POINTS

logger = getLogger(__name__)


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Try to generate Voronoi polygons, doubling the distance threshold on failure."""
    points.build_segments(conn, name)
    natural_res = conn.execute(f"""--sql
        SELECT median(seg_len) FROM "{name}_03_tmp1"
    """).fetchall()[0][0]

    if natural_res is None:
        effective_distance = DEFAULT_DISTANCE
        logger.info("distance-calc: %s no real segments, using default", name)
    else:
        effective_distance = Decimal(str(min(float(DEFAULT_DISTANCE), natural_res)))
        logger.info(
            "distance-calc: %s natural_res=%s effective=%s",
            name,
            natural_res,
            effective_distance,
        )

    try:
        for d in [effective_distance * 2**i for i in range(10)]:
            try:
                points.main(conn, name, d, debug=debug)
                count = conn.execute(f'SELECT count(*) FROM "{name}_03b"').fetchall()[
                    0
                ][0]
                _check_point_count(count)
                voronoi.main(conn, name, debug=debug)
            except (RuntimeError, DuckDBError) as e:
                logger.warning("fail: %s distance=%s: %s", name, d, e)
            else:
                return
        error = f"{name} did not succeed generating voronoi polygons"
        logger.error(error)
        raise RuntimeError(error)
    finally:
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp1"')
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')


def _check_point_count(count: int) -> None:
    if count > MAX_POINTS:
        msg = f"too many points: {count:,}"
        raise RuntimeError(msg)
