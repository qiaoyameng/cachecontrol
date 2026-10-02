# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
from email.utils import formatdate
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from requests import Request, Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3 import HTTPResponse

from cachecontrol.adapter import CacheControlAdapter
from cachecontrol.cache import DictCache
from cachecontrol.controller import CacheController
from tests.utils import NullSerializer

URL = "http://example.com/config"


class FakeClock:
    def __init__(self, current=0):
        self.current = current

    def time(self):
        return self.current


def http_date(when):
    return formatdate(when, usegmt=True)


def prepare_request(headers=None):
    return Session().prepare_request(Request("GET", URL, headers=headers))


def cached_response(body=b"stale body", **headers):
    cache_control = headers.pop("cache_control", None)
    response_headers = {
        "date": http_date(0),
        "cache-control": "max-age=10, stale-if-error=20",
        **headers,
    }
    if cache_control is not None:
        response_headers["cache-control"] = cache_control
    return HTTPResponse(
        body=BytesIO(body),
        headers=response_headers,
        status=200,
        reason="OK",
        preload_content=False,
    )


def seed_cache(adapter, request, response, body=b"stale body"):
    adapter.controller.cache_response(request, response, body)


def upstream_response(status_code):
    response = Response()
    response.status_code = status_code
    response.raw = Mock()
    return response


def make_adapter(cache=None, **kwargs):
    cache = cache or DictCache()
    kwargs.setdefault("stale_if_error", True)
    return (
        CacheControlAdapter(cache=cache, serializer=NullSerializer(), **kwargs),
        cache,
    )


def test_stale_if_error_sequence_with_fake_clock():
    clock = FakeClock()
    adapter, cache = make_adapter()
    request = prepare_request()
    original = cached_response()
    seed_cache(adapter, request, original)
    key = adapter.controller.cache_url(URL)

    cache.set = Mock(wraps=cache.set)
    cache.delete = Mock(wraps=cache.delete)
    upstream = Mock(
        side_effect=[
            upstream_response(503),
            upstream_response(503),
            upstream_response(401),
        ]
    )

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter, "send", upstream
    ):
        clock.current = 15
        stale_response = adapter.send(request)
        assert stale_response.status_code == 200
        assert stale_response.from_cache is True
        assert stale_response.from_cache_reason == "stale-if-error"
        assert stale_response.content == b"stale body"

        clock.current = 35
        late_failure = adapter.send(request)
        assert late_failure.status_code == 503
        assert getattr(late_failure, "from_cache", False) is False
        assert getattr(late_failure, "from_cache_reason", None) is None

        # An authentication failure must not be masked, even when it occurs
        # within the stale-if-error window.
        clock.current = 25
        auth_failure = adapter.send(request)
        assert auth_failure.status_code == 401
        assert getattr(auth_failure, "from_cache", False) is False
        assert getattr(auth_failure, "from_cache_reason", None) is None

    stored = cache.get(key)
    assert stored is original
    assert stored.headers["Date"] == http_date(0)
    cache.set.assert_not_called()
    cache.delete.assert_not_called()


def test_connection_error_uses_stale_response_within_window():
    clock = FakeClock(15)
    adapter, cache = make_adapter()
    request = prepare_request()
    original = cached_response()
    seed_cache(adapter, request, original)

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=RequestsConnectionError("connection refused"),
    ):
        response = adapter.send(request)

    assert response.status_code == 200
    assert response.from_cache is True
    assert response.from_cache_reason == "stale-if-error"
    assert cache.get(adapter.controller.cache_url(URL)) is original


def test_caller_cancellation_is_not_replaced_with_stale_response():
    clock = FakeClock(15)
    adapter, cache = make_adapter()
    request = prepare_request()
    original = cached_response()
    seed_cache(adapter, request, original)
    key = adapter.controller.cache_url(URL)

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter, "send", side_effect=asyncio.CancelledError()
    ):
        with pytest.raises(asyncio.CancelledError):
            adapter.send(request)

    assert cache.get(key) is original


