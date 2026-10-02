# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the ``only-if-cached`` request directive.

The underlying network send is replaced with a stub that fails the
moment it is called, so every result in this module is decided by
cache policy alone.
"""

import io
import logging
import time
from email.utils import formatdate

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

from cachecontrol import CacheControlAdapter
from cachecontrol.cache import DictCache
from cachecontrol.serialize import Serializer

ONLY_IF_CACHED = {"Cache-Control": "only-if-cached"}


@pytest.fixture()
def no_network(monkeypatch):
    """Turn the transport's network send into a fail-on-call stub."""

    def send(self, request, **kwargs):
        raise AssertionError(f"network access attempted for {request.url}")

    monkeypatch.setattr(HTTPAdapter, "send", send)


@pytest.fixture()
def sess(no_network):
    session = requests.Session()
    session.mount("http://", CacheControlAdapter(cache=DictCache()))
    return session


def http_date(age=0):
    return formatdate(time.time() - age, usegmt=True)


def store(sess, url, status=200, headers=None, body=b"cached"):
    """Place a response in the cache as if it had come from the network."""
    request = requests.Request("GET", url).prepare()
    response = HTTPResponse(
        body=io.BytesIO(body),
        status=status,
        headers=headers or {},
        preload_content=False,
    )
    controller = sess.get_adapter(url).controller
    controller.cache_response(request, response, body)


class TestOnlyIfCached:
    def test_fresh_entry_is_served_from_cache(self, sess):
        url = "http://example.com/fresh"
        store(
            sess,
            url,
            headers={"date": http_date(), "cache-control": "max-age=3600"},
        )

        resp = sess.get(url, headers=ONLY_IF_CACHED)

        assert resp.status_code == 200
        assert resp.from_cache
        assert resp.content == b"cached"

    def test_missing_entry_returns_504(self, sess, caplog):
        with caplog.at_level(logging.DEBUG, logger="cachecontrol.controller"):
            resp = sess.get("http://example.com/absent", headers=ONLY_IF_CACHED)

        assert resp.status_code == 504
        assert resp.from_cache
        assert "cannot be satisfied: no cache entry" in caplog.text

    def test_stale_entry_returns_504(self, sess, caplog):
        url = "http://example.com/stale"
        store(
            sess,
            url,
            headers={"date": http_date(120), "cache-control": "max-age=60"},
        )

        with caplog.at_level(logging.DEBUG, logger="cachecontrol.controller"):
            resp = sess.get(url, headers=ONLY_IF_CACHED)

        assert resp.status_code == 504
        # The diagnostics distinguish an unusable entry from a missing one.
        assert "cannot be satisfied: cache entry is stale" in caplog.text

    def test_stale_entry_allowed_by_max_stale(self, sess):
        url = "http://example.com/stale-allowed"
        store(
            sess,
            url,
            headers={"date": http_date(120), "cache-control": "max-age=60"},
        )

        resp = sess.get(
            url, headers={"Cache-Control": "only-if-cached, max-stale=180"}
        )

        assert resp.status_code == 200
        assert resp.from_cache
        assert resp.content == b"cached"

    def test_stale_entry_allowed_by_valueless_max_stale(self, sess):
        url = "http://example.com/stale-allowed-any"
        store(
            sess,
            url,
            headers={"date": http_date(120), "cache-control": "max-age=60"},
        )

        resp = sess.get(url, headers={"Cache-Control": "only-if-cached, max-stale"})

        assert resp.status_code == 200
        assert resp.from_cache

    def test_staleness_beyond_max_stale_returns_504(self, sess):
        url = "http://example.com/too-stale"
        store(
            sess,
            url,
            headers={"date": http_date(120), "cache-control": "max-age=60"},
        )

        resp = sess.get(
            url, headers={"Cache-Control": "only-if-cached, max-stale=30"}
        )

        assert resp.status_code == 504

    def test_must_revalidate_forbids_serving_stale(self, sess, caplog):
        url = "http://example.com/must-revalidate"
        store(
            sess,
            url,
            headers={
                "date": http_date(120),
                "cache-control": "max-age=60, must-revalidate",
            },
        )

        with caplog.at_level(logging.DEBUG, logger="cachecontrol.controller"):
            resp = sess.get(
                url, headers={"Cache-Control": "only-if-cached, max-stale"}
            )

        assert resp.status_code == 504
        assert "must-revalidate" in caplog.text

    def test_no_cache_request_directive_returns_504(self, sess):
        url = "http://example.com/no-cache"
        store(
            sess,
            url,
            headers={"date": http_date(), "cache-control": "max-age=3600"},
        )

        resp = sess.get(
            url, headers={"Cache-Control": "only-if-cached, no-cache"}
        )

        assert resp.status_code == 504

    def test_min_fresh_makes_entry_unusable(self, sess):
        url = "http://example.com/min-fresh"
        store(
            sess,
            url,
            headers={"date": http_date(), "cache-control": "max-age=60"},
        )

        resp = sess.get(
            url, headers={"Cache-Control": "only-if-cached, min-fresh=120"}
        )

        assert resp.status_code == 504

    def test_min_fresh_staleness_allowed_by_max_stale(self, sess):
        url = "http://example.com/min-fresh-max-stale"
        store(
            sess,
            url,
            headers={"date": http_date(), "cache-control": "max-age=60"},
        )

        resp = sess.get(
            url,
            headers={"Cache-Control": "only-if-cached, min-fresh=120, max-stale"},
        )

        assert resp.status_code == 200
        assert resp.from_cache

    def test_diagnostics_do_not_reread_the_cached_stream(self, no_network, caplog):
        """Diagnosing an unusable entry must not re-read its stream."""
        loads_calls = 0

        class CountingSerializer(Serializer):
            def loads(self, request, data, body_file=None):
                nonlocal loads_calls
                loads_calls += 1
                return super().loads(request, data, body_file)

        session = requests.Session()
        session.mount(
            "http://",
            CacheControlAdapter(
                cache=DictCache(), serializer=CountingSerializer()
            ),
        )
        url = "http://example.com/count-reads"
        store(
            session,
            url,
            headers={"date": http_date(120), "cache-control": "max-age=60"},
        )

        with caplog.at_level(logging.DEBUG, logger="cachecontrol.controller"):
            resp = session.get(url, headers=ONLY_IF_CACHED)

        assert resp.status_code == 504
        assert "cannot be satisfied: cache entry is stale" in caplog.text
        assert loads_calls == 1


class TestOnlyIfCachedRedirects:
    def test_redirect_chain_served_from_cache(self, sess):
        start = "http://example.com/a"
        target = "http://example.com/b"
        store(
            sess,
            start,
            status=301,
            headers={"date": http_date(), "location": target},
        )
        store(
            sess,
            target,
            headers={"date": http_date(), "cache-control": "max-age=3600"},
        )

        resp = sess.get(start, headers=ONLY_IF_CACHED)

        assert resp.status_code == 200
        assert resp.url == target
        assert resp.from_cache
        assert [r.status_code for r in resp.history] == [301]
        assert resp.history[0].from_cache

    def test_redirect_chain_with_missing_hop_returns_504(self, sess):
        start = "http://example.com/c"
        store(
            sess,
            start,
            status=301,
            headers={"date": http_date(), "location": "http://example.com/d"},
        )

        resp = sess.get(start, headers=ONLY_IF_CACHED)

        assert resp.status_code == 504
        assert [r.status_code for r in resp.history] == [301]
