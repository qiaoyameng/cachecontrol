# SPDX-FileCopyrightText: 2026 CacheControl contributors
#
# SPDX-License-Identifier: Apache-2.0

import email.utils
import gzip

import pytest
from requests.structures import CaseInsensitiveDict
from urllib3 import HTTPResponse

from cachecontrol.cache import DictCache
from cachecontrol.caches import SeparateBodyFileCache
from cachecontrol.controller import CacheController


class Request:
    def __init__(self, url, method="GET", headers=None):
        self.url = url
        self.method = method
        self.headers = CaseInsensitiveDict(headers or {})


@pytest.fixture(params=[DictCache, SeparateBodyFileCache])
def controller(request, tmp_path):
    if request.param is DictCache:
        cache = DictCache()
    else:
        cache = SeparateBodyFileCache(tmp_path)
    return CacheController(cache)


def store_response(controller, url, body, headers=None, include_length=True):
    response_headers = {
        "Date": email.utils.formatdate(usegmt=True),
        "Cache-Control": "max-age=3600",
        "Content-Length": str(len(body)),
    }
    response_headers.update(headers or {})
    if not include_length:
        response_headers.pop("Content-Length")
    response = HTTPResponse(
        body=body,
        headers=response_headers,
        status=200,
        reason="OK",
        preload_content=False,
        decode_content=False,
    )
    controller.cache_response(Request(url), response, body)
    return response_headers


def cached_range(controller, url, range_header, **headers):
    return controller.cached_request(
        Request(url, headers={"Range": range_header, **headers})
    )


@pytest.mark.parametrize(
    ("range_header", "status", "content_range", "expected"),
    [
        ("bytes=2-4", 206, "bytes 2-4/10", b"234"),
        ("bytes=7-", 206, "bytes 7-9/10", b"789"),
        ("bytes=-3", 206, "bytes 7-9/10", b"789"),
        ("bytes=8-20", 206, "bytes 8-9/10", b"89"),
        ("bytes=10-", 416, "bytes */10", b""),
        ("bytes=-0", 416, "bytes */10", b""),
    ],
)
def test_single_range_uses_fresh_complete_cache(
    controller, range_header, status, content_range, expected
):
    url = "http://example.com/fixed"
    body = b"0123456789"
    store_response(controller, url, body)

    response = cached_range(controller, url, range_header)

    assert response is not False
    assert response.status == status
    assert response.headers["Content-Range"] == content_range
    assert response.headers["Content-Length"] == str(len(expected))
    assert response.read(decode_content=False) == expected


def test_range_uses_encoded_representation_bytes(controller):
    url = "http://example.com/encoded"
    unencoded = b"x" * 100
    encoded = gzip.compress(unencoded, mtime=0)
    store_response(
        controller,
        url,
        encoded,
        {"Content-Encoding": "gzip"},
    )

    response = cached_range(controller, url, "bytes=10-14")

    assert response.status == 206
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Content-Range"] == f"bytes 10-14/{len(encoded)}"
    assert response.read(decode_content=False) == encoded[10:15]


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("If-Range", '"v1"', 206),
        ("If-Range", 'W/"v1"', 200),
        ("If-Range", 'w/"v1"', 200),
        ("If-Range", '"other"', 200),
    ],
)
def test_if_range_etag_uses_strong_comparison(controller, header, value, status):
    url = "http://example.com/etag"
    store_response(controller, url, b"0123456789", {"ETag": '"v1"'})

    response = cached_range(controller, url, "bytes=2-4", **{header: value})

    assert response.status == status
    if status == 206:
        assert response.read(decode_content=False) == b"234"
    else:
        assert "Content-Range" not in response.headers
        assert response.read(decode_content=False) == b"0123456789"


