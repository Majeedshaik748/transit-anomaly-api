"""
Tests for the arrival-time-selection logic in ingest/fetch_feed.py.

We use small fake classes instead of real `google.transit` protobuf
objects so these tests run without the gtfs-realtime-bindings package
installed — they exercise the exact same attribute-access pattern
(`.arrival.time`, `.departure.time`, `.stop_time_update`) that the real
protobuf-generated classes expose, so the logic under test is identical;
only the object construction differs.
"""

from ingest.fetch_feed import _next_arrival_epoch


class _FakeTime:
    def __init__(self, time):
        self.time = time

    def __bool__(self):
        # protobuf message fields are "truthy" based on presence; a
        # FakeTime(0) should behave like an unset field would when we
        # check `if stu.arrival and stu.arrival.time`. We model that by
        # making the wrapper falsy when zero, matching protobuf's
        # `HasField`-adjacent truthiness for our purposes here.
        return self.time != 0


class _FakeStopTimeUpdate:
    def __init__(self, arrival_time=0, departure_time=0):
        self.arrival = _FakeTime(arrival_time)
        self.departure = _FakeTime(departure_time)


class _FakeTripUpdate:
    def __init__(self, stop_time_updates):
        self.stop_time_update = stop_time_updates


def test_picks_earliest_arrival():
    tu = _FakeTripUpdate(
        [
            _FakeStopTimeUpdate(arrival_time=1000200),
            _FakeStopTimeUpdate(arrival_time=1000100),
            _FakeStopTimeUpdate(arrival_time=1000300),
        ]
    )
    assert _next_arrival_epoch(tu) == 1000100


def test_falls_back_to_departure_when_no_arrival():
    tu = _FakeTripUpdate([_FakeStopTimeUpdate(arrival_time=0, departure_time=1000500)])
    assert _next_arrival_epoch(tu) == 1000500


def test_returns_none_for_empty_trip():
    tu = _FakeTripUpdate([])
    assert _next_arrival_epoch(tu) is None


def test_returns_none_when_all_times_unset():
    tu = _FakeTripUpdate([_FakeStopTimeUpdate(), _FakeStopTimeUpdate()])
    assert _next_arrival_epoch(tu) is None
