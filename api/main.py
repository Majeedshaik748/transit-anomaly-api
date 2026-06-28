"""
FastAPI app. Three jobs:
  1. On startup, init the DB and start the background polling scheduler.
  2. Serve REST endpoints for historical reads (what the dashboard loads
     on first paint).
  3. Serve a WebSocket that pushes new readings/anomalies the moment the
     scheduler produces them — this is what makes the dashboard feel
     "live" instead of "polling the page every few seconds and pretending."
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from api.ws_broadcast import broadcaster
from ingest.scheduler import start_scheduler
from storage.db import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    global _scheduler
    _scheduler = start_scheduler()

    # Bridge: the scheduler runs in a plain background thread (APScheduler),
    # but broadcasting to WebSocket clients needs the asyncio event loop.
    # We poll a thread-safe queue from a small asyncio task instead of
    # trying to call asyncio code directly from the scheduler thread.
    broadcast_task = asyncio.create_task(broadcaster.run_forever())

    yield

    broadcast_task.cancel()
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Transit Anomaly Detector", lifespan=lifespan)

# Wide-open CORS: this is a public read-only dashboard over public transit
# data, not an authenticated app, so there's no per-user state to protect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
