"""
Unit tests for the rolling z-score detector.

These only depend on stdlib, so `pytest tests/test_rolling_zscore.py`
works with zero project dependencies installed — useful as a quick
sanity check before setting up the full environment.
"""

import pytest

from detection.rolling_zscore import (
    MIN_OBSERVATIONS_BEFORE_SCORING,
    RollingZScoreDetector,
)


def test_warmup_never_flags():
    """Before MIN_OBSERVATIONS_BEFORE_SCORING readings, nothing should
    be flagged regardless of value — we don't have enough history to
    judge yet."""
    d = RollingZScoreDetector(window_size=20, threshold=3.0)
    for i in range(MIN_OBSERVATIONS_BEFORE_SCORING):
        result = d.score("1", 100.0 + i * 50)  # wildly varying on purpose
        assert result.is_anomaly is False
        assert result.score == 0.0


def test_clear_outlier_is_flagged():
    d = RollingZScoreDetector(window_size=20, threshold=3.0)
    for _ in range(15):
        d.score("1", 120.0)
    result = d.score("1", 900.0)
    assert result.is_anomaly is True
    assert result.score > 3.0


def test_normal_value_not_flagged():
    d = RollingZScoreDetector(window_size=20, threshold=3.0)
    values = [115, 118, 121, 119, 117, 122, 120, 116, 119, 121, 118, 120]
    for v in values:
        d.score("1", float(v))
    result = d.score("1", 119.0)
    assert result.is_anomaly is False


def test_zero_variance_window_does_not_crash():
    """If every recent reading is identical (e.g. a stuck feed value),
    std is 0. Division by zero must not crash the detector."""
    d = RollingZScoreDetector(window_size=15, threshold=3.0)
    for _ in range(12):
        d.score("G", 100.0)
    # an exact repeat of the flat value should not be flagged
    result = d.score("G", 100.0)
    assert result.is_anomaly is False
    # any deviation from a perfectly flat window is automatically extreme
    result2 = d.score("G", 105.0)
    assert result2.is_anomaly is True


def test_routes_are_scored_independently():
    """Route A's anomaly history must not affect route B's baseline."""
    d = RollingZScoreDetector(window_size=15, threshold=3.0)
    for _ in range(15):
        d.score("A", 800.0)  # route A's "normal" is very different from B's
    result_b = d.score("B", 100.0)
    assert result_b.score == 0.0  # B is still in its own warmup
    assert result_b.is_anomaly is False


def test_recovers_after_single_spike():
    """A rolling window means one outlier shouldn't permanently distort
    the baseline — the very next normal value should read as normal."""
    d = RollingZScoreDetector(window_size=20, threshold=3.0)
    for _ in range(15):
        d.score("1", 120.0)
    spike = d.score("1", 900.0)
    assert spike.is_anomaly is True
    recovery = d.score("1", 121.0)
    assert recovery.is_anomaly is False


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
