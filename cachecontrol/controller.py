# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
The httplib2 algorithms ported for use with requests.
"""

from __future__ import annotations

import calendar
import io
import logging
import re
import time
import weakref
from email.utils import parsedate_tz
from threading import RLock
from typing import IO, TYPE_CHECKING, Any, Collection, Mapping

from requests.structures import CaseInsensitiveDict

from cachecontrol.cache import DictCache, SeparateBodyBaseCache
from cachecontrol.serialize import Serializer, normalize_vary_fields

if TYPE_CHECKING:
    from typing import Literal

    from requests import PreparedRequest
    from urllib3 import HTTPResponse

    from cachecontrol.cache import BaseCache

logger = logging.getLogger(__name__)

DEFAULT_MAX_VARIANTS = 16

URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")

PERMANENT_REDIRECT_STATUSES = (301, 308)


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
        max_variants: int = DEFAULT_MAX_VARIANTS,
    ):
        if max_variants < 1:
            raise ValueError("max_variants must be at least 1")
        self.cache = DictCache() if cache is None else cache
        self.cache_etags = cache_etags
        self.serializer = serializer or Serializer()
        self.cacheable_status_codes = status_codes or (200, 203, 300, 301, 308)
        self.max_variants = max_variants
        self.variant_lock = RLock()

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

    def _supports_variants(self) -> bool:
        return all(
            hasattr(self.serializer, method)
            for method in ("is_index", "loads_index", "loads_record", "variant_id")
        )

    def _is_index(self, data: bytes | None) -> bool:
        return isinstance(data, bytes) and data.startswith(Serializer.index_prefix)

    def _loads_index(self, data: bytes) -> dict[str, Any] | None:
        if not self._supports_variants() or not isinstance(data, bytes):
            return None
        return self.serializer.loads_index(data)

    def _loads_record(self, data: Any) -> Mapping[str, Any] | None:
        if not self._supports_variants():
            return {"vary": {}}
        if not isinstance(data, bytes):
            return {"vary": {}}
        return self.serializer.loads_record(data)

    def _variant_id(
        self, fields: tuple[str, ...], headers: Mapping[str, Any]
    ) -> str:
        return self.serializer.variant_id(fields, headers)

    def _variant_key(self, cache_url: str, variant_id: str) -> str:
        return f"{cache_url}#variants/{variant_id}"

    def _load_index(self, cache_url: str) -> dict[str, Any] | None:
        raw_index = self.cache.get(cache_url)
        if raw_index is None:
            return None

        index = self._loads_index(raw_index)
        if index is None:
            return None

        variants = []
        for variant in index["variants"]:
            variant_key = self._variant_key(cache_url, variant["id"])
            if self.cache.get(variant_key) is None:
                continue
            if isinstance(self.cache, SeparateBodyBaseCache):
                body_file = self.cache.get_body(variant_key)
                if body_file is None:
                    continue
                body_file.close()
            variants.append(variant)
        if len(variants) != len(index["variants"]):
            index["variants"] = variants
            self._save_index(cache_url, index)
        return index

    def _save_index(self, cache_url: str, index: dict[str, Any]) -> None:
        self.cache.set(
            cache_url,
            self.serializer.dumps_index(index["vary"], index["variants"]),
        )

    def _invalidate_url(self, cache_url: str) -> None:
        with self.variant_lock:
            index = self._load_index(cache_url)
            if index is not None:
                for variant in index["variants"]:
                    self._delete_variant(cache_url, variant["id"])
            self.cache.delete(cache_url)

    def _delete_variant(self, cache_url: str, variant_id: str) -> None:
        variant_key = self._variant_key(cache_url, variant_id)
        self.cache.delete(variant_key)

    def _remove_variant(
        self, cache_url: str, index: dict[str, Any], variant_id: str
    ) -> None:
        self._delete_variant(cache_url, variant_id)
        index["variants"] = [
            variant
            for variant in index["variants"]
            if variant["id"] != variant_id
        ]
        if index["variants"]:
            self._save_index(cache_url, index)
        else:
            self.cache.delete(cache_url)

    def _invalidate_selected(
        self, request: PreparedRequest, cache_url: str
    ) -> None:
        with self.variant_lock:
            entry = self._load_cache_entry(request, cache_url)
            if entry is None:
                return
            if entry["legacy"]:
                self.cache.delete(cache_url)
            else:
                self._remove_variant(cache_url, entry["index"], entry["id"])

    def _load_entry(
        self,
        request: PreparedRequest,
        cache_url: str,
        raw_entry: bytes | None,
        body_file: IO[bytes] | None = None,
    ) -> tuple[HTTPResponse | None, Mapping[str, Any] | None]:
        if raw_entry is None:
            return None, None
        record = self._loads_record(raw_entry)
        if isinstance(raw_entry, bytes) and raw_entry.startswith(
            Serializer.index_prefix
        ):
            return None, None
        return self.serializer.loads(request, raw_entry, body_file), record

    def _load_cache_entry(
        self, request: PreparedRequest, cache_url: str
    ) -> dict[str, Any] | None:
        with self.variant_lock:
            raw = self.cache.get(cache_url)
            if raw is None:
                return None

            if self._is_index(raw):
                index = self._loads_index(raw)
                if index is None:
                    logger.debug("Invalid Vary index, purging")
                    self._invalidate_url(cache_url)
                    return None

                current_id = self._variant_id(index["vary"], request.headers)
                variant = next(
                    (
                        variant
                        for variant in index["variants"]
                        if variant["id"] == current_id
                    ),
                    None,
                )
                if variant is None:
                    return None

                variant_key = self._variant_key(cache_url, current_id)
                raw_entry = self.cache.get(variant_key)
                if raw_entry is None:
                    self._remove_variant(cache_url, index, current_id)
                    return None

                body_file = None
                if isinstance(self.cache, SeparateBodyBaseCache):
                    body_file = self.cache.get_body(variant_key)
                    if body_file is None:
                        self._remove_variant(cache_url, index, current_id)
                        return None

                response, record = self._load_entry(
                    request, variant_key, raw_entry, body_file
                )
                if response is None or record is None:
                    self._remove_variant(cache_url, index, current_id)
                    return None

                return {
                    "response": response,
                    "key": variant_key,
                    "id": current_id,
                    "index": index,
                    "legacy": False,
                    "record": record,
                }

            body_file = None
            if isinstance(self.cache, SeparateBodyBaseCache):
                body_file = self.cache.get_body(cache_url)
            response, record = self._load_entry(request, cache_url, raw, body_file)
            if response is None:
                return None
            return {
                "response": response,
                "key": cache_url,
                "id": None,
                "index": None,
                "legacy": True,
                "record": record,
            }

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

        cache_url = request.url
        assert cache_url is not None
        entry = self._load_cache_entry(request, cache_url)
        if entry is None:
            logger.debug("No cache entry available")
            return None
        return entry["response"]

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
        resp = self._load_from_cache(request)
        if not resp:
            return False

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
                self._invalidate_selected(request, cache_url)
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
            self._invalidate_selected(request, cache_url)

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
    ) -> None:
        """Store the data in the cache.
        """
        if expires_time is not None and expires_time <= 0:
            # Already stale on arrival
            logger.debug("Purging cached response: expires in the past")
            self.cache.delete(cache_url)
            return

        if isinstance(self.cache, SeparateBodyBaseCache):
            # We pass in the body separately; just put a placeholder empty
            # string in the metadata.
            self.cache.set(
                cache_url,
                self.serializer.dumps(request, response, b""),
                expires=expires_time,
            )
            # body is None can happen when, for example, we're only updating
            # headers, as is the case in update_cached_response().
            if body is not None:
                self.cache.set_body(cache_url, body)
        else:
            self.cache.set(
                cache_url,
                self.serializer.dumps(request, response, body),
                expires=expires_time,
            )

    def _migrate_legacy_variant(
        self,
        cache_url: str,
        raw_entry: bytes,
        record: Mapping[str, Any],
        fields: tuple[str, ...],
    ) -> dict[str, Any] | None:
        lookup = CaseInsensitiveDict(data=dict(record.get("vary", {})))
        identity = {field: lookup.get(field) for field in fields}
        legacy_id = self._variant_id(fields, identity)

        legacy_key = self._variant_key(cache_url, legacy_id)
        legacy_body = None
        if isinstance(self.cache, SeparateBodyBaseCache):
            old_body = self.cache.get_body(cache_url)
            if old_body is None:
                return None
            with old_body:
                legacy_body = old_body.read()

        self.cache.set(legacy_key, raw_entry)
        if legacy_body is not None:
            self.cache.set_body(legacy_key, legacy_body)

        self.cache.delete(cache_url)
        index = {
            "vary": fields,
            "variants": [{"id": legacy_id, "vary": identity}],
        }
        self._save_index(cache_url, index)
        return index

    def _prepare_variant_index(
        self,
        cache_url: str,
        fields: tuple[str, ...],
        request: PreparedRequest,
    ) -> dict[str, Any]:
        raw = self.cache.get(cache_url)
        current_id = self._variant_id(fields, request.headers)

        if raw is None:
            return {"vary": fields, "variants": [], "current_id": current_id}

        if self._is_index(raw):
            index = self._loads_index(raw)
            if index is None or tuple(index["vary"]) != fields:
                self._invalidate_url(cache_url)
                return {"vary": fields, "variants": [], "current_id": current_id}
            index["current_id"] = current_id
            return index

        record = self._loads_record(raw)
        if record is None:
            self.cache.delete(cache_url)
            return {"vary": fields, "variants": [], "current_id": current_id}

        old_vary = record.get("vary", {})
        old_response = record.get("response", {})
        old_headers = (
            old_response.get("headers", {})
            if isinstance(old_response, Mapping)
            else {}
        )
        old_response_headers = CaseInsensitiveDict(data=old_headers)
        if (
            not isinstance(old_response, Mapping)
            or not isinstance(old_vary, Mapping)
            or "*" in old_vary
            or normalize_vary_fields(old_response_headers.get("vary")) == ("*",)
        ):
            self.cache.delete(cache_url)
            return {"vary": fields, "variants": [], "current_id": current_id}

        old_fields = tuple(sorted(str(field).lower() for field in old_vary))
        if old_fields != fields:
            self.cache.delete(cache_url)
            return {"vary": fields, "variants": [], "current_id": current_id}

        migrated = self._migrate_legacy_variant(cache_url, raw, record, fields)
        if migrated is None:
            self.cache.delete(cache_url)
            return {"vary": fields, "variants": [], "current_id": current_id}

        migrated["current_id"] = current_id
        return migrated

    def _store_response(
        self,
        cache_url: str,
        request: PreparedRequest,
        response: HTTPResponse,
        body: bytes | None,
        expires_time: int | None,
        fields: tuple[str, ...],
    ) -> None:
        with self.variant_lock:
            if not fields or not self._supports_variants():
                raw = self.cache.get(cache_url)
                if self._is_index(raw):
                    self._invalidate_url(cache_url)
                if expires_time is not None and expires_time <= 0:
                    self.cache.delete(cache_url)
                else:
                    self._cache_set(
                        cache_url, request, response, body, expires_time
                    )
                return

            request_headers = (
                CaseInsensitiveDict(data=request.headers)
                if isinstance(request.headers, Mapping)
                else request.headers
            )
            current_id = self._variant_id(fields, request_headers)
            if expires_time is not None and expires_time <= 0:
                raw = self.cache.get(cache_url)
                index = self._loads_index(raw) if raw is not None else None
                if index is not None and tuple(index["vary"]) == fields:
                    self._remove_variant(cache_url, index, current_id)
                return

            index = self._prepare_variant_index(cache_url, fields, request)
            variants = index["variants"]
            existing = next(
                (
                    position
                    for position, variant in enumerate(variants)
                    if variant["id"] == current_id
                ),
                None,
            )

            if existing is None:
                while len(variants) >= self.max_variants:
                    evicted = variants.pop(0)
                    self._delete_variant(cache_url, evicted["id"])
                variants.append(
                    {
                        "id": current_id,
                        "vary": {
                            field: request_headers.get(field)
                            for field in fields
                        },
                    }
                )

            self._save_index(cache_url, {"vary": fields, "variants": variants})
            variant_key = self._variant_key(cache_url, current_id)
            self._cache_set(variant_key, request, response, body, expires_time)

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
        if no_store and self.cache.get(cache_url):
            logger.debug('Purging existing cache entry to honor "no-store"')
            self._invalidate_url(cache_url)
        if no_store:
            return

        vary_fields = normalize_vary_fields(response_headers.get("vary"))

        # https://tools.ietf.org/html/rfc7234#section-4.1:
        # A Vary header field-value of "*" always fails to match.
        # Storing such a response leads to a deserialization warning
        # during cache lookup and is not allowed to ever be served,
        # so storing it can be avoided.
        if vary_fields == ("*",):
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
            self._store_response(
                cache_url, request, response, body, expires_time, vary_fields
            )

        # Add to the cache any permanent redirects. We do this before looking
        # that the Date headers.
        elif int(response.status) in PERMANENT_REDIRECT_STATUSES:
            logger.debug("Caching permanent redirect")
            self._store_response(cache_url, request, response, b"", None, vary_fields)

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
                self._store_response(
                    cache_url,
                    request,
                    response,
                    body,
                    expires_time,
                    vary_fields,
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
                    self._store_response(
                        cache_url,
                        request,
                        response,
                        body,
                        expires_time,
                        vary_fields,
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
        entry = self._load_cache_entry(request, cache_url)

        if entry is None:
            # we didn't have a cached response
            return response
        cached_response = entry["response"]
        record = entry["record"]
        if record is None:
            return cached_response
        old_vary = record.get("vary", {})
        old_fields = tuple(sorted(str(field).lower() for field in old_vary))

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

        if "vary" in response.headers:
            vary_fields = normalize_vary_fields(response.headers["vary"])
        else:
            vary_fields = old_fields

        if vary_fields == ("*",):
            logger.debug('Response header has "Vary: *"')
            return cached_response

        if "no-store" in self.parse_cache_control(response.headers):
            logger.debug('Purging validated response to honor "no-store"')
            if entry["legacy"]:
                self.cache.delete(cache_url)
            else:
                self._remove_variant(cache_url, entry["index"], entry["id"])
            return cached_response

        body_for_update = None
        if self._supports_variants() and (
            entry["legacy"] or old_fields != vary_fields
        ):
            body_for_update = cached_response.read(decode_content=False)
            cached_response._fp = io.BytesIO(body_for_update)
            cached_response.length_remaining = len(body_for_update)

        self._store_response(
            cache_url,
            request,
            cached_response,
            body_for_update,
            None,
            vary_fields,
        )

        return cached_response