def test_if_range_date_must_equal_last_modified(controller):
    url = "http://example.com/date"
    last_modified = "Wed, 21 Oct 2015 07:28:00 GMT"
    store_response(
        controller,
        url,
        b"0123456789",
        {"Last-Modified": last_modified},
    )

    matching = cached_range(
        controller, url, "bytes=2-4", **{"If-Range": last_modified}
    )
    assert matching.status == 206

    different = cached_range(
        controller,
        url,
        "bytes=2-4",
        **{"If-Range": "Wed, 21 Oct 2015 07:28:01 GMT"},
    )
    assert different.status == 200
    assert different.read(decode_content=False) == b"0123456789"


def test_if_range_requires_usable_validator(controller):
    url = "http://example.com/no-validator"
    store_response(controller, url, b"0123456789")

    response = cached_range(
        controller,
        url,
        "bytes=2-4",
        **{"If-Range": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )

    assert response.status == 200
    assert "Content-Range" not in response.headers


def test_multipart_range_goes_to_network_without_consuming_cached_body(controller):
    url = "http://example.com/multipart"
    store_response(controller, url, b"0123456789")
    request = Request(url, headers={"Range": "bytes=0-1,3-4"})

    assert controller.cached_request(request) is False
    assert controller.conditional_headers(request) == {}


def test_head_range_does_not_consume_cached_body(controller):
    url = "http://example.com/head"
    store_response(controller, url, b"0123456789")
    request = Request(url, method="HEAD", headers={"Range": "bytes=2-4"})

    response = controller.cached_request(request)

    assert response.status == 200
    assert "Content-Range" not in response.headers
    assert response._fp is not None
    assert response._fp.tell() == 0


def test_range_without_content_length_uses_seekable_cache_length(controller):
    url = "http://example.com/chunked-origin"
    store_response(
        controller,
        url,
        b"0123456789",
        {"Transfer-Encoding": "chunked"},
        include_length=False,
    )

    response = cached_range(controller, url, "bytes=-4")

    assert response.status == 206
    assert response.headers["Content-Range"] == "bytes 6-9/10"
    assert response.headers["Content-Length"] == "4"
    assert "Transfer-Encoding" not in response.headers
    assert response.read(decode_content=False) == b"6789"


def test_stale_single_range_revalidates_but_multipart_does_not(controller):
    url = "http://example.com/stale"
    store_response(
        controller,
        url,
        b"0123456789",
        {
            "ETag": '"v1"',
            "Date": "Tue, 26 Nov 2012 00:50:49 GMT",
            "Cache-Control": "max-age=0",
        },
    )

    single = Request(url, headers={"Range": "bytes=2-4"})
    assert controller.cached_request(single) is False
    assert controller.conditional_headers(single)["If-None-Match"] == '"v1"'

    last_modified_url = "http://example.com/stale-last-modified"
    last_modified = "Wed, 21 Oct 2015 07:28:00 GMT"
    store_response(
        controller,
        last_modified_url,
        b"0123456789",
        {"Last-Modified": last_modified},
    )
    assert (
        controller.conditional_headers(
            Request(last_modified_url, headers={"Range": "bytes=2-4"})
        )["If-Modified-Since"]
        == last_modified
    )

    multipart = Request(url, headers={"Range": "bytes=0-1,3-4"})
    assert controller.cached_request(multipart) is False
    assert controller.conditional_headers(multipart) == {}


def test_network_partial_response_does_not_replace_complete_cache(controller):
    url = "http://example.com/network-partial"
    store_response(controller, url, b"0123456789")
    partial = HTTPResponse(
        body=b"234",
        headers={
            "Date": email.utils.formatdate(usegmt=True),
            "Cache-Control": "max-age=3600",
            "Content-Length": "3",
            "Content-Range": "bytes 2-4/10",
        },
        status=206,
        reason="Partial Content",
        preload_content=False,
        decode_content=False,
    )

    controller.cache_response(
        Request(url), partial, b"234", status_codes=(206,)
    )

    cached = controller._load_from_cache(Request(url))
    assert cached.status == 200
    assert cached.read(decode_content=False) == b"0123456789"
