# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
The httplib2 algorithms ported for use with requests.
"""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import re
import threading
import time
import weakref
from email.utils import parsedate_tz
from typing import TYPE_CHECKING, Any, Collection, Mapping

from requests.structures import CaseInsensitiveDict

from cachecontrol.cache import DictCache, SeparateBodyBaseCache
from cachecontrol.serialize import Serializer, parse_vary_headers, vary_identity

if TYPE_CHECKING:
    from typing import Literal

    from requests import PreparedRequest
    from urllib3 import HTTPResponse

    from cachecontrol.cache import BaseCache

logger = logging.getLogger(__name__)

URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")

PERMANENT_REDIRECT_STATUSES = (301, 308)
VARIANT_INDEX_SUFFIX = "#cachecontrol-variants"
VARIANT_KEY_PREFIX = "#cachecontrol-variant:"


class _CacheEntry:
    def __init__(
        self,
        key: str,
        response: HTTPResponse,
        identity: dict[str, str | None],
    ) -> None:
        self.key = key
        self.response = response
        self.identity = identity


def parse_uri(uri: str) -> tuple[str, str, str, str, str]:
    """Parses a URI using the regex given in Appendix B of RFC 3986.

    (scheme, authority, path, query, fragment) = parse_uri(uri)
    """
    match = URI.match(uri)
    assert match is not None
    groups = match.groups()
    return (groups[1], groups[3], groups[4], groups[6], groups[8])


class CacheController:
    """An interface to see if request should cached or not."""

    def __init__(
        self,
        cache: BaseCache | None = None,
        cache_etags: bool = True,
        serializer: Serializer | None = None,
        status_codes: Collection[int] | None = None,
        max_variants: int = 16,
    ):
        self.cache = DictCache() if cache is None else cache
        self.cache_etags = cache_etags
        self.serializer = serializer or Serializer()
        self.cacheable_status_codes = status_codes or (200, 203, 300, 301, 308)
        if max_variants < 1:
            raise ValueError("max_variants must be at least 1")
        self.max_variants = max_variants
        self._variants_lock = threading.RLock()

    @classmethod
    def _urlnorm(cls, uri: str) -> str:
        """Normalize the URL to create a safe key for the cache"""
        (scheme, authority, path, query, fragment) = parse_uri(uri)
        if not scheme or not authority:
            raise Exception("Only absolute URIs are allowed. uri = %s" % uri)

        scheme = scheme.lower()
        authority = authority.lower()

        if not path:
            path = "/"

        # Could do syntax based normalization of the URI before
        # computing the digest. See Section 6.2.2 of Std 66.
        request_uri = query and "?".join([path, query]) or path
        defrag_uri = scheme + "://" + authority + request_uri

        return defrag_uri

    @classmethod
    def cache_url(cls, uri: str) -> str:
        return cls._urlnorm(uri)

    @staticmethod
    def _variant_index_key(cache_url: str) -> str:
        return cache_url + VARIANT_INDEX_SUFFIX

    @staticmethod
    def _variant_key(cache_url: str, identity: Mapping[str, str | None]) -> str:
        raw = "\n".join(f"{key}={value!r}" for key, value in identity.items())
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return cache_url + VARIANT_KEY_PREFIX + digest

    def _load_index(self, cache_url: str) -> dict[str, Any] | None:
        data = self.cache.get(self._variant_index_key(cache_url))
        if not isinstance(data, (bytes, bytearray)):
            return None
        index = self.serializer.loads_index(data)
        if not isinstance(index, dict):
            logger.debug("Variant index deserialization failed, entry ignored")
            return None
        return index

    @staticmethod
    def _index_identity(variant: Mapping[str, Any]) -> dict[str, str | None]:
        identity = variant.get("identity", {})
        return (
            {str(key): value for key, value in identity.items()}
            if isinstance(identity, dict)
            else {}
        )

    def _identity_matches(
        self,
        identity: Mapping[str, str | None],
        request: PreparedRequest,
    ) -> bool:
        return all(
            request.headers.get(header, None) == value
            for header, value in identity.items()
        )

    def _load_entry_unchecked(
        self,
        key: str,
        identity: Mapping[str, str | None],
        request: PreparedRequest,
    ) -> _CacheEntry | None:
        cache_data = self.cache.get(key)
        if cache_data is None:
            return None

        if isinstance(self.cache, SeparateBodyBaseCache):
            body_file = self.cache.get_body(key)
        else:
            body_file = None

        result = self.serializer.loads(request, cache_data, body_file)
        if result is None:
            return None
        return _CacheEntry(key, result, dict(identity))

    def _touch_index(self, cache_url: str, index: dict[str, Any], key: str) -> None:
        variants = index["variants"]
        variants.sort(key=lambda variant: variant.get("key") != key, reverse=True)
        self._write_index(cache_url, index)

    def _load_cache_entry(self, request: PreparedRequest) -> _CacheEntry | None:
        assert request.url is not None
        cache_url = self.cache_url(request.url)
        assert cache_url is not None
        index = self._load_index(cache_url)

        if index is not None:
            for variant in index["variants"]:
                key = variant.get("key")
                if not isinstance(key, str):
                    continue
                identity = self._index_identity(variant)
                if self._identity_matches(identity, request):
                    entry = self._load_entry_unchecked(key, identity, request)
                    if entry is not None:
                        self._touch_index(cache_url, index, key)
                        return entry
                    variants = [
                        variant
                        for variant in index["variants"]
                        if variant.get("key") != key
                    ]
                    if variants:
                        index["variants"] = variants
                        self._write_index(cache_url, index)
                    else:
                        self._delete_variant_group(cache_url, index)
                    return None
            return None

        cache_data = self.cache.get(cache_url)
        if cache_data is None:
            return None
        if isinstance(cache_data, (bytes, bytearray)):
            cached_vary = self.serializer.loads_vary(cache_data)
            if cached_vary is None:
                return None
        else:
            cached_vary = {}
        if not self._identity_matches(cached_vary, request):
            return None

        if isinstance(self.cache, SeparateBodyBaseCache):
            body_file = self.cache.get_body(cache_url)
        else:
            body_file = None
        result = self.serializer.loads(request, cache_data, body_file)
        if result is None:
            return None
        return _CacheEntry(cache_url, result, cached_vary)

    def _delete_cache_key(self, key: str) -> None:
        self.cache.delete(key)

    def _delete_variant_group(self, cache_url: str, index: dict[str, Any]) -> None:
        for variant in index.get("variants", []):
            key = variant.get("key")
            if isinstance(key, str):
                self._delete_cache_key(key)
        self._delete_cache_key(self._variant_index_key(cache_url))

    def invalidate_cache(self, cache_url: str) -> None:
        with self._variants_lock:
            index = self._load_index(cache_url)
            if index is not None:
                self._delete_variant_group(cache_url, index)
            self._delete_cache_key(cache_url)

    def _remove_cache_entry(self, cache_url: str, entry: _CacheEntry) -> None:
        index = self._load_index(cache_url)
        if index is None:
            self._delete_cache_key(entry.key)
            return

        variants = [
            variant
            for variant in index["variants"]
            if variant.get("key") != entry.key
        ]
        self._delete_cache_key(entry.key)
        if variants:
            index["variants"] = variants
            self._write_index(cache_url, index)
        else:
            self._delete_cache_key(self._variant_index_key(cache_url))

    def _invalidate_variants_for_direct_write(self, cache_url: str) -> None:
        index = self._load_index(cache_url)
        if index is not None:
            self._delete_variant_group(cache_url, index)

    @staticmethod
    def _index_expires(index: Mapping[str, Any]) -> int | None:
        expires_at = [
            variant["expires_at"]
            for variant in index["variants"]
            if isinstance(variant, dict) and variant.get("expires_at") is not None
        ]
        if not expires_at:
            return None
        return max(1, int(max(expires_at) - time.time()))

    def _write_index(self, cache_url: str, index: dict[str, Any]) -> None:
        self.cache.set(
            self._variant_index_key(cache_url),
            self.serializer.dumps_index(index),
            expires=self._index_expires(index),
        )

    def _write_cache_entry(
        self,
        key: str,
        request: PreparedRequest,
        response: HTTPResponse,
        body: bytes | None,
        expires_time: int | None,
    ) -> None:
        if isinstance(self.cache, SeparateBodyBaseCache):
            metadata = self.serializer.dumps(request, response, b"")
            self.cache.set(key, metadata, expires=expires_time)
            if body is not None:
                self.cache.set_body(key, body)
        else:
            metadata = self.serializer.dumps(request, response, body)
            self.cache.set(key, metadata, expires=expires_time)

    def _migrate_legacy_variant(
        self,
        cache_url: str,
        index: dict[str, Any],
        vary_headers: tuple[str, ...],
    ) -> None:
        cache_data = self.cache.get(cache_url)
        if cache_data is None:
            return
        cached_vary = self.serializer.loads_vary(cache_data)
        if not cached_vary:
            return
        key = self._variant_key(cache_url, cached_vary)
        legacy_body = None
        if isinstance(self.cache, SeparateBodyBaseCache):
            body_file = self.cache.get_body(cache_url)
            if body_file is not None:
                legacy_body = body_file.read()

        self.cache.set(key, cache_data)
        if legacy_body is not None:
            self.cache.set_body(key, legacy_body)
        index["variants"].append(
            {
                "key": key,
                "identity": cached_vary,
                "expires_at": None,
                "accessed": time.monotonic(),
            }
        )
        self._delete_cache_key(cache_url)

    def _set_variant(
        self,
        cache_url: str,
        request: PreparedRequest,
        response: HTTPResponse,
        body: bytes | None,
        expires_time: int | None,
        vary_headers: tuple[str, ...],
    ) -> None:
        identity = vary_identity(request, vary_headers)
        key = self._variant_key(cache_url, identity)
        index = self._load_index(cache_url)

        if index is None:
            index = {"vary": list(vary_headers), "variants": []}
            legacy_data = self.cache.get(cache_url)
            if legacy_data is not None:
                if not isinstance(legacy_data, (bytes, bytearray)):
                    self._delete_cache_key(cache_url)
                    legacy_data = None
                if legacy_data is not None:
                    legacy_vary = self.serializer.loads_vary(legacy_data)
                    if not legacy_vary:
                        logger.debug("Unreadable legacy cache entry invalidated")
                        self._delete_cache_key(cache_url)
                    elif set(legacy_vary) != set(vary_headers):
                        logger.debug(
                            "Legacy variant invalidated due to changed Vary header set"
                        )
                        self._delete_cache_key(cache_url)
                    else:
                        self._migrate_legacy_variant(
                            cache_url, index, vary_headers
                        )
        elif index.get("vary") != list(vary_headers):
            logger.debug("Replacing variants due to changed Vary header set")
            self._delete_variant_group(cache_url, index)
            index = {"vary": list(vary_headers), "variants": []}

        variants = index["variants"]
        existing = next(
            (variant for variant in variants if variant.get("key") == key), None
        )
        if existing is not None:
            variants.remove(existing)

        variants.append(
            {
                "key": key,
                "identity": identity,
                "expires_at": (
                    None if expires_time is None else time.time() + expires_time
                ),
                "accessed": time.monotonic(),
            }
        )

        while len(variants) > self.max_variants:
            evicted = variants.pop(0)
            evicted_key = evicted.get("key")
            if isinstance(evicted_key, str):
                logger.debug(
                    "Evicting least recently used cache variant: %s", evicted_key
                )
                self._delete_cache_key(evicted_key)

        self._write_cache_entry(key, request, response, body, expires_time)
        self._write_index(cache_url, index)

    def parse_cache_control(self, headers: Mapping[str, str]) -> dict[str, int | None]:
        known_directives = {
            # https://tools.ietf.org/html/rfc7234#section-5.2
            "max-age": (int, True),
            "max-stale": (int, False),
            "min-fresh": (int, True),
            "no-cache": (None, False),
            "no-store": (None, False),
            "no-transform": (None, False),
            "only-if-cached": (None, False),
            "must-revalidate": (None, False),
            "public": (None, False),
            "private": (None, False),
            "proxy-revalidate": (None, False),
            "s-maxage": (int, True),
        }

        cc_headers = headers.get("cache-control", headers.get("Cache-Control", ""))

        retval: dict[str, int | None] = {}

        for cc_directive in cc_headers.split(","):
            if not cc_directive.strip():
                continue

            parts = cc_directive.split("=", 1)
            directive = parts[0].strip().lower()

            try:
                typ, required = known_directives[directive]
            except KeyError:
                logger.debug("Ignoring unknown cache-control directive: %s", directive)
                continue

            if not typ or not required:
                retval[directive] = None
            if typ:
                try:
                    retval[directive] = typ(parts[1].strip())
                except IndexError:
                    if required:
                        logger.debug(
                            "Missing value for cache-control directive: %s",
                            directive,
                        )
                except ValueError:
                    logger.debug(
                        "Invalid value for cache-control directive %s, must be %s",
                        directive,
                        typ.__name__,
                    )

        return retval

    def _load_from_cache(self, request: PreparedRequest) -> HTTPResponse | None:
        """
        Load a cached response, or return None if it's not available.
        """
        # We do not support caching of partial content: so if the request contains a
        # Range header then we don't want to load anything from the cache.
        if "Range" in request.headers:
            return None

        entry = self._load_cache_entry(request)
        if entry is None:
            logger.debug("Cache entry deserialization failed, entry ignored")
            return None
        return entry.response

    def cached_request(self, request: PreparedRequest) -> HTTPResponse | Literal[False]:
        """
        Return a cached response if it exists in the cache, otherwise
        return False.
        """
        assert request.url is not None
        cache_url = self.cache_url(request.url)
        logger.debug('Looking up "%s" in the cache', cache_url)
        cc = self.parse_cache_control(request.headers)

        # Bail out if the request insists on fresh data
        if "no-cache" in cc:
            logger.debug('Request header has "no-cache", cache bypassed')
            return False

        if "max-age" in cc and cc["max-age"] == 0:
            logger.debug('Request header has "max_age" as 0, cache bypassed')
            return False

        # Check whether we can load the response from the cache:
        entry = self._load_cache_entry(request)
        if not entry:
            return False
        resp = entry.response

        # Apply normal freshness checks to every cached response. In the case
        # of permanent redirects, RFC 9110 sections 15.4.2 and 15.4.9 permit
        # heuristic caching, but RFC 9111 section 4.2.2 requires explicit
        # expiration to take precedence. The redirect fallback below is used
        # only when neither max-age nor Expires provides an expiration.
        headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(resp.headers)
        if not headers or "date" not in headers:
            if "etag" not in headers:
                # Without date or etag, the cached response can never be used
                # and should be deleted.
                logger.debug("Purging cached response: no date or etag")
                self._remove_cache_entry(cache_url, entry)
            logger.debug("Ignoring cached response: no date")
            return False

        now = time.time()
        time_tuple = parsedate_tz(headers["date"])
        if time_tuple is None:
            logger.debug("Ignoring cached response: invalid date")
            return False
        date = calendar.timegm(time_tuple[:6])
        current_age = max(0, now - date)
        logger.debug("Current age based on date: %i", current_age)

        # TODO: There is an assumption that the result will be a
        #       urllib3 response object. This may not be best since we
        #       could probably avoid instantiating or constructing the
        #       response until we know we need it.
        resp_cc = self.parse_cache_control(headers)

        # determine freshness
        freshness_lifetime = 0

        # Check the max-age pragma in the cache control header
        max_age = resp_cc.get("max-age")
        if max_age is not None:
            freshness_lifetime = max_age
            logger.debug("Freshness lifetime from max-age: %i", freshness_lifetime)

        # If there isn't a max-age, check for an expires header
        elif "expires" in headers:
            expires = parsedate_tz(headers["expires"])
            if expires is not None:
                expire_time = calendar.timegm(expires[:6]) - date
                freshness_lifetime = max(0, expire_time)
                logger.debug("Freshness lifetime from expires: %i", freshness_lifetime)

        # Permanent redirects are heuristically cacheable when the response
        # does not provide an explicit freshness lifetime.
        elif int(resp.status) in PERMANENT_REDIRECT_STATUSES:
            logger.debug("Returning cached permanent redirect response")
            return resp

        # Determine if we are setting freshness limit in the
        # request. Note, this overrides what was in the response.
        max_age = cc.get("max-age")
        if max_age is not None:
            freshness_lifetime = max_age
            logger.debug(
                "Freshness lifetime from request max-age: %i", freshness_lifetime
            )

        min_fresh = cc.get("min-fresh")
        if min_fresh is not None:
            # adjust our current age by our min fresh
            current_age += min_fresh
            logger.debug("Adjusted current age from min-fresh: %i", current_age)

        # Return entry if it is fresh enough
        if freshness_lifetime > current_age:
            logger.debug('The response is "fresh", returning cached response')
            logger.debug("%i > %i", freshness_lifetime, current_age)
            return resp

        # we're not fresh. If we don't have an Etag, clear it out
        if "etag" not in headers:
            logger.debug('The cached response is "stale" with no etag, purging')
            self._remove_cache_entry(cache_url, entry)

        # return the original handler
        return False

    def conditional_headers(self, request: PreparedRequest) -> dict[str, str]:
        resp = self._load_from_cache(request)
        new_headers = {}

        if resp:
            headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(resp.headers)

            if "etag" in headers:
                new_headers["If-None-Match"] = headers["ETag"]

            if "last-modified" in headers:
                new_headers["If-Modified-Since"] = headers["Last-Modified"]

        return new_headers

    def _cache_set(
        self,
        cache_url: str,
        request: PreparedRequest,
        response: HTTPResponse,
        body: bytes | None = None,
        expires_time: int | None = None,
        vary_headers: tuple[str, ...] | None = None,
    ) -> None:
        """Store the data in the cache.
        """
        if expires_time is not None and expires_time <= 0:
            # Already stale on arrival
            logger.debug("Purging cached response: expires in the past")
            self.invalidate_cache(cache_url)
            return

        with self._variants_lock:
            if vary_headers is None or not vary_headers:
                self._invalidate_variants_for_direct_write(cache_url)
                self._write_cache_entry(
                    cache_url, request, response, body, expires_time
                )
            elif vary_headers:
                self._set_variant(
                    cache_url,
                    request,
                    response,
                    body,
                    expires_time,
                    vary_headers,
                )

    def cache_response(
        self,
        request: PreparedRequest,
        response_or_ref: HTTPResponse | weakref.ReferenceType[HTTPResponse],
        body: bytes | None = None,
        status_codes: Collection[int] | None = None,
    ) -> None:
        """
        Algorithm for caching requests.

        This assumes a requests Response object.
        """
        if isinstance(response_or_ref, weakref.ReferenceType):
            response = response_or_ref()
            if response is None:
                # The weakref can be None only in case the user used streamed request
                # and did not consume or close it, and holds no reference to requests.Response.
                # In such case, we don't want to cache the response.
                return
        else:
            response = response_or_ref

        # From httplib2: Don't cache 206's since we aren't going to
        #                handle byte range requests
        cacheable_status_codes = status_codes or self.cacheable_status_codes
        if response.status not in cacheable_status_codes:
            logger.debug(
                "Status code %s not in %s", response.status, cacheable_status_codes
            )
            return

        response_headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(
            response.headers
        )

        if "date" in response_headers:
            time_tuple = parsedate_tz(response_headers["date"])
            date = calendar.timegm(time_tuple[:6]) if time_tuple is not None else 0
        else:
            date = 0

        # If we've been given a body, our response has a Content-Length, that
        # Content-Length is valid then we can check to see if the body we've
        # been given matches the expected size, and if it doesn't we'll just
        # skip trying to cache it.
        if (
            body is not None
            and "content-length" in response_headers
            and response_headers["content-length"].isdigit()
            and int(response_headers["content-length"]) != len(body)
        ):
            return

        cc_req = self.parse_cache_control(request.headers)
        cc = self.parse_cache_control(response_headers)

        assert request.url is not None
        cache_url = self.cache_url(request.url)
        logger.debug('Updating cache with response from "%s"', cache_url)

        # Delete it from the cache if we happen to have it stored there
        no_store = False
        if "no-store" in cc:
            no_store = True
            logger.debug('Response header has "no-store"')
        if "no-store" in cc_req:
            no_store = True
            logger.debug('Request header has "no-store"')
        if no_store:
            logger.debug('Purging existing cache entry to honor "no-store"')
            self.invalidate_cache(cache_url)
            return

        vary_headers = parse_vary_headers(response_headers)
        if vary_headers is None:
            logger.debug('Response header has "Vary: *"')
            return

        # If we've been given an etag, then keep the response
        if self.cache_etags and "etag" in response_headers:
            expires_time = 0
            if response_headers.get("expires"):
                expires = parsedate_tz(response_headers["expires"])
                if expires is not None:
                    expires_time = calendar.timegm(expires[:6]) - date

            expires_time = max(expires_time, 14 * 86400)

            logger.debug(f"etag object cached for {expires_time} seconds")
            logger.debug("Caching due to etag")
            self._cache_set(
                cache_url,
                request,
                response,
                body,
                expires_time,
                vary_headers,
            )

        # Add to the cache any permanent redirects. We do this before looking
        # that the Date headers.
        elif int(response.status) in PERMANENT_REDIRECT_STATUSES:
            logger.debug("Caching permanent redirect")
            self._cache_set(
                cache_url, request, response, b"", vary_headers=vary_headers
            )

        # Add to the cache if the response headers demand it. If there
        # is no date header then we can't do anything about expiring
        # the cache.
        elif "date" in response_headers:
            time_tuple = parsedate_tz(response_headers["date"])
            if time_tuple is None:
                return
            date = calendar.timegm(time_tuple[:6])
            # cache when there is a max-age > 0
            max_age = cc.get("max-age")
            if max_age is not None and max_age > 0:
                logger.debug("Caching b/c date exists and max-age > 0")
                expires_time = max_age
                self._cache_set(
                    cache_url,
                    request,
                    response,
                    body,
                    expires_time,
                    vary_headers,
                )

            # If the request can expire, it means we should cache it
            # in the meantime.
            elif "expires" in response_headers:
                if response_headers["expires"]:
                    expires = parsedate_tz(response_headers["expires"])
                    if expires is None:
                        # https://tools.ietf.org/html/rfc9111#section-5.3: an
                        # invalid Expires must be read as a time in the past.
                        expires_time = 0
                    else:
                        expires_time = calendar.timegm(expires[:6]) - date

                    # A non-positive lifetime here means the response arrived
                    # stale
                    logger.debug(
                        "Caching b/c of expires header. expires in {} seconds".format(
                            expires_time
                        )
                    )
                    self._cache_set(
                        cache_url,
                        request,
                        response,
                        body,
                        expires_time,
                        vary_headers,
                    )

    def update_cached_response(
        self, request: PreparedRequest, response: HTTPResponse
    ) -> HTTPResponse:
        """On a 304 we will get a new set of headers that we want to
        update our cached value with, assuming we have one.

        This should only ever be called when we've sent an ETag and
        gotten a 304 as the response.
        """
        assert request.url is not None
        cache_url = self.cache_url(request.url)
        cached_entry = self._load_cache_entry(request)

        if not cached_entry:
            # we didn't have a cached response
            return response
        cached_response = cached_entry.response

        new_response_headers = CaseInsensitiveDict(response.headers)
        if "vary" in new_response_headers:
            new_vary_headers = parse_vary_headers(new_response_headers)
        else:
            new_vary_headers = tuple(sorted(cached_entry.identity))

        old_vary_headers = tuple(sorted(cached_entry.identity))
        variant_group_changed = new_vary_headers is not None and (
            new_vary_headers != old_vary_headers
        )
        cached_body = cached_response.read(decode_content=False)
        cached_response._fp = io.BytesIO(cached_body)  # type: ignore[assignment]
        cached_response.length_remaining = len(cached_body)
        if new_vary_headers is None:
            self.invalidate_cache(cache_url)
            return response

        # Lets update our headers with the headers from the new request:
        # http://tools.ietf.org/html/draft-ietf-httpbis-p4-conditional-26#section-4.1
        #
        # The server isn't supposed to send headers that would make
        # the cached body invalid. But... just in case, we'll be sure
        # to strip out ones we know that might be problematic due to
        # typical assumptions.
        excluded_headers = ["content-length"]

        cached_response.headers.update(
            {
                k: v
                for k, v in response.headers.items()
                if k.lower() not in excluded_headers
            }
        )

        # we want a 200 b/c we have content via the cache
        cached_response.status = 200

        # update our cache
        self._cache_set(
            cache_url,
            request,
            cached_response,
            cached_body,
            vary_headers=new_vary_headers,
        )
        cached_response._fp = io.BytesIO(cached_body)  # type: ignore[assignment]
        cached_response.length_remaining = len(cached_body)

        return cached_response
