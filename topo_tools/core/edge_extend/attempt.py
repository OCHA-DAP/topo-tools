"""Retries points + voronoi stages with doubling distance on failure."""

import time
from decimal import Decimal
from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import Error as DuckDBError

from . import _03_points as points
from . import _04_voronoi as voronoi
from ._constants import DEFAULT_DISTANCE, MAX_POINTS, POINT_ESTIMATE_MIN_RATIO

logger = getLogger(__name__)

ATTEMPTS = 10


def main(
    conn: DuckDBPyConnection,
    name: str,
    *,
    distance: Decimal | None = None,
    max_points: int = MAX_POINTS,
    debug: bool = False,
) -> None:
    """Try to generate Voronoi polygons, doubling the distance threshold on failure.

    distance overrides the starting distance derived from the file's segments.
    """
    points.build_segments(conn, name)
    if distance is not None:
        effective_distance = Decimal(distance)
        logger.info("distance-calc: %s given=%s", name, effective_distance)
    else:
        effective_distance = _derived_distance(conn, name)

    try:
        for i in range(ATTEMPTS):
            d = effective_distance * 2**i
            estimate = points.estimate_count(conn, name, d)
            if estimate * POINT_ESTIMATE_MIN_RATIO > max_points:
                logger.info(
                    "skip: %s distance=%s, about %s points estimated (max %s)",
                    name,
                    d,
                    f"{estimate:,}",
                    f"{max_points:,}",
                )
                continue
            logger.info(
                "attempt %d: %s distance=%s, about %s points estimated",
                i + 1,
                name,
                d,
                f"{estimate:,}",
            )
            start = time.monotonic()
            try:
                points.main(conn, name, d, debug=debug)
                count = conn.execute(f'SELECT count(*) FROM "{name}_03b"').fetchall()[
                    0
                ][0]
                logger.info("points: %s %s built", name, f"{count:,}")
                _check_point_count(count, max_points)
                voronoi.main(conn, name, debug=debug)
            except voronoi.AssignmentError as e:
                error = f"{name} distance={d}: {e}"
                raise RuntimeError(error) from e
            except (RuntimeError, DuckDBError) as e:
                logger.warning("fail: %s distance=%s: %s", name, d, e)
            else:
                logger.info("voronoi: %s done in %.0fs", name, time.monotonic() - start)
                return
        error = f"{name} did not succeed generating voronoi polygons"
        logger.error(error)
        raise RuntimeError(error)
    finally:
        if not debug:
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03_tmp1"')
            conn.execute(f'DROP TABLE IF EXISTS "{name}_03a"')


def _derived_distance(conn: DuckDBPyConnection, name: str) -> Decimal:
    natural_res = conn.execute(f"""--sql
        SELECT median(seg_len) FROM "{name}_03_tmp1"
    """).fetchall()[0][0]
    if natural_res is None:
        logger.info("distance-calc: %s no real segments, using default", name)
        return DEFAULT_DISTANCE
    effective_distance = Decimal(str(min(float(DEFAULT_DISTANCE), natural_res)))
    logger.info(
        "distance-calc: %s natural_res=%s effective=%s",
        name,
        natural_res,
        effective_distance,
    )
    return effective_distance


def _check_point_count(count: int, max_points: int) -> None:
    if count > max_points:
        msg = f"too many points: {count:,}"
        raise RuntimeError(msg)
