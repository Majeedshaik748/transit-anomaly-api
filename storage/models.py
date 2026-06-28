"""
Database schema for the transit anomaly detector.

Design notes
------------
We are NOT storing raw GTFS-RT protobuf blobs. We extract one derived
metric per (route, timestamp) on every poll: **headway gap**, i.e. the
number of seconds since the previous train on that route passed the same
"checkpoint" (we use the count of currently-active trips heading to a
terminal as a simple proxy, see ingest/fetch_feed.py for the exact
calculation).

Why headway gap and not raw arrival delay?
- MTA's "scheduled" arrival times in supplemented GTFS are themselves
  unstable in service-change periods, so delay-vs-schedule is noisy.
- Headway (the spacing between trains) is what riders actually feel,
  and irregular headway is the textbook definition of "service anomaly"
  in transit ops literature, regardless of whether the agency calls it
  a delay.

Two tables:
- `metric_readings`: one row per (route, poll time) — the raw time series.
- `anomalies`: one row per detected anomaly, pointing back at the reading
  that triggered it, with the score and method that flagged it. Kept
  separate so we can re-run different detectors over history without
  mutating the raw series.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MetricReading(Base):
    """One observation: at `observed_at`, route `route_id` had a headway
    gap of `headway_seconds` between its most recent two trains."""

    __tablename__ = "metric_readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(String(8), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    headway_seconds = Column(Float, nullable=False)
    active_trains = Column(Integer, nullable=False, default=0)

    anomalies = relationship("Anomaly", back_populates="reading")

    __table_args__ = (
        # Composite index: nearly every query is "give me route X's
        # recent history", ordered by time.
        Index("ix_route_time", "route_id", "observed_at"),
    )


class Anomaly(Base):
    """A flagged anomaly. method is 'zscore' or 'isolation_forest' so the
    frontend/README can show which detector caught what."""

    __tablename__ = "anomalies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reading_id = Column(Integer, ForeignKey("metric_readings.id"), nullable=False)
    route_id = Column(String(8), nullable=False, index=True)
    detected_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    method = Column(String(32), nullable=False)
    score = Column(Float, nullable=False)  # z-score or anomaly score
    headway_seconds = Column(Float, nullable=False)  # denormalized for fast reads

    reading = relationship("MetricReading", back_populates="anomalies")
