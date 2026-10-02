from __future__ import annotations

import asyncio
import io
from email.utils import formatdate

import pytest
import requests
from requests import PreparedRequest
from requests.adapters import HTTPAdapter as BaseHTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3 import HTTPResponse

from cachecontrol.adapter import CacheControlAdapter
from cachecontrol.cache import DictCache
from cachecontrol.controller import (
    CACHE_HIT_REASON_FRESH,
    CACHE_HIT_REASON_STALE_IF_ERROR,
)

from .utils import NullSerializer


URL = "http://example.com/test"


class FakeClock:
    def __init__(self, current: float = 0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current


def date_at(seconds: int) -> str:
    return formatdate(seconds, usegmt=True)


def make_raw_response(
    status: int,
    body: bytes,
    headers: dict[str, str] | None = None,
) -> HTTPResponse:
    return HTTPResponse(
        body=io.BytesIO(body),
        headers=CaseInsensitiveDict(headers or {}),
        status=status,
        reason=b"OK",
        preload_content=False,
        decode_content=False,
    )


def seed_cached_response(
    adapter: CacheControlAdapter,
    request: PreparedRequest,
    cache_control: str = "max-age=60, stale-if-error=100",
) -> HTTPResponse:
    cached = make_raw_response(
        200,
        b"cached body",
        {
            "Date": date_at(0),
            "Cache-Control": cache_control,
            "Content-Type": "text/plain",
        },
    )
    adapter.controller._cache_set(URL, request, cached, b"cached body")
    return cached


@pytest.fixture()
def prepared_request() -> PreparedRequest:
    return requests.Request("GET", URL).prepare()


@pytest.fixture()
def adapter(monkeypatch: pytest.MonkeyPatch) -> CacheControlAdapter:
    clock = FakeClock()
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)

    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=True,
    )
    adapter.clock = clock  # type: ignore[attr-defined]
    return adapter


def install_upstream(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[int | type[BaseException]],
) -> None:
    outcomes_iter = iter(outcomes)

    def bound_send(
        self: BaseHTTPAdapter,
        prepared_request: PreparedRequest,
        stream: bool = False,
        timeout: None | float | tuple[float, float] = None,
        verify: bool | str = True,
        cert: None | bytes | str | tuple[bytes | str, bytes | str] = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        outcome = next(outcomes_iter)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome()

        raw = make_raw_response(
            outcome,
            f"upstream-{outcome}".encode(),
            {"Content-Type": "text/plain"},
        )
        return self.build_response(prepared_request, raw)

    monkeypatch.setattr(BaseHTTPAdapter, "send", bound_send)


def test_stale_if_error_window_status_and_storage_time(
    adapter: CacheControlAdapter,
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [503, 503, 401])
    clock = adapter.clock  # type: ignore[attr-defined]

    clock.current = 120
    within_window = adapter.send(prepared_request)
    assert within_window.status_code == 200
    assert within_window.raw is cached
    assert within_window.from_cache is True
    assert within_window.cache_hit_reason == CACHE_HIT_REASON_STALE_IF_ERROR
    assert adapter.cache.get(URL) is cached
    assert adapter.cache.get(URL).headers["Date"] == date_at(0)

    clock.current = 200
    outside_window = adapter.send(prepared_request)
    assert outside_window.status_code == 503
    assert outside_window.raw is not cached
    assert outside_window.from_cache is False
    assert outside_window.cache_hit_reason is None
    assert adapter.cache.get(URL) is cached
    assert adapter.cache.get(URL).headers["Date"] == date_at(0)

    clock.current = 200
    unauthorized = adapter.send(prepared_request)
    assert unauthorized.status_code == 401
    assert unauthorized.raw is not cached
    assert unauthorized.from_cache is False
    assert unauthorized.cache_hit_reason is None
    assert adapter.cache.get(URL) is cached
    assert adapter.cache.get(URL).headers["Date"] == date_at(0)


def test_unauthorized_within_window_is_not_masked(
    adapter: CacheControlAdapter,
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [401])
    adapter.clock.current = 120  # type: ignore[attr-defined]

    response = adapter.send(prepared_request)

    assert response.status_code == 401
    assert response.raw is not cached
    assert response.from_cache is False
    assert adapter.cache.get(URL) is cached


def test_connection_error_uses_stale_copy(
    adapter: CacheControlAdapter,
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [requests.exceptions.ConnectionError])
    adapter.clock.current = 120  # type: ignore[attr-defined]

    response = adapter.send(prepared_request)

    assert response.status_code == 200
    assert response.raw is cached
    assert response.from_cache_reason == CACHE_HIT_REASON_STALE_IF_ERROR


def test_caller_cancellation_is_not_masked(
    adapter: CacheControlAdapter,
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [asyncio.CancelledError])
    adapter.clock.current = 120  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        adapter.send(prepared_request)

    assert adapter.cache.get(URL) is cached


def test_unparseable_cache_entry_is_not_used_after_503(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache({URL: b"not-a-cache-entry"})
    adapter = CacheControlAdapter(cache, stale_if_error=True)
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared_request)

    assert response.status_code == 503
    assert response.from_cache is False


def test_cache_parse_exception_returns_current_failure(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSerializer(NullSerializer):
        def loads(self, request, data, body_file=None):
            raise TypeError("broken cached response")

    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache({URL: b"broken-cache-entry"})
    adapter = CacheControlAdapter(
        cache,
        serializer=FailingSerializer(),
        stale_if_error=True,
    )
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared_request)

    assert response.status_code == 503
    assert response.from_cache is False


@pytest.mark.parametrize(
    ("response_cache_control", "request_headers"),
    [
        ("max-age=60, stale-if-error=100, must-revalidate", {}),
        ("max-age=60, stale-if-error=100, no-store", {}),
        ("max-age=60, stale-if-error=100", {"Cache-Control": "no-store"}),
        ("max-age=60, stale-if-error=100", {"Cache-Control": "no-cache"}),
    ],
)
def test_cache_control_constraints_prevent_stale_fallback(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
    response_cache_control: str,
    request_headers: dict[str, str],
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=True,
    )
    prepared = requests.Request("GET", URL, headers=request_headers).prepare()
    seed_cached_response(adapter, prepared_request, response_cache_control)
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared)

    assert response.status_code == 503
    assert response.from_cache is False