def test_authentication_failure_never_uses_stale_response():
    clock = FakeClock(15)
    adapter, _ = make_adapter(stale_if_error_status_codes=[401, 503])
    request = prepare_request()
    seed_cache(adapter, request, cached_response())

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(401),
    ):
        response = adapter.send(request)

    assert response.status_code == 401
    assert getattr(response, "from_cache", False) is False


def test_client_stale_if_error_limit_is_stricter_than_response():
    clock = FakeClock()
    adapter, _ = make_adapter(stale_if_error=10)
    request = prepare_request()
    seed_cache(adapter, request, cached_response())

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        clock.current = 15
        response = adapter.send(request)
        assert response.status_code == 200
        assert response.from_cache_reason == "stale-if-error"

        clock.current = 35
        response = adapter.send(request)
        assert response.status_code == 503


def test_client_stale_if_error_limit_can_enable_response_window():
    clock = FakeClock(15)
    adapter, _ = make_adapter(stale_if_error=20)
    request = prepare_request()
    seed_cache(
        adapter,
        request,
        cached_response(cache_control="max-age=10"),
    )

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        response = adapter.send(request)

    assert response.status_code == 200
    assert response.from_cache_reason == "stale-if-error"


def test_boolean_stale_if_error_requires_response_window():
    clock = FakeClock(15)
    adapter, _ = make_adapter(stale_if_error=True)
    request = prepare_request()
    seed_cache(
        adapter,
        request,
        cached_response(cache_control="max-age=10"),
    )

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        response = adapter.send(request)

    assert response.status_code == 503
    assert getattr(response, "from_cache", False) is False


def test_stale_if_error_is_opt_in():
    clock = FakeClock(15)
    cache = DictCache()
    adapter = CacheControlAdapter(cache=cache, serializer=NullSerializer())
    request = prepare_request()
    response = cached_response()
    seed_cache(adapter, request, response)

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        result = adapter.send(request)

    assert result.status_code == 503
    assert cache.get(adapter.controller.cache_url(URL)) is None


@pytest.mark.parametrize(
    "request_headers",
    [
        {"cache-control": "no-store"},
        {"cache-control": "no-cache"},
        {"cache-control": "max-age=0"},
        {"cache-control": "min-fresh=60"},
        {"cache-control": "max-stale=0"},
    ],
)
def test_request_cache_control_blocks_stale_if_error(request_headers):
    clock = FakeClock(15)
    adapter, cache = make_adapter()
    request = prepare_request(request_headers)
    original = cached_response()
    seed_cache(adapter, request, original)
    key = adapter.controller.cache_url(URL)

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        response = adapter.send(request)

    assert response.status_code == 503
    directive = request_headers["cache-control"]
    if directive == "no-store":
        assert cache.get(key) is None
    else:
        assert cache.get(key) is original


@pytest.mark.parametrize(
    "response_headers",
    [
        {"cache-control": "max-age=10, stale-if-error=20, no-store"},
        {"cache-control": "max-age=10, stale-if-error=20, no-cache"},
        {"cache-control": "max-age=10, stale-if-error=20, must-revalidate"},
        {"cache-control": "max-age=10, stale-if-error=20, proxy-revalidate"},
    ],
)
def test_response_revalidation_directives_block_stale_if_error(response_headers):
    clock = FakeClock(15)
    adapter, cache = make_adapter()
    request = prepare_request()
    seed_cache(adapter, request, cached_response(**response_headers))

    with patch("cachecontrol.controller.time.time", clock.time), patch.object(
        HTTPAdapter,
        "send",
        side_effect=lambda *args, **kwargs: upstream_response(503),
    ):
        response = adapter.send(request)

    assert response.status_code == 503


def test_deserialization_error_is_not_masked_by_stale_response():
    clock = FakeClock(15)
    serializer = Mock()
    serializer.loads.side_effect = ValueError("bad cache entry")
    controller = CacheController(
        DictCache({URL: b"cached"}),
        serializer=serializer,
        stale_if_error=True,
    )
    request = prepare_request()

    with patch("cachecontrol.controller.time.time", clock.time):
        with pytest.raises(ValueError, match="bad cache entry"):
            controller.stale_if_error_response(request)
