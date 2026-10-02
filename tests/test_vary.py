# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

from pprint import pprint
import time
from urllib.parse import urljoin

import pytest
import requests
from requests.structures import CaseInsensitiveDict
from urllib3 import HTTPResponse

from cachecontrol import CacheControl
from cachecontrol.cache import DictCache
from cachecontrol.controller import CacheController
from cachecontrol.serialize import Serializer


class TestVary:
    @pytest.fixture()
    def sess(self, url):
        self.url = urljoin(url, "/vary_accept")
        self.cache = DictCache()
        sess = CacheControl(requests.Session(), cache=self.cache)
        return sess

    def cached_equal(self, cached, resp):
        # remove any transfer-encoding headers as they don't apply to
        # a cached value
        if "chunked" in resp.raw.headers.get("transfer-encoding", ""):
            resp.raw.headers.pop("transfer-encoding")

        checks = [
            cached._fp.getvalue() == resp.content,
            cached.headers == resp.raw.headers,
            cached.status == resp.raw.status,
            cached.version == resp.raw.version,
            cached.reason == resp.raw.reason,
            cached.decode_content == resp.raw.decode_content,
        ]

        print(checks)
        pprint(dict(cached.headers))
        pprint(dict(resp.raw.headers))
        return all(checks)

    def test_vary_example(self, sess):
        """RFC 2616 13.6

        When the cache receives a subsequent request whose Request-URI
        specifies one or more cache entries including a Vary header field,
        the cache MUST NOT use such a cache entry to construct a response
        to the new request unless all of the selecting request-headers
        present in the new request match the corresponding stored
        request-headers in the original request.

        Or, in simpler terms, when you make a request and the server
        returns defines a Vary header, unless all the headers listed
        in the Vary header are the same, it won't use the cached
        value.
        """
        s = sess.adapters["http://"].controller.serializer
        r = sess.get(self.url)
        c = s.loads(r.request, self.cache.get(self.url))

        # make sure we cached it
        assert self.cached_equal(c, r)

        # make the same request
        resp = sess.get(self.url)
        assert self.cached_equal(c, resp)
        assert resp.from_cache

        # make a similar request, changing the accept header
        resp = sess.get(self.url, headers={"Accept": "text/plain, text/html"})
        assert not self.cached_equal(c, resp)
        assert not resp.from_cache

        # Just confirming two things here:
        #
        #   1) The server used the vary header
        #   2) We have more than one header we vary on
        #
        # The reason for this is that when we don't specify the header
        # in the request, it is considered the same in terms of
        # whether or not to use the cached value.
        assert "vary" in r.headers
        assert len(r.headers["vary"].replace(" ", "").split(",")) == 2


