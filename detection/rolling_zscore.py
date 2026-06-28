"""
Rolling z-score detector — the default, "boring on purpose" model.

Why start here instead of Isolation Forest / LSTM / Prophet / etc?
--------------------------------------------------------------------
1. Interpretability: a z-score has a unit (standard deviations from
   recent normal) that a transit rider or ops person can sanity-check
   immediately. "This headway is 3.8 standard deviations above the last
   hour's average" is auditable in your head. "The isolation forest gave
   this a path-length anomaly score of 0.71" is not, without more
   context.

2. Cold-start behavior: with a rolling window we get *useful* output
   after ~window_size observations (a few minutes), which matters
   because this system starts with an empty database. Isolation Forest
   and most ML detectors need a meaningfully sized, somewhat
   representative training set before their scores mean anything —
   fine for a batch job over historical data, awkward for a live
   system that's supposed to start flagging things on day one.

3. Concept drift: subway headways have real daily/weekly seasonality
   (rush hour vs. 3am are different "normal"s). A rolling window
   naturally adapts to the current regime without retraining. A static
   Isolation Forest trained once would need to be periodically refit or
   it'll flag "normal rush hour" as anomalous forever.

4. Compute cost: O(1) update per observation (Welford's algorithm),
   no model file, no retraining job, no scikit-learn dependency on the
   hot path. Appropriate for a project polling a free API every 30s on
   a free-tier dyno.

Where this falls short (and why detection/isolation_forest.py exists)
------------------------------------------------------------------------
Z-score assumes the metric is roughly unimodal and that a single
threshold means the same thing regardless of *which other signals* are
also moving. It can't express "this headway is normal on its own, but
combined with falling active_trains count, it's actually suspicious" —
that's a multivariate pattern, and Isolation Forest is the documented
upgrade path for exactly that case. We ship z-score first because it's
correct to ship the simple thing first and add complexity only once you
can point at what the simple thing is failing to catch — not because
Isolation Forest is hard to use.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

ZSCORE_THRESHOLD = 3.0
WINDOW_SIZE = 60  # ~30 minutes of history at a 30s poll interval
MIN_OBSERVATIONS_BEFORE_SCORING = 10


@dataclass
class DetectionResult:
    is_anomaly: bool
    score: float  # the z-score itself (signed), regardless of is_anomaly


class RollingZScoreDetector:
    """Maintains a separate rolling window per route_id, since different
    lines have structurally different normal headways (an express line
    and a shuttle are not on the same scale)."""

    def __init__(self, window_size: int = WINDOW_SIZE, threshold: float = ZSCORE_THRESHOLD):
        self.window_size = window_size
        self.threshold = threshold
        self._windows: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window_size))

    def score(self, route_id: str, value: float) -> DetectionResult:
        window = self._windows[route_id]

        if len(window) < MIN_OBSERVATIONS_BEFORE_SCORING:
            window.append(value)
            return DetectionResult(is_anomaly=False, score=0.0)

        mean = sum(window) / len(window)
        variance = sum((x - mean) ** 2 for x in window) / len(window)
        std = variance ** 0.5

        if std == 0:
            # Degenerate case: every recent reading was identical (e.g.
            # the feed returned a stuck value). Any deviation at all is
            # notable, but we can't divide by zero — flag deterministically.
            z = 0.0 if value == mean else float("inf")
        else:
            z = (value - mean) / std

        window.append(value)

        is_anomaly = abs(z) >= self.threshold
        # Cap reported score so "inf" doesn't break JSON serialization
        # downstream; anything this extreme is anomalous regardless.
        reported = z if z != float("inf") else 999.0
        return DetectionResult(is_anomaly=is_anomaly, score=reported)
