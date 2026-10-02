# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

import time
from email.utils import formatdate

import msgpack
import pytest
from requests.structures import CaseInsensitiveDict

from cachecontrol.cache import DictCache
from cachecontrol.caches import SeparateBodyFileCache
from cachecontrol.controller import CacheController, VARIANT_INDEX_SUFFIX
from cachecontrol.serialize import Serializer, parse_vary_headers, vary_identity
from tests.utils import DummyRequest, DummyResponse

TIME_FMT = "%a, %d %b %Y %H:%M:%S GMT"
URL = "http://example.com/resource"


def request(language=None):
    headers = {} if language is None else {"Accept-Language": language}
    return DummyRequest(URL, headers)


def response(language, etag, max_age=600, date=None, vary="Accept-Language"):
    headers = {
        "Date": date or formatdate(usegmt=True),
        "Cache-Control": f"max-age={max_age}",
        "ETag": etag,
        "Vary": vary,
    }
    return DummyResponse(200, headers)


def index(controller):
    data = controller.cache.get(URL + VARIANT_INDEX_SUFFIX)
    assert data is not None
    payload = msgpack.loads(data[len(b"ccv=1,") :], raw=False)
    assert payload["version"] == 1
    return payload


class TestBoundedVariants:
    def test_language_variants_remain_separate(self):
        cache = DictCache()
        controller = CacheController(cache)

        date = formatdate(usegmt=True)
        zh_response = response("zh", '"zh"', date=date)
        en_response = response("en", '"en"', date=date)
        controller.cache_response(request("zh"), zh_response, b"chinese")
        controller.cache_response(request("en"), en_response, b"english")

        zh = controller._load_from_cache(request("zh"))
        en = controller._load_from_cache(request("en"))
        assert zh.read() == b"chinese"
        assert en.read() == b"english"
        assert zh.headers["ETag"] == '"zh"'
        assert en.headers["ETag"] == '"en"'
        assert controller.cached_request(request("zh")).read() == b"chinese"
        assert controller.cached_request(request("en")).read() == b"english"

    def test_304_updates_only_validated_variant(self):
        cache = DictCache()
        controller = CacheController(cache)

        date = formatdate(time.time() - 120, usegmt=True)
        controller.cache_response(
            request("zh"), response("zh", '"zh"', max_age=60, date=date), b"chinese"
        )
        controller.cache_response(
            request("en"), response("en", '"en"', max_age=6000, date=date), b"english"
        )

        assert not controller.cached_request(request("zh"))
        assert controller.cached_request(request("en")).read() == b"english"

        not_modified = DummyResponse(
            304,
            {
                "Date": formatdate(time.time() + 1000, usegmt=True),
                "Cache-Control": "max-age=1000",
                "ETag": '"zh-v2"',
            },
        )
        updated = controller.update_cached_response(request("zh"), not_modified)
        en = controller._load_from_cache(request("en"))

        assert updated.read() == b"chinese"
        assert updated.headers["ETag"] == '"zh-v2"'
        assert updated.headers["Date"] == not_modified.headers["Date"]
        assert en.read() == b"english"
        assert en.headers["ETag"] == '"en"'
        assert en.headers["Date"] == date
        assert en.headers["Cache-Control"] == "max-age=6000"

    def test_missing_and_empty_request_header_are_distinct(self):
        headers = CaseInsensitiveDict({"Vary": " Accept-Language, X-Foo "})
        assert parse_vary_headers(headers) == ("accept-language", "x-foo")

        empty = DummyRequest(URL, {"Accept-Language": "", "X-Foo": "x"})
        missing = DummyRequest(URL, {"X-Foo": "x"})
        assert vary_identity(empty, ("accept-language", "x-foo")) == {
            "accept-language": "",
            "x-foo": "x",
        }
        assert vary_identity(missing, ("accept-language", "x-foo")) == {
            "accept-language": None,
            "x-foo": "x",
        }

    def test_vary_star_is_not_stored(self):
        cache = DictCache()
        controller = CacheController(cache)

        controller.cache_response(
            request("zh"), response("zh", '"zh"', vary="*")
        )

        assert cache.data == {}

    def test_variant_limit_evicts_least_recently_used(self):
        cache = DictCache()
        controller = CacheController(cache, max_variants=2)

        controller.cache_response(request("zh"), response("zh", '"zh"'), b"chinese")
        controller.cache_response(request("en"), response("en", '"en"'), b"english")
        controller._load_from_cache(request("zh"))
        controller.cache_response(request("fr"), response("fr", '"fr"'), b"french")

        payload = index(controller)
        identities = [variant["identity"] for variant in payload["variants"]]
        assert identities == [
            {"accept-language": "zh"},
            {"accept-language": "fr"},
        ]
        assert controller._load_from_cache(request("zh")).read() == b"chinese"
        assert controller._load_from_cache(request("en")) is None
        assert controller._load_from_cache(request("fr")).read() == b"french"

    def test_no_store_invalidates_all_variants(self):
        cache = DictCache()
        controller = CacheController(cache)
        controller.cache_response(request("zh"), response("zh", '"zh"'), b"chinese")
        controller.cache_response(request("en"), response("en", '"en"'), b"english")

        no_store = response("zh", '"zh"', max_age=60)
        no_store.headers["Cache-Control"] = "no-store"
        controller.cache_response(request("zh"), no_store, b"chinese")

        assert controller._load_from_cache(request("zh")) is None
        assert controller._load_from_cache(request("en")) is None
        assert cache.data == {}

    def test_changed_vary_set_replaces_old_group(self):
        cache = DictCache()
        controller = CacheController(cache)
        controller.cache_response(request("zh"), response("zh", '"zh"'), b"chinese")
        controller.cache_response(request("en"), response("en", '"en"'), b"english")

        new_response = response("zh", '"zh"', vary="Accept-Language, X-Region")
        request_with_region = DummyRequest(
            URL, {"Accept-Language": "zh", "X-Region": "cn"}
        )
        controller.cache_response(request_with_region, new_response, b"chinese-cn")

        payload = index(controller)
        assert payload["vary"] == ["accept-language", "x-region"]
        assert len(payload["variants"]) == 1
        assert controller._load_from_cache(request("zh")) is None
        assert controller._load_from_cache(request("en")) is None
        assert controller._load_from_cache(request_with_region).read() == b"chinese-cn"

    def test_304_changing_vary_set_replaces_without_losing_body(self):
        cache = DictCache()
        controller = CacheController(cache)
        controller.cache_response(request("zh"), response("zh", '"zh"'), b"chinese")

        not_modified = DummyResponse(
            304,
            {
                "Date": formatdate(usegmt=True),
                "Cache-Control": "max-age=600",
                "ETag": '"zh"',
                "Vary": "Accept-Language, X-Region",
            },
        )
        request_with_region = DummyRequest(
            URL, {"Accept-Language": "zh", "X-Region": "cn"}
        )
        updated = controller.update_cached_response(request_with_region, not_modified)

        assert updated.read() == b"chinese"
        assert controller._load_from_cache(request_with_region).read() == b"chinese"
        assert controller._load_from_cache(request("zh")) is None

    def test_legacy_single_vary_record_is_read_before_migration(self):
        cache = DictCache()
        controller = CacheController(cache)
        legacy_request = request("zh")
        legacy_response = response("zh", '"zh"')
        cache.set(URL, Serializer().dumps(legacy_request, legacy_response, b"chinese"))

        assert controller._load_from_cache(request("zh")).read() == b"chinese"

        controller.cache_response(request("en"), response("en", '"en"'), b"english")
        assert controller._load_from_cache(request("zh")).read() == b"chinese"
        assert controller._load_from_cache(request("en")).read() == b"english"

    def test_legacy_single_vary_record_migrates_separate_body(self, tmp_path):
        cache = SeparateBodyFileCache(str(tmp_path))
        controller = CacheController(cache)
        legacy_request = request("zh")
        legacy_response = response("zh", '"zh"')
        controller._cache_set(
            URL, legacy_request, legacy_response, b"chinese"
        )

        assert cache.get_body(URL).read() == b"chinese"

        controller.cache_response(request("en"), response("en", '"en"'), b"english")

        assert controller._load_from_cache(request("zh")).read() == b"chinese"
        assert controller._load_from_cache(request("en")).read() == b"english"
        assert cache.get_body(URL) is None

    def test_legacy_record_with_changed_vary_set_is_safely_invalidated(self):
        cache = DictCache()
        controller = CacheController(cache)
        legacy_request = request("zh")
        legacy_response = response("zh", '"zh"', vary="Accept-Language")
        cache.set(
            URL,
            Serializer().dumps(legacy_request, legacy_response, b"chinese"),
        )

        new_request = DummyRequest(
            URL, {"Accept-Language": "zh", "X-Region": "cn"}
        )
        new_response = response(
            "zh", '"zh-v2"', vary="Accept-Language, X-Region"
        )
        controller.cache_response(new_request, new_response, b"chinese-cn")

        assert controller._load_from_cache(request("zh")) is None
        assert controller._load_from_cache(new_request).read() == b"chinese-cn"

    def test_legacy_no_vary_record_is_safely_invalidated_by_variant_store(self):
        cache = DictCache()
        controller = CacheController(cache)
        legacy_request = request(None)
        legacy_response = DummyResponse(
            200,
            {"Date": formatdate(usegmt=True), "Cache-Control": "max-age=600"},
        )
        cache.set(URL, Serializer().dumps(legacy_request, legacy_response, b"legacy"))

        controller.cache_response(request("zh"), response("zh", '"zh"'), b"chinese")

        assert controller._load_from_cache(request(None)) is None
        assert controller._load_from_cache(request("zh")).read() == b"chinese"

    @pytest.mark.parametrize(
        "cache_factory",
        [lambda tmp: DictCache(), lambda tmp: SeparateBodyFileCache(str(tmp))],
    )
    def test_304_preserves_separate_body_and_other_variant(
        self, tmp_path, cache_factory
    ):
        cache = cache_factory(tmp_path)
        controller = CacheController(cache)

        date = formatdate(usegmt=True)
        controller.cache_response(
            request("zh"), response("zh", '"zh"', date=date), b"chinese"
        )
        controller.cache_response(
            request("en"), response("en", '"en"', date=date), b"english"
        )

        not_modified = DummyResponse(
            304,
            {
                "Date": date,
                "Cache-Control": "max-age=1000",
                "ETag": '"zh-v2"',
            },
        )
        updated = controller.update_cached_response(request("zh"), not_modified)
        loaded_zh = controller._load_from_cache(request("zh"))
        loaded_en = controller._load_from_cache(request("en"))

        assert updated.read() == b"chinese"
        assert loaded_zh.read() == b"chinese"
        assert loaded_zh.headers["ETag"] == '"zh-v2"'
        assert loaded_en.read() == b"english"
        assert loaded_en.headers["ETag"] == '"en"'
        assert loaded_en.headers["Date"] == date