class TestVaryVariants:
    url = "http://example.com/resource"

    def request(self, language=None):
        headers = {}
        if language is not None:
            headers["Accept-Language"] = language
        return type(
            "Request",
            (),
            {"url": self.url, "headers": CaseInsensitiveDict(headers)},
        )()

    def response(self, language, etag, max_age):
        headers = {
            "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
            "Cache-Control": f"max-age={max_age}",
            "Expires": time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT",
                time.gmtime(1000 + max_age),
            ),
            "Vary": "Accept-Language",
            "ETag": etag,
        }
        return HTTPResponse(
            body=language.encode(),
            headers=headers,
            status=200,
            preload_content=False,
        )

    def test_language_variants_are_stored_independently_and_304_updates_one(self):
        controller = CacheController(DictCache())
        zh_request = self.request("zh")
        en_request = self.request("en")

        controller.cache_response(
            zh_request,
            self.response("zh", '"zh-etag"', 100),
            b"zh",
        )
        controller.cache_response(
            en_request,
            self.response("en", '"en-etag"', 200),
            b"en",
        )

        not_modified = HTTPResponse(
            headers={
                "ETag": '"en-etag-2"',
                "Cache-Control": "max-age=300",
            },
            status=304,
            preload_content=False,
        )
        updated = controller.update_cached_response(en_request, not_modified)

        assert updated.read() == b"en"
        assert updated.headers["ETag"] == '"en-etag-2"'
        assert updated.headers["Cache-Control"] == "max-age=300"

        chinese = controller._load_from_cache(zh_request)
        assert chinese is not None
        assert chinese.read() == b"zh"
        assert chinese.headers["ETag"] == '"zh-etag"'
        assert chinese.headers["Cache-Control"] == "max-age=100"
        assert chinese.headers["Expires"] == "Thu, 01 Jan 1970 00:18:20 GMT"

    def test_missing_and_empty_vary_headers_are_distinct_identities(self):
        controller = CacheController(DictCache())
        missing_request = self.request(None)
        empty_request = self.request("")

        controller.cache_response(
            missing_request,
            self.response("missing", '"missing"', 100),
            b"missing",
        )
        controller.cache_response(
            empty_request,
            self.response("empty", '"empty"', 200),
            b"empty",
        )

        assert controller._load_from_cache(missing_request).read() == b"missing"
        assert controller._load_from_cache(empty_request).read() == b"empty"

    def test_variants_are_bounded_by_fifo_replacement(self):
        controller = CacheController(DictCache(), max_variants=2)

        controller.cache_response(
            self.request("zh"), self.response("zh", '"zh"', 100), b"zh"
        )
        controller.cache_response(
            self.request("en"), self.response("en", '"en"', 100), b"en"
        )
        controller.cache_response(
            self.request("fr"), self.response("fr", '"fr"', 100), b"fr"
        )

        assert controller._load_from_cache(self.request("zh")) is None
        assert controller._load_from_cache(self.request("en")).read() == b"en"
        assert controller._load_from_cache(self.request("fr")).read() == b"fr"

    def test_vary_set_change_replaces_all_old_variants(self):
        controller = CacheController(DictCache())
        controller.cache_response(
            self.request("zh"), self.response("zh", '"zh"', 100), b"zh"
        )

        def response_for(field):
            return HTTPResponse(
                body=b"plain",
                headers={
                    "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
                    "Cache-Control": "max-age=100",
                    "Vary": field,
                    "ETag": '"plain"',
                },
                status=200,
                preload_content=False,
            )

        accept_request = self.request(None)
        accept_request.headers["Accept"] = "text/plain"
        controller.cache_response(accept_request, response_for("Accept"), b"plain")

        assert controller._load_from_cache(self.request("zh")) is None
        assert controller._load_from_cache(accept_request).read() == b"plain"

    def test_no_store_invalidates_all_variants(self):
        controller = CacheController(DictCache())
        controller.cache_response(
            self.request("zh"), self.response("zh", '"zh"', 100), b"zh"
        )

        not_stored = HTTPResponse(
            headers={"Cache-Control": "no-store"},
            status=200,
            preload_content=False,
        )
        controller.cache_response(self.request("en"), not_stored, b"")

        assert not controller.cache.data
        assert controller._load_from_cache(self.request("zh")) is None

    def test_vary_star_is_never_stored(self):
        controller = CacheController(DictCache())
        response = HTTPResponse(
            body=b"star",
            headers={
                "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
                "Cache-Control": "max-age=100",
                "Vary": "*",
                "ETag": '"star"',
            },
            status=200,
            preload_content=False,
        )

        controller.cache_response(self.request("zh"), response, b"star")

        assert not controller.cache.data
        assert controller._load_from_cache(self.request("zh")) is None

    def test_legacy_single_record_is_migrated_when_second_variant_arrives(self):
        cache = DictCache()
        serializer = Serializer()
        controller = CacheController(cache, serializer=serializer)
        zh_request = self.request("zh")
        en_request = self.request("en")
        zh_response = self.response("zh", '"zh"', 100)

        cache.set(self.url, serializer.dumps(zh_request, zh_response, b"zh"))
        controller.cache_response(
            en_request, self.response("en", '"en"', 200), b"en"
        )

        assert controller._load_from_cache(zh_request).read() == b"zh"
        assert controller._load_from_cache(en_request).read() == b"en"

    def test_legacy_vary_star_record_is_not_served_and_can_be_replaced(self):
        cache = DictCache()
        serializer = Serializer()
        zh_request = self.request("zh")
        star_response = HTTPResponse(
            body=b"star",
            headers={
                "Date": "Thu, 01 Jan 1970 00:16:40 GMT",
                "Cache-Control": "max-age=100",
                "Vary": "*",
                "ETag": '"star"',
            },
            status=200,
            preload_content=False,
        )
        cache.set(self.url, serializer.dumps(zh_request, star_response, b"star"))
        controller = CacheController(cache)

        assert controller._load_from_cache(zh_request) is None

        controller.cache_response(
            zh_request, self.response("zh", '"zh"', 100), b"zh"
        )

        assert controller._load_from_cache(zh_request).read() == b"zh"
