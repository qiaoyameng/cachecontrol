# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import time
from unittest.mock import Mock

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

from cachecontrol import (
    CacheControlAdapter,
    CachedResponseUnavailableError,
    NotInCacheError,
)
from cachecontrol.cache import DictCache
from cachecontrol.serialize import Serializer

from .utils import NullSerializer


TIME_FMT = "%a, %d %b %Y %H:%M:%S GMT"


def http_date(seconds_from_now: int) -> str:
    return time.strftime(TIME_FMT, time.gmtime(time.time() + seconds_from_now))


def cached_response(
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = b"cached body",
) -> HTTPResponse:
    return HTTPResponse(
        body=io.BytesIO(body),
        headers=headers or {},
        status=status,
        version=0,
        reason="OK",
        preload_content=False,
    )


def fail_network_send(*args, **kwargs):
    raise AssertionError("only-if-cached must not attempt a network request")


@pytest.fixture(params=[Serializer, NullSerializer])
def session(monkeypatch, request):
    monkeypatch.setattr(HTTPAdapter, "send", fail_network_send)
    cache = DictCache()
    adapter = CacheControlAdapter(cache, serializer=request.param())
    session = requests.Session()
    session.mount("http://", adapter)
    return session, adapter


def store(
    adapter: CacheControlAdapter,
    url: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = b"cached body",
) -> HTTPResponse:
    response = cached_response(status, headers, body)
    request = requests.Request("GET", url).prepare()
    adapter.controller._cache_set(
        adapter.controller.cache_url(url), request, response, body
    )
    return response


@pytest.mark.parametrize(
    ("response_headers", "request_headers", "expected_status"),
    [
        (
            {"Date": http_date(0), "Cache-Control": "max-age=60"},
            {"Cache-Control": "only-if-cached"},
            200,
        ),
        (
            {"Date": http_date(-30), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached, max-stale=60"},
            200,
        ),
        (
            {"Date": http_date(-30), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached"},
            504,
        ),
        (
            {"Date": http_date(-30), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached, max-stale=10"},
            504,
        ),
        (
            {"Date": http_date(0), "Cache-Control": "max-age=60"},
            {"Cache-Control": "only-if-cached, min-fresh=120"},
            504,
        ),
        (
            {"Date": http_date(-20), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached, max-age=0, max-stale=0"},
            504,
        ),
        (
            {"Date": http_date(-20), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached, max-age=0, max-stale=60"},
            200,
        ),
        (
            {"Date": http_date(-30), "Cache-Control": "max-age=10"},
            {"Cache-Control": "only-if-cached, max-age=0"},
            504,
        ),
        (
            {"Date": http_date(0), "Cache-Control": "max-age=60, no-cache"},
            {"Cache-Control": "only-if-cached"},
            504,
        ),
        (
            {"Date": http_date(0), "Cache-Control": "max-age=60"},
            {"Cache-Control": "only-if-cached, no-cache"},
            504,
        ),
        (
            {
                "Date": http_date(-30),
                "Cache-Control": "max-age=10, must-revalidate",
            },
            {"Cache-Control": "only-if-cached, max-stale=60"},
            504,
        ),
    ],
)
def test_only_if_cached_decision_matrix(
    session, response_headers, request_headers, expected_status
):
    sess, adapter = session
    url = "http://example.test/resource"
    store(adapter, url, headers=response_headers)

    response = sess.get(url, headers=request_headers)

    assert response.status_code == expected_status
    assert response.from_cache is (expected_status == 200)


def test_missing_only_if_cached_response_is_504(session):
    sess, adapter = session
    url = "http://example.test/missing"

    response = sess.get(url, headers={"Cache-Control": "only-if-cached"})

    assert response.status_code == 504
    assert not response.from_cache
    assert adapter.cache.get(adapter.controller.cache_url(url)) is None


def test_controller_distinguishes_missing_and_unqualified(session, request):
    _, adapter = session
    serializer_class = request.node.callspec.params["session"]
    url = "http://example.test/resource"
    request = requests.Request("GET", url).prepare()
    request.headers["Cache-Control"] = "only-if-cached"

    with pytest.raises(NotInCacheError):
        adapter.controller.cached_request(request)

    cached = store(
        adapter,
        url,
        headers={"Date": http_date(-60), "Cache-Control": "max-age=10"},
    )

    with pytest.raises(CachedResponseUnavailableError) as exc_info:
        adapter.controller.cached_request(request)

    if serializer_class is NullSerializer:
        assert exc_info.value.response is cached
        assert adapter.cache.get(adapter.controller.cache_url(url)) is cached
    else:
        assert exc_info.value.response is not None
        assert adapter.cache.get(adapter.controller.cache_url(url)) is not None


def test_unloadable_entry_is_unqualified_not_missing():
    adapter = CacheControlAdapter(DictCache(), serializer=Serializer())
    url = "http://example.test/corrupt"
    adapter.cache.set(adapter.controller.cache_url(url), b"not-a-cache-entry")
    prepared = requests.Request("GET", url).prepare()
    prepared.headers["Cache-Control"] = "only-if-cached"

    with pytest.raises(CachedResponseUnavailableError):
        adapter.controller.cached_request(prepared)


def test_unavailable_diagnostic_does_not_read_stream():
    cache = DictCache()
    adapter = CacheControlAdapter(cache, serializer=NullSerializer())
    url = "http://example.test/resource"
    cached = Mock(
        headers={"Date": http_date(-60), "Cache-Control": "max-age=10"},
        status=200,
    )
    adapter.cache.set(adapter.controller.cache_url(url), cached)
    request = requests.Request("GET", url).prepare()
    request.headers["Cache-Control"] = "only-if-cached"

    with pytest.raises(CachedResponseUnavailableError) as exc_info:
        adapter.controller.cached_request(request)

    assert exc_info.value.response is cached
    cached.read.assert_not_called()


def test_only_if_cached_redirect_chain_uses_cache_for_every_hop(session):
    sess, adapter = session
    start = "http://example.test/start"
    second = "http://example.test/second"
    final = "http://example.test/final"
    now = {"Date": http_date(0), "Cache-Control": "max-age=60"}
    store(
        adapter,
        start,
        status=301,
        headers={**now, "Location": "/second"},
        body=b"second",
    )
    store(
        adapter,
        second,
        status=301,
        headers={**now, "Location": "/final"},
        body=b"final",
    )
    store(adapter, final, headers=now, body=b"done")

    response = sess.get(start, headers={"Cache-Control": "only-if-cached"})

    assert response.status_code == 200
    assert response.from_cache
    assert [r.status_code for r in response.history] == [301, 301]
    assert all(r.from_cache for r in response.history)


def test_only_if_cached_redirect_chain_fails_closed_for_every_hop(session):
    sess, adapter = session
    start = "http://example.test/start"
    second = "http://example.test/second"
    store(
        adapter,
        start,
        status=301,
        headers={
            "Date": http_date(0),
            "Cache-Control": "max-age=60",
            "Location": "/second",
        },
        body=b"second",
    )
    store(
        adapter,
        second,
        status=200,
        headers={
            "Date": http_date(-60),
            "Cache-Control": "max-age=10, must-revalidate",
        },
    )

    response = sess.get(start, headers={"Cache-Control": "only-if-cached"})

    assert response.status_code == 504
    assert [r.status_code for r in response.history] == [301]
    assert response.history[0].from_cache
