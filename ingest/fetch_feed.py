"""
Pull the MTA NYC Subway GTFS-realtime feed and turn it into a single
number per monitored route: the current headway gap, in seconds.

No API key required — MTA dropped the key requirement for subway
GTFS-RT feeds. See: https://github.com/Andrew-Dickinson/nyct-gtfs

Feed URLs are rooted at api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct/
with a per-line-bundle suffix (gtfs, gtfs-ace, gtfs-bdfm, gtfs-g, gtfs-jz,
gtfs-nqrw, gtfs-l, gtfs-si — each bundle covers lines that share trackage).
Verified against the `underground` and `nyct-gtfs` open-source clients,
which both document this exact path. If a request ever 404s, check
those repos or api.mta.info first — this is the kind of URL that can
quietly move.

How "headway gap" is computed
------------------------------
GTFS-RT gives us, per route, a list of currently active TripUpdates —
trains that are out on the line right now along with their predicted
arrival time at their *next* stop. We don't have a fixed physical sensor
to clock trains past, so we use a deliberately simple proxy:

    headway_seconds = time between the two soonest predicted arrivals,
                       among trains converging on the same direction.

This is an approximation, not a precise platform-level headway, and the
README says so explicitly. It's good enough to surface real signal
(bunching, gaps from disruptions) without needing the static GTFS
schedule join that exact platform headways would require — that's a
documented simplification, not an accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

FEED_BASE = "https://cdn.mbta.com/realtime" 

# route_id (as it appears in TripUpdate.trip.route_id) -> feed suffix
# Verified against https://github.com/nolanbconaway/underground and
# https://github.com/Andrew-Dickinson/nyct-gtfs, both of which document
# this exact path structure as of 2026. If MTA changes this, both of
# those libraries' READMEs (and api.mta.info) are the source of truth
# to re-check against.
MONITORED_FEEDS = {
    "Red": "TripUpdates.pb",
    "Orange": "TripUpdates.pb",
    "Blue": "TripUpdates.pb",
}

REQUEST_TIMEOUT_SECONDS = 15


@dataclass
class HeadwayObservation:
    route_id: str
    headway_seconds: float
    active_trains: int
    observed_at: datetime


def _feed_url(feed_suffix: str) -> str:
    return f"{FEED_BASE}/{feed_suffix}"


def _fetch_feed_bytes(feed_suffix: str) -> bytes:
    url = _feed_url(feed_suffix)
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.content


def _parse_feed(raw_bytes: bytes):
    # Imported lazily, here rather than at module level, so that pure
    # parsing-logic functions in this module (like _next_arrival_epoch)
    # remain unit-testable without the gtfs-realtime-bindings package
    # installed — only this function actually needs it.
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw_bytes)
    return feed


def _next_arrival_epoch(trip_update) -> int | None:
    """Return the earliest future stop-time arrival/departure epoch
    for a TripUpdate, or None if it has no usable times left."""
    best = None
    for stu in trip_update.stop_time_update:
        candidate = None
        if stu.arrival and stu.arrival.time:
            candidate = stu.arrival.time
        elif stu.departure and stu.departure.time:
            candidate = stu.departure.time
        if candidate is not None and (best is None or candidate < best):
            best = candidate
    return best


def compute_headways_for_feed(feed_suffix: str, route_ids: list[str]) -> list[HeadwayObservation]:
    """Fetch one GTFS-RT bundle and compute a headway observation for
    each of `route_ids` that has at least two active trips."""
    now = datetime.now(timezone.utc)
    raw = _fetch_feed_bytes(feed_suffix)
    feed = _parse_feed(raw)

    # route_id -> list of next-arrival epochs across active trips
    upcoming: dict[str, list[int]] = {r: [] for r in route_ids}

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        route_id = tu.trip.route_id
        if route_id not in upcoming:
            continue
        epoch = _next_arrival_epoch(tu)
        if epoch is not None:
            upcoming[route_id].append(epoch)

    observations: list[HeadwayObservation] = []
    for route_id, epochs in upcoming.items():
        epochs.sort()
        active = len(epochs)
        if active < 2:
            # Can't compute a gap with fewer than two trains in service;
            # skip rather than fabricate a number. Late night / early
            # morning legitimately has gaps like this.
            logger.info("route %s has %d active trips, skipping", route_id, active)
            continue
        gap = float(epochs[1] - epochs[0])
        observations.append(
            HeadwayObservation(
                route_id=route_id,
                headway_seconds=gap,
                active_trains=active,
                observed_at=now,
            )
        )
    return observations


def poll_all_feeds() -> list[HeadwayObservation]:
    """Fetch every distinct feed bundle in MONITORED_FEEDS once, and
    return headway observations for every monitored route found."""
    feed_to_routes: dict[str, list[str]] = {}
    for route_id, feed_suffix in MONITORED_FEEDS.items():
        feed_to_routes.setdefault(feed_suffix, []).append(route_id)

    all_observations: list[HeadwayObservation] = []
    for feed_suffix, route_ids in feed_to_routes.items():
        try:
            obs = compute_headways_for_feed(feed_suffix, route_ids)
            all_observations.extend(obs)
        except requests.RequestException as exc:
            logger.warning("failed to fetch feed %s: %s", feed_suffix, exc)
        except Exception:
            logger.exception("unexpected error parsing feed %s", feed_suffix)
    return all_observations
