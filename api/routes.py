"""
REST + WebSocket routes.

REST is what the dashboard calls once on page load, to back-fill chart
history before the live feed takes over. WebSocket is what keeps it
live after that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from api.ws_broadcast import broadcaster
from ingest.fetch_feed import MONITORED_FEEDS
from storage.db import SessionLocal
from storage.models import Anomaly, MetricReading

router = APIRouter()


@router.get("/api/routes")
def list_routes():
    """Which routes this deployment is monitoring, for the frontend to
    build its route picker without hardcoding the list twice."""
    return {"routes": sorted(MONITORED_FEEDS.keys())}


@router.get("/api/readings")
def get_readings(
    route_id: str = Query(..., description="e.g. '1', 'L', 'G'"),
    minutes: int = Query(60, ge=1, le=24 * 60),
):
    """Recent headway readings for one route, oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    session = SessionLocal()
    try:
        rows = (
            session.query(MetricReading)
            .filter(MetricReading.route_id == route_id, MetricReading.observed_at >= cutoff)
            .order_by(MetricReading.observed_at.asc())
            .all()
        )
        return {
            "route_id": route_id,
            "readings": [
                {
                    "observed_at": r.observed_at.isoformat(),
                    "headway_seconds": r.headway_seconds,
                    "active_trains": r.active_trains,
                }
                for r in rows
            ],
        }
    finally:
        session.close()


@router.get("/api/anomalies")
def get_anomalies(
    route_id: str | None = Query(None),
    minutes: int = Query(24 * 60, ge=1, le=7 * 24 * 60),
    limit: int = Query(200, ge=1, le=1000),
):
    """Recent anomaly flags, newest first. Omit route_id for all routes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    session = SessionLocal()
    try:
        q = session.query(Anomaly).filter(Anomaly.detected_at >= cutoff)
        if route_id:
            q = q.filter(Anomaly.route_id == route_id)
        rows = q.order_by(Anomaly.detected_at.desc()).limit(limit).all()
        return {
            "anomalies": [
                {
                    "route_id": a.route_id,
                    "detected_at": a.detected_at.isoformat(),
                    "method": a.method,
                    "score": a.score,
                    "headway_seconds": a.headway_seconds,
                }
                for a in rows
            ]
        }
    finally:
        session.close()


@router.get("/api/health")
def health():
    return {"status": "ok"}


@router.websocket("/ws/live")
async def live_feed(websocket: WebSocket):
    await websocket.accept()
    await broadcaster.register(websocket)
    try:
        while True:
            # We don't expect messages from the client; this just keeps
            # the connection open and lets us detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unregister(websocket)
