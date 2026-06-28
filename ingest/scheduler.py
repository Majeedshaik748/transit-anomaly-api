"""
The background job that makes this a *streaming* system rather than a
batch script: every POLL_INTERVAL_SECONDS, fetch the live feed, persist
each route's headway observation, then immediately run it through the
anomaly detector and store any flags.

This runs in the same process as the FastAPI app (via APScheduler's
BackgroundScheduler), which is the right call at this scale: one
process, one free-tier dyno, no message queue needed. The moment you
have multiple ingest workers or need the job to survive the API process
restarting independently, split this into its own worker — APScheduler
doesn't coordinate across processes, so don't run two of these against
the same DB without adding a lock.
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from detection.rolling_zscore import RollingZScoreDetector
from ingest.fetch_feed import poll_all_feeds
from storage.db import SessionLocal
from storage.models import Anomaly, MetricReading

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

_detector = RollingZScoreDetector()


def poll_and_store() -> None:
    # Imported lazily to avoid a circular import (api.main imports this
    # module to start the scheduler; this module would otherwise need to
    # import api.ws_broadcast at module load time).
    from api.ws_broadcast import BroadcastEvent, broadcaster

    observations = poll_all_feeds()
    if not observations:
        logger.warning("poll returned zero observations")
        return

    session = SessionLocal()
    try:
        for obs in observations:
            reading = MetricReading(
                route_id=obs.route_id,
                observed_at=obs.observed_at,
                headway_seconds=obs.headway_seconds,
                active_trains=obs.active_trains,
            )
            session.add(reading)
            session.flush()  # need reading.id before scoring

            result = _detector.score(obs.route_id, obs.headway_seconds)

            broadcaster.publish(
                BroadcastEvent(
                    type="reading",
                    payload={
                        "route_id": obs.route_id,
                        "observed_at": obs.observed_at.isoformat(),
                        "headway_seconds": obs.headway_seconds,
                        "active_trains": obs.active_trains,
                        "zscore": result.score,
                        "is_anomaly": result.is_anomaly,
                    },
                )
            )

            if result.is_anomaly:
                session.add(
                    Anomaly(
                        reading_id=reading.id,
                        route_id=obs.route_id,
                        method="rolling_zscore",
                        score=result.score,
                        headway_seconds=obs.headway_seconds,
                    )
                )
                broadcaster.publish(
                    BroadcastEvent(
                        type="anomaly",
                        payload={
                            "route_id": obs.route_id,
                            "observed_at": obs.observed_at.isoformat(),
                            "headway_seconds": obs.headway_seconds,
                            "method": "rolling_zscore",
                            "score": result.score,
                        },
                    )
                )
                logger.info(
                    "ANOMALY route=%s headway=%.0fs zscore=%.2f",
                    obs.route_id,
                    obs.headway_seconds,
                    result.score,
                )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("failed to persist poll results")
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        poll_and_store,
        "interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="gtfs_poll",
        max_instances=1,  # never let a slow poll overlap the next one
        coalesce=True,
    )
    scheduler.start()
    logger.info("scheduler started, polling every %ds", POLL_INTERVAL_SECONDS)
    return scheduler
