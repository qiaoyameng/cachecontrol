# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urllib3 import HTTPResponse


class CacheControlError(Exception):
    """Base class for CacheControl errors."""


class OnlyIfCachedError(CacheControlError):
    """Base class for failures serving a cache-only request."""


class NotInCacheError(OnlyIfCachedError):
    """No stored response was found for a cache-only request."""



class CachedResponseUnavailableError(OnlyIfCachedError):
    """A stored response exists, but it cannot satisfy the request."""

    def __init__(self, response: HTTPResponse | None = None, reason: str = "") -> None:
        self.response = response
        self.reason = reason
        super().__init__(reason)


NotCachedError = NotInCacheError
CacheMissError = NotInCacheError
OnlyIfCachedMissError = NotInCacheError
OnlyIfCachedUnavailableError = CachedResponseUnavailableError
CachedResponseNotUsableError = CachedResponseUnavailableError
