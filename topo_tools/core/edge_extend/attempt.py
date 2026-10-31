"""Retries points + voronoi stages with doubling distance on failure."""

import time
from decimal import Decimal
from logging import getLogger

from duckdb import DuckDBPyConnection
from duckdb import Error as DuckDBError

from . import _03_points as points
from . import _04_voronoi as voronoi
from ._constants import DEFAULT_DISTANCE, MAX_POINTS

logger = getLogger(__name__)

ATTEMPTS = 10


def main(conn: DuckDBPyConnection, name: str, *, debug: bool = False) -> None:
    """Try to generate Voronoi polygons, doubling the distance threshold on failure.

    A distance whose estimated point count is over MAX_POINTS is skipped unbuilt.
    """
    points.build_segments(conn, name)
    effective_distance = _derived_distance(conn, name)
    skipped = 0

    try:
        for i in range(ATTEMPTS):
            d = effective_distance * 2**i
            estimate = points.estimate_count(conn, name, d)
            if estimate > MAX_POINTS:
                logger.info(
                    "skip: %s distance=%s, about %s points estimated (max %s)",
                    name,
                    d,
                    f"{estimate:,}",
                    f"{MAX_POINTS:,}",
                )
                skipped += 1
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
                _check_point_count(count)
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
        if skipped == ATTEMPTS:
            error += f": every distance estimated over {MAX_POINTS:,} points"
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


def _check_point_count(count: int) -> None:
    if count > MAX_POINTS:
        msg = f"too many points: {count:,}"
        raise RuntimeError(msg)
