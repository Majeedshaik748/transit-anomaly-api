"""
Isolation Forest detector — the documented upgrade path from rolling
z-score, NOT the default.

When you'd actually reach for this
------------------------------------
Once you have enough history that a multivariate view pays off: e.g.
you want a single anomaly score that jointly considers headway_seconds
*and* active_trains *and* time-of-day, rather than three independent
z-scores you have to eyeball together. Isolation Forest handles that
combination natively and doesn't assume Gaussian-shaped "normal" — it
isolates points by how few random splits it takes to separate them from
the rest of the data, so it copes with skewed, multimodal distributions
that headway data actually has (most gaps cluster tight, with a long
right tail during disruptions — not symmetric, which a z-score quietly
assumes).

Why this is NOT what the live scheduler calls
-------------------------------------------------
Isolation Forest is a *batch* model: you fit it on a window of historical
rows, then score new points against that fit. It can't update itself one
point at a time the way the rolling z-score does, so using it on the hot
path means either:
  (a) refitting on every single poll (wasteful — O(n log n) refit every
      30s for marginal benefit), or
  (b) fitting periodically (e.g. nightly) and scoring against a stale
      model in between, which silently degrades as the regime shifts.

This module implements (b) as an explicit, separate batch job —
`refit_and_score()` — that you'd run on a schedule (e.g. once an hour via
a second APScheduler job, or a cron line) independent of the 30-second
ingest poll. It is intentionally not wired into ingest/scheduler.py so
that the live system's correctness never depends on sklearn being
installed or a refit having succeeded recently.

Run it manually to see it work:
    python -m detection.isolation_forest
"""

from __future__ import annotations

import logging

import numpy as np
from sklearn.ensemble import IsolationForest

from storage.db import SessionLocal
from storage.models import Anomaly, MetricReading

logger = logging.getLogger(__name__)

CONTAMINATION = 0.02  # prior belief: ~2% of readings are truly anomalous
MIN_ROWS_TO_FIT = 50


def _load_route_readings(session, route_id: str) -> list[MetricReading]:
    return (
        session.query(MetricReading)
        .filter(MetricReading.route_id == route_id)
        .order_by(MetricReading.observed_at.asc())
        .all()
    )


def _distinct_route_ids(session) -> list[str]:
    rows = session.query(MetricReading.route_id).distinct().all()
    return [r[0] for r in rows]


def refit_and_score(route_id: str) -> int:
    """Fit an Isolation Forest on all stored readings for one route and
    write Anomaly rows for any point it flags that isn't already
    flagged. Returns the number of new anomalies written.

    Features used: [headway_seconds, active_trains, hour_of_day_sin,
    hour_of_day_cos]. The sin/cos encoding of hour-of-day avoids the
    discontinuity you'd get from treating "23:00" and "00:00" as far
    apart on a linear 0-23 scale — they're one hour apart, and a
    cyclical encoding tells the model that.
    """
    session = SessionLocal()
    try:
        readings = _load_route_readings(session, route_id)
        if len(readings) < MIN_ROWS_TO_FIT:
            logger.info(
                "route %s has only %d readings, need >= %d to fit; skipping",
                route_id,
                len(readings),
                MIN_ROWS_TO_FIT,
            )
            return 0

        X = []
        for r in readings:
            hour = r.observed_at.hour + r.observed_at.minute / 60.0
            angle = 2 * np.pi * hour / 24.0
            X.append(
                [
                    r.headway_seconds,
                    float(r.active_trains),
                    np.sin(angle),
                    np.cos(angle),
                ]
            )
        X = np.array(X)

        model = IsolationForest(contamination=CONTAMINATION, random_state=42)
        # -1 = anomaly, 1 = normal (sklearn's convention)
        predictions = model.fit_predict(X)
        scores = model.score_samples(X)  # higher = more normal

        already_flagged = {
            a.reading_id
            for a in session.query(Anomaly.reading_id)
            .filter(Anomaly.route_id == route_id, Anomaly.method == "isolation_forest")
            .all()
        }

        new_count = 0
        for reading, pred, score in zip(readings, predictions, scores):
            if pred == -1 and reading.id not in already_flagged:
                session.add(
                    Anomaly(
                        reading_id=reading.id,
                        route_id=route_id,
                        method="isolation_forest",
                        score=float(-score),  # flip sign: higher = more anomalous
                        headway_seconds=reading.headway_seconds,
                    )
                )
                new_count += 1
        session.commit()
        logger.info("route %s: isolation forest flagged %d new anomalies", route_id, new_count)
        return new_count
    finally:
        session.close()


def refit_all_routes() -> None:
    session = SessionLocal()
    try:
        route_ids = _distinct_route_ids(session)
    finally:
        session.close()

    for route_id in route_ids:
        refit_and_score(route_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    refit_all_routes()
