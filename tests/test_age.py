# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for response age accounting (RFC 9111 section 4.2.3).

These tests use fixed request/response timestamps and a fake clock so
every age and remaining freshness lifetime can be checked against an
exact number.
"""

import io
from email.utils import formatdate

import msgpack
import pytest
import requests
from urllib3 import HTTPResponse

from cachecontrol.age import AgeInfo
from cachecontrol.cache import DictCache
from cachecontrol.controller import CacheController
from cachecontrol.serialize import Serializer

URL = "http://example.com/"
MAX_AGE = 3600


class FakeClock:
    """Deterministic stand-in for the ``time`` module."""

    def __init__(self, wall, mono):
        self.wall = float(wall)
        self.mono = float(mono)

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def advance_wall(self, seconds):
        self.wall += seconds

    def advance_monotonic(self, seconds):
        self.mono += seconds


@pytest.fixture()
def clock(monkeypatch):
    clock = FakeClock(wall=1_000_000, mono=50_000)
    monkeypatch.setattr("cachecontrol.controller.time", clock)
    monkeypatch.setattr("cachecontrol.age.time", clock)
    return clock


@pytest.fixture()
def cc():
    return CacheController(DictCache())


def make_request():
    return requests.Request("GET", URL).prepare()


def make_response(status=200, headers=None):
    return HTTPResponse(
        body=io.BytesIO(b"body"),
        status=status,
        headers=headers or {},
        preload_content=False,
    )


def cache_response(cc, request, headers, clock, request_sent=None, delay=0):
    """Cache a response with fixed send/receive times."""
    if request_sent is not None:
        request.cachecontrol_request_time = request_sent
    if delay:
        clock.advance(delay)
    cc.cache_response(request, make_response(headers=headers), b"body")


class TestInitialAge:
    def test_counts_upstream_age_delay_and_date(self, cc, clock):
        req = make_request()
        # Request sent at t=1_000_000, response received 2s later. The
        # Date header is 100s old and the response sat 30s in an
        # upstream cache.
        cache_response(
            cc,
            req,
            {
                "Date": formatdate(1_000_002 - 100, usegmt=True),
                "Age": "30",
                "Cache-Control": f"max-age={MAX_AGE}",
            },
            clock,
            request_sent=1_000_000,
            delay=2,
        )

        # apparent_age = 100, response_delay = 2,
        # corrected_age_value = 30 + 2 = 32,
        # corrected_initial_age = max(100, 32) = 100
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "100"
        # remaining freshness: 3600 - 100 = 3500 seconds

        clock.advance(3499)
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "3599"

        # The full freshness lifetime is now used up.
        clock.advance(1)
        assert cc.cached_request(req) is False

    def test_upstream_age_wins_when_date_is_optimistic(self, cc, clock):
        req = make_request()
        # Date says the response is 5s old, but the upstream Age plus
        # the 4s request/response delay is larger.
        cache_response(
            cc,
            req,
            {
                "Date": formatdate(1_000_004 - 5, usegmt=True),
                "Age": "40",
                "Cache-Control": f"max-age={MAX_AGE}",
            },
            clock,
            request_sent=1_000_000,
            delay=4,
        )

        # corrected_initial_age = max(5, 40 + 4) = 44
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "44"


class TestResidentTime:
    def test_monotonic_clock_used_in_process(self, cc, clock):
        req = make_request()
        cache_response(
            cc,
            req,
            {"Date": formatdate(1_000_000, usegmt=True),
             "Cache-Control": f"max-age={MAX_AGE}"},
            clock,
        )

        # The wall clock jumps backwards; the monotonic clock keeps
        # moving. The response must keep aging on the monotonic clock
        # instead of becoming younger.
        clock.advance_wall(-3600)
        clock.advance_monotonic(10)

        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "10"

    def test_restore_rebases_resident_time_on_wall_clock(
        self, cc, clock, monkeypatch
    ):
        req = make_request()
        cache_response(
            cc,
            req,
            {
                "Date": formatdate(1_000_000 - 10, usegmt=True),
                "Age": "5",
                "Cache-Control": f"max-age={MAX_AGE}",
            },
            clock,
        )
        # corrected_initial_age = max(10, 5) = 10

        # A restore in the same process keeps the monotonic time basis.
        data = cc.cache.get(URL)
        restored = Serializer().loads(make_request(), data)
        info = restored.cachecontrol_age_info
        assert info.monotonic_response_time == 50_000

        # Simulate a process restart: only the serialized form survives,
        # and the monotonic timestamp is meaningless across processes.
        monkeypatch.setattr("cachecontrol.age._PROCESS_TOKEN", "other-process")
        restored = Serializer().loads(make_request(), data)
        info = restored.cachecontrol_age_info
        assert info.monotonic_response_time is None
        # The time basis for restored entries is the wall clock.
        assert info.response_time == 1_000_000

        # Move only the wall clock: the restored entry still ages.
        clock.advance_wall(50)
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "60"
        # remaining freshness: 3600 - 60 = 3540 seconds

        # A wall-clock rollback clamps the resident time at zero; the
        # entry keeps its initial age instead of becoming ageless.
        clock.advance_wall(-500)
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "10"

    def test_corrupt_serialized_age_metadata_is_stale_not_fresh(self, cc, clock):
        req = make_request()
        cache_response(
            cc,
            req,
            {"Date": formatdate(1_000_000, usegmt=True),
             "Cache-Control": f"max-age={MAX_AGE}"},
            clock,
        )

        serializer = Serializer()
        prefix, packed = cc.cache.get(URL).split(b",", 1)
        data = msgpack.loads(packed, raw=False)
        data["age"] = {
            "date_value": "junk",
            "age_value": "lots",
            "request_time": "soon",
            "response_time": "whenever",
        }
        cc.cache.set(
            URL, prefix + b"," + msgpack.dumps(data, use_bin_type=True)
        )

        # Corrupt metadata restores as "as old as possible" rather than
        # making the entry infinitely fresh.
        assert cc.cached_request(req) is False


class TestRevalidation:
    def test_304_recomputes_age_metadata(self, cc, clock):
        req = make_request()
        cache_response(
            cc,
            req,
            {
                "Date": formatdate(1_000_002 - 100, usegmt=True),
                "Age": "30",
                "Cache-Control": f"max-age={MAX_AGE}",
                "ETag": '"xyz"',
            },
            clock,
            request_sent=1_000_000,
            delay=2,
        )
        # corrected_initial_age = max(100, 30 + 2) = 100

        clock.advance(40)
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "140"
        # remaining freshness: 3600 - 140 = 3460 seconds

        # The conditional request is sent at t=1_000_042 and the 304
        # arrives 3s later with a 1s old Date and an upstream Age of 5.
        req.cachecontrol_request_time = clock.time()
        clock.advance(3)
        not_modified = make_response(
            status=304,
            headers={
                "Date": formatdate(1_000_045 - 1, usegmt=True),
                "Age": "5",
                "Cache-Control": f"max-age={MAX_AGE}",
                "ETag": '"xyz"',
            },
        )
        updated = cc.update_cached_response(req, not_modified)

        # The validated metadata resets the age basis:
        # apparent_age = 1, corrected_age_value = 5 + 3 = 8,
        # corrected_initial_age = max(1, 8) = 8
        assert updated.status == 200
        assert updated.headers["Age"] == "8"

        clock.advance(12)
        resp = cc.cached_request(req)
        assert resp is not False
        # 8 + 12 = 20: neither the Age emitted on the earlier hit (140)
        # nor the original upstream Age (30) is accumulated again.
        assert resp.headers["Age"] == "20"
        # remaining freshness: 3600 - 20 = 3580 seconds

        # Repeated hits do not accumulate the emitted Age either.
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "20"

        clock.advance(3579)
        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "3599"

        clock.advance(1)
        assert cc.cached_request(req) is False


class TestInvalidValues:
    @pytest.mark.parametrize("age_header", ["garbage", "-50"])
    def test_invalid_age_header_treated_as_zero(self, cc, clock, age_header):
        req = make_request()
        cache_response(
            cc,
            req,
            {
                "Date": formatdate(1_000_000, usegmt=True),
                "Age": age_header,
                "Cache-Control": "max-age=100",
            },
            clock,
        )

        resp = cc.cached_request(req)
        assert resp is not False
        assert resp.headers["Age"] == "0"

    def test_invalid_date_in_cached_entry_not_served(self, cc, clock):
        req = make_request()
        cache_response(
            cc,
            req,
            {"Date": formatdate(1_000_000, usegmt=True),
             "Cache-Control": f"max-age={MAX_AGE}"},
            clock,
        )

        # Corrupt the Date header of the stored entry.
        prefix, packed = cc.cache.get(URL).split(b",", 1)
        data = msgpack.loads(packed, raw=False)
        data["response"]["headers"]["Date"] = "garbage"
        cc.cache.set(
            URL, prefix + b"," + msgpack.dumps(data, use_bin_type=True)
        )

        # The invalid Date must not make the entry infinitely fresh;
        # it cannot be served at all.
        assert cc.cached_request(req) is False

    def test_age_info_from_dict_with_corrupt_values(self):
        info = AgeInfo.from_dict(
            {
                "date_value": "junk",
                "age_value": "lots",
                "request_time": "soon",
                "response_time": "whenever",
            }
        )
        # A zero response time makes the entry as old as possible.
        assert info.response_time == 0.0
        assert info.current_age(now=1_000_000) == 1_000_000