def test_client_stale_if_error_limit_is_stricter_than_response(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=50,
    )
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared_request)

    assert response.status_code == 503
    assert response.raw is not cached
    assert adapter.cache.get(URL) is cached


def test_client_stale_if_error_requires_response_directive(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=100,
    )
    cached = seed_cached_response(adapter, prepared_request, "max-age=60")
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared_request)

    assert response.status_code == 503
    assert response.raw is not cached


def test_request_stale_if_error_limit_is_stricter_than_response(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=True,
    )
    prepared = requests.Request(
        "GET", URL, headers={"Cache-Control": "stale-if-error=50"}
    ).prepare()
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared)

    assert response.status_code == 503
    assert response.raw is not cached
    assert adapter.cache.get(URL) is cached


def test_request_stale_if_error_requires_response_directive(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(
        cache,
        serializer=NullSerializer(),
        stale_if_error=True,
    )
    prepared = requests.Request(
        "GET", URL, headers={"Cache-Control": "stale-if-error=100"}
    ).prepare()
    cached = seed_cached_response(adapter, prepared_request, "max-age=60")
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared)

    assert response.status_code == 503
    assert response.raw is not cached


def test_stale_if_error_is_disabled_by_default(
    prepared_request: PreparedRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(120)
    monkeypatch.setattr("cachecontrol.controller.time.time", clock)
    cache = DictCache()
    adapter = CacheControlAdapter(cache, serializer=NullSerializer())
    cached = seed_cached_response(adapter, prepared_request)
    install_upstream(monkeypatch, [503])

    response = adapter.send(prepared_request)

    assert response.status_code == 503
    assert response.raw is not cached


def test_fresh_cache_hit_reason(
    adapter: CacheControlAdapter,
    prepared_request: PreparedRequest,
) -> None:
    cached = seed_cached_response(adapter, prepared_request)

    response = adapter.send(prepared_request)

    assert response.status_code == 200
    assert response.raw is cached
    assert response.cache_hit_reason == CACHE_HIT_REASON_FRESH


def test_default_adapter_uses_legacy_controller_constructor() -> None:
    class LegacyController:
        def __init__(self, cache, cache_etags=True, serializer=None):
            self.cache = cache
            self.cache_etags = cache_etags
            self.serializer = serializer
            self.stale_if_error_status_codes = ()

        def cached_request(self, request):
            return False

        def conditional_headers(self, request):
            return {}

    CacheControlAdapter(DictCache(), controller_class=LegacyController)
