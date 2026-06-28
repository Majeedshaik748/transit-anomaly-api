"""
Bridges the scheduler's background thread (where new readings are
produced) to the asyncio event loop (where WebSocket clients live).

APScheduler's BackgroundScheduler runs jobs in plain OS threads, not
coroutines, so the scheduler thread cannot safely call into asyncio
WebSocket sends directly. The standard pattern: the producer thread
pushes onto a plain thread-safe queue.Queue; a dedicated asyncio task
drains that queue and fans out to all connected WebSocket clients.
"""

from __future__ import annotations

import asyncio
import json
import queue
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import WebSocket


@dataclass
class BroadcastEvent:
    type: str  # "reading" or "anomaly"
    payload: dict[str, Any]


class Broadcaster:
    def __init__(self):
        self._queue: queue.Queue[BroadcastEvent] = queue.Queue()
        self._clients: set[WebSocket] = set()

    def publish(self, event: BroadcastEvent) -> None:
        """Call this from ANY thread, including the scheduler thread."""
        self._queue.put(event)

    async def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def run_forever(self) -> None:
        """Drain the thread-safe queue and fan out to websocket clients.
        Runs as a long-lived asyncio task started in the FastAPI lifespan.

        Uses a short sleep + non-blocking get rather than a blocking get
        in an executor: this is a low-frequency queue (one poll cycle's
        worth of events every ~30s), so a 200ms tick is plenty responsive
        without parking a threadpool worker on every iteration.
        """
        while True:
            drained_any = False
            while True:
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    break
                drained_any = True
                if self._clients:
                    message = json.dumps({"type": event.type, "payload": event.payload})
                    dead = []
                    for client in list(self._clients):
                        try:
                            await client.send_text(message)
                        except Exception:
                            dead.append(client)
                    for d in dead:
                        self.unregister(d)
            await asyncio.sleep(0.05 if drained_any else 0.2)


broadcaster = Broadcaster()
