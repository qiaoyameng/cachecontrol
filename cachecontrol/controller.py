# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

"""
The httplib2 algorithms ported for use with requests.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
import weakref
from email.utils import parsedate_tz
from typing import TYPE_CHECKING, Collection, Mapping

from requests.structures import CaseInsensitiveDict

from cachecontrol.cache import DictCache, SeparateBodyBaseCache
from cachecontrol.serialize import Serializer

if TYPE_CHECKING:
    from typing import Literal

    from requests import PreparedRequest
    from urllib3 import HTTPResponse

    from cachecontrol.cache import BaseCache

logger = logging.getLogger(__name__)

URI = re.compile(r"^(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?")

PERMANENT_REDIRECT_STATUSES = (301, 308)
STALE_IF_ERROR_STATUS_CODES = (500, 502, 503, 504)

CACHE_HIT_REASON_FRESH = "fresh"
CACHE_HIT_REASON_VALIDATED = "validated"
CACHE_HIT_REASON_STALE_IF_ERROR = "stale-if-error"


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
        stale_if_error: bool | int = False,
        stale_if_error_status_codes: Collection[int] | None = None,
    ):
        self.cache = DictCache() if cache is None else cache
        self.cache_etags = cache_etags
        self.serializer = serializer or Serializer()
        self.cacheable_status_codes = status_codes or (200, 203, 300, 301, 308)
        self.stale_if_error = stale_if_error
        self.stale_if_error_status_codes = (
            stale_if_error_status_codes or STALE_IF_ERROR_STATUS_CODES
        )

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
            "stale-if-error": (int, False),
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
        cache_data = self.cache.get(cache_url)
        if cache_data is None:
            logger.debug("No cache entry available")
            return None

        if isinstance(self.cache, SeparateBodyBaseCache):
            body_file = self.cache.get_body(cache_url)
        else:
            body_file = None

        result = self.serializer.loads(request, cache_data, body_file)
        if result is None:
            logger.debug("Cache entry deserialization failed, entry ignored")
        return result

    def _stale_if_error_limit(
        self,
        request_cc: dict[str, int | None],
        response_cc: dict[str, int | None],
    ) -> int | None:
        if self.stale_if_error is False:
            return None

        response_delta = response_cc.get("stale-if-error")
        if response_delta is None or response_delta < 0:
            return None

        limits: list[int] = [response_delta]

        if isinstance(self.stale_if_error, int) and not isinstance(
            self.stale_if_error, bool
        ):
            if self.stale_if_error < 0:
                return None
            limits.append(self.stale_if_error)

        request_delta = request_cc.get("stale-if-error")
        if request_delta is not None and request_delta >= 0:
            limits.append(request_delta)

        if not limits:
            return None

        return min(limits)

    def _stale_if_error_storage_extension(
        self,
        request_cc: dict[str, int | None],
        response_cc: dict[str, int | None],
    ) -> int | None:
        if self.stale_if_error is False:
            return None

        if "no-store" in request_cc or "no-cache" in request_cc:
            return None

        delta = response_cc.get("stale-if-error")
        if delta is None or delta < 0:
            return None
        if "no-store" in response_cc or "no-cache" in response_cc:
            return None
        if "must-revalidate" in response_cc:
            return None

        limits: list[int] = [delta]
        if isinstance(self.stale_if_error, int) and not isinstance(
            self.stale_if_error, bool
        ):
            if self.stale_if_error < 0:
                return None
            limits.append(self.stale_if_error)

        request_delta = request_cc.get("stale-if-error")
        if request_delta is not None and request_delta >= 0:
            limits.append(request_delta)

        if not limits:
            return None

        return min(limits)

    def cached_request(
        self, request: PreparedRequest, *, stale_on_error: bool = False
    ) -> HTTPResponse | Literal[False]:
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

        if "no-store" in cc:
            logger.debug('Request header has "no-store", cache bypassed')
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
                self.cache.delete(cache_url)
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

        stale_if_error_limit = self._stale_if_error_limit(cc, resp_cc)
        can_use_stale_on_error = (
            stale_if_error_limit is not None
            and "no-store" not in resp_cc
            and "no-cache" not in resp_cc
            and "must-revalidate" not in resp_cc
        )
        if stale_on_error and can_use_stale_on_error:
            if current_age <= freshness_lifetime + stale_if_error_limit:
                logger.debug('The response is stale, but usable with "stale-if-error"')
                return resp
            logger.debug('The cached response is stale beyond "stale-if-error" window')

        # we're not fresh. If we don't have an Etag, clear it out
        if "etag" not in headers and not can_use_stale_on_error:
            logger.debug('The cached response is "stale" with no etag, purging')
            self.cache.delete(cache_url)

        # return the original handler
        return False

    def stale_if_error_request(
        self, request: PreparedRequest
    ) -> HTTPResponse | Literal[False]:
        return self.cached_request(request, stale_on_error=True)

    def conditional_headers(self, request: PreparedRequest) -> dict[str, str]:
        if "no-store" in self.parse_cache_control(request.headers):
            return {}

        try:
            resp = self._load_from_cache(request)
        except Exception:
            logger.debug(
                "Ignoring conditional headers because cache could not be loaded"
            )
            return {}
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
            self.cache.delete(cache_url)
        if no_store:
            return

        # https://tools.ietf.org/html/rfc7234#section-4.1:
        # A Vary header field-value of "*" always fails to match.
        # Storing such a response leads to a deserialization warning
        # during cache lookup and is not allowed to ever be served,
        # so storing it can be avoided.
        if "*" in response_headers.get("vary", ""):
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
            self._cache_set(cache_url, request, response, body, expires_time)

        # Add to the cache any permanent redirects. We do this before looking
        # that the Date headers.
        elif int(response.status) in PERMANENT_REDIRECT_STATUSES:
            logger.debug("Caching permanent redirect")
            self._cache_set(cache_url, request, response, b"")

        # Add to the cache if the response headers demand it. If there
        # is no date header then we can't do anything about expiring
        # the cache.
        elif "date" in response_headers:
            time_tuple = parsedate_tz(response_headers["date"])
            if time_tuple is None:
                return
            date = calendar.timegm(time_tuple[:6])
            stale_if_error_extension = self._stale_if_error_storage_extension(
                cc_req, cc
            )
            # cache when there is a max-age > 0
            max_age = cc.get("max-age")
            if max_age is not None and max_age > 0:
                logger.debug("Caching b/c date exists and max-age > 0")
                expires_time = (
                    max_age + stale_if_error_extension
                    if stale_if_error_extension is not None
                    else max_age
                )
                self._cache_set(
                    cache_url,
                    request,
                    response,
                    body,
                    expires_time,
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
                    if stale_if_error_extension is not None and expires_time > 0:
                        expires_time += stale_if_error_extension

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
        cached_response = self._load_from_cache(request)

        if not cached_response:
            # we didn't have a cached response
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
        self._cache_set(cache_url, request, cached_response)

        return cached_response
