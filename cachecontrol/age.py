# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0
"""
Response age accounting, following RFC 9111 section 4.2.3.

A cached response's current age is more than "now minus the Date
header": the response may have sat in upstream caches (the Age header),
it aged while the request was in flight, and it ages while it resides
in our local cache. ``AgeInfo`` records all of these components so the
controller can compute how much of the freshness lifetime is actually
left and emit an accurate Age header when serving cache hits.
"""

from __future__ import annotations

import calendar
import logging
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_tz
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Identifies this process. The monotonic clock is only comparable
# within a single process, so serialized age metadata carries this
# token: a restore in the same process keeps the monotonic time basis,
# a restore anywhere else falls back to the wall clock.
_PROCESS_TOKEN = uuid.uuid4().hex


def parse_http_date(value: str | None) -> int | None:
    """Parse an HTTP-date into epoch seconds, or None if it is invalid."""
    if not value:
        return None
    time_tuple = parsedate_tz(value)
    if time_tuple is None:
        return None
    return calendar.timegm(time_tuple[:6])


def parse_age_header(value: str | None) -> int:
    """Parse an Age header into a non-negative number of seconds.

    An invalid or negative Age is treated as 0. It must never make a
    response look younger than it is; freshness stays bounded by the
    response's freshness lifetime regardless.
    """
    if not value:
        return 0
    try:
        age = int(value)
    except ValueError:
        logger.debug("Ignoring invalid Age header: %r", value)
        return 0
    return max(0, age)


@dataclass
class AgeInfo:
    """Bookkeeping for computing a cached response's current age.

    Time basis: ``request_time`` and ``response_time`` are wall-clock
    timestamps (``time.time()``) so they survive serialization.
    ``monotonic_response_time`` is a ``time.monotonic()`` timestamp and
    is only meaningful within this process; it is serialized together
    with a process token so a restore in the same process can keep
    using it. Resident time is therefore measured on the monotonic
    clock while the entry lives in this process, and falls back to the
    wall clock for entries restored by another process. Both are
    clamped at zero so a wall-clock rollback can never make a response
    younger.
    """

    date_value: int | None
    age_value: int
    request_time: float
    response_time: float
    monotonic_response_time: float | None

    @classmethod
    def from_headers(
        cls,
        headers: Mapping[str, str],
        request_time: float | None = None,
        response_time: float | None = None,
        monotonic_response_time: float | None = None,
    ) -> AgeInfo:
        """Capture the age metadata of a freshly received response."""
        if response_time is None:
            response_time = time.time()
        if monotonic_response_time is None:
            monotonic_response_time = time.monotonic()
        if request_time is None:
            request_time = response_time
        return cls(
            date_value=parse_http_date(headers.get("date", headers.get("Date"))),
            age_value=parse_age_header(headers.get("age", headers.get("Age"))),
            request_time=request_time,
            response_time=response_time,
            monotonic_response_time=monotonic_response_time,
        )

    @property
    def initial_age(self) -> float:
        """The corrected initial age (RFC 9111 section 4.2.3)."""
        apparent_age = 0.0
        if self.date_value is not None:
            apparent_age = max(0.0, self.response_time - self.date_value)
        response_delay = max(0.0, self.response_time - self.request_time)
        corrected_age_value = self.age_value + response_delay
        return max(apparent_age, corrected_age_value)

    def current_age(
        self, now: float | None = None, monotonic_now: float | None = None
    ) -> float:
        """The response's current age: initial age plus resident time."""
        if self.monotonic_response_time is not None:
            if monotonic_now is None:
                monotonic_now = time.monotonic()
            resident_time = max(0.0, monotonic_now - self.monotonic_response_time)
        else:
            # Restored from a serialized entry: the monotonic clock has
            # no cross-process meaning, so the resident time is rebased
            # on the wall clock. A wall-clock rollback clamps to zero
            # rather than making the entry younger.
            if now is None:
                now = time.time()
            resident_time = max(0.0, now - self.response_time)
        return self.initial_age + resident_time

    def to_dict(self) -> dict[str, Any]:
        """Serialize the age metadata.

        The monotonic timestamp is process-local, so it is stored
        together with a token identifying this process; a restore by
        any other process ignores it and rebases the resident time on
        the wall clock (see ``from_dict``).
        """
        return {
            "date_value": self.date_value,
            "age_value": self.age_value,
            "request_time": self.request_time,
            "response_time": self.response_time,
            "monotonic_response_time": self.monotonic_response_time,
            "process_token": _PROCESS_TOKEN,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AgeInfo:
        """Restore age metadata serialized by ``to_dict``.

        Corrupt metadata must not make the entry infinitely fresh, so
        unparseable values restore as a zero ``response_time``: the
        entry then appears as old as possible instead of ageless.
        """
        try:
            date_value = data.get("date_value")
            age_value = int(data.get("age_value") or 0)
            request_time = float(data.get("request_time") or 0.0)
            response_time = float(data.get("response_time") or 0.0)
        except (TypeError, ValueError):
            logger.debug("Ignoring corrupt serialized age metadata")
            return cls(
                date_value=None,
                age_value=0,
                request_time=0.0,
                response_time=0.0,
                monotonic_response_time=None,
            )
        monotonic_response_time = data.get("monotonic_response_time")
        if data.get("process_token") == _PROCESS_TOKEN and isinstance(
            monotonic_response_time, (int, float)
        ):
            # Same process: the monotonic time basis is still valid.
            monotonic = float(monotonic_response_time)
        else:
            # Another process (or an older format): rebase on the wall
            # clock.
            monotonic = None
        return cls(
            date_value=date_value if isinstance(date_value, (int, float)) else None,
            age_value=max(0, age_value),
            request_time=request_time,
            response_time=response_time,
            monotonic_response_time=monotonic,
        )
