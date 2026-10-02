# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
from typing import IO, TYPE_CHECKING, Any, Collection, Mapping, cast

import msgpack
from requests.structures import CaseInsensitiveDict
from urllib3 import HTTPResponse

if TYPE_CHECKING:
    from requests import PreparedRequest


def parse_vary_headers(headers: Mapping[str, str]) -> tuple[str, ...] | None:
    """Return the normalized set of headers named by ``Vary``.

    ``None`` means ``Vary: *`` and therefore identifies no cacheable variant.
    """
    value = headers.get("vary", headers.get("Vary", ""))
    fields = {
        field.strip().lower()
        for field in value.split(",")
        if field.strip()
    }
    if "*" in fields:
        return None
    return tuple(sorted(fields))


def vary_identity(
    request: PreparedRequest, vary_headers: Collection[str]
) -> dict[str, str | None]:
    """Build the selecting request-header identity for a cached variant."""
    return {
        header.lower(): _request_header_value(request, header)
        for header in sorted(vary_headers, key=str.lower)
    }


def _request_header_value(request: PreparedRequest, header: str) -> str | None:
    value = request.headers.get(header, None)
    return None if value is None else str(value)


class Serializer:
    serde_version = "4"
    index_version = 1
    index_prefix = b"ccv=1,"

    def dumps(
        self,
        request: PreparedRequest,
        response: HTTPResponse,
        body: bytes | None = None,
    ) -> bytes:
        response_headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(
            response.headers
        )

        if body is None:
            # When a body isn't passed in, we'll read the response. We
            # also update the response with a new file handler to be
            # sure it acts as though it was never read.
            body = response.read(decode_content=False)
            response._fp = io.BytesIO(body)  # type: ignore[assignment]
            response.length_remaining = len(body)

        data = {
            "response": {
                "body": body,  # Empty bytestring if body is stored separately
                "headers": {str(k): str(v) for k, v in response.headers.items()},
                "status": response.status,
                "version": response.version,
                "reason": str(response.reason),
                "decode_content": response.decode_content,
            }
        }

        vary_headers = parse_vary_headers(response_headers)
        data["vary"] = (
            {} if vary_headers is None else vary_identity(request, vary_headers)
        )

        return b",".join([f"cc={self.serde_version}".encode(), self.serialize(data)])

    def serialize(self, data: dict[str, Any]) -> bytes:
        return cast(bytes, msgpack.dumps(data, use_bin_type=True))

    def loads(
        self,
        request: PreparedRequest,
        data: bytes,
        body_file: IO[bytes] | None = None,
    ) -> HTTPResponse | None:
        # Short circuit if we've been given an empty set of data
        if not data:
            return None

        # Previous versions of this library supported other serialization
        # formats, but these have all been removed.
        if not data.startswith(f"cc={self.serde_version},".encode()):
            return None

        data = data[len(f"cc={self.serde_version},"):]
        return self._loads_v4(request, data, body_file)

    def dumps_index(self, index: Mapping[str, Any]) -> bytes:
        return self.index_prefix + msgpack.dumps(
            {"version": self.index_version, **dict(index)}, use_bin_type=True
        )

    def loads_index(self, data: bytes) -> dict[str, Any] | None:
        if not data or not data.startswith(self.index_prefix):
            return None
        try:
            index = msgpack.loads(data[len(self.index_prefix) :], raw=False)
        except ValueError:
            return None
        if (
            not isinstance(index, dict)
            or index.get("version") != self.index_version
        ):
            return None
        vary = index.get("vary")
        variants = index.get("variants")
        if not isinstance(vary, list) or not all(
            isinstance(x, str) for x in vary
        ):
            return None
        if not isinstance(variants, list):
            return None
        for variant in variants:
            if not isinstance(variant, dict):
                return None
            identity = variant.get("identity")
            if not isinstance(identity, dict):
                return None
            if not all(
                isinstance(key, str)
                and (value is None or isinstance(value, str))
                for key, value in identity.items()
            ):
                return None
            if not isinstance(variant.get("key"), str):
                return None
        return index

    def loads_vary(self, data: bytes) -> dict[str, str | None] | None:
        """Return the selecting request headers stored in a cached response."""
        prefix = f"cc={self.serde_version},".encode()
        if not data or not data.startswith(prefix):
            return None

        try:
            cached = msgpack.loads(data[len(prefix) :], raw=False)
        except ValueError:
            return None

        vary = cached.get("vary")
        if not isinstance(vary, dict):
            return {}
        return {
            str(key).lower(): value
            for key, value in vary.items()
            if key and (value is None or isinstance(value, str))
        }

    def prepare_response(
        self,
        request: PreparedRequest,
        cached: Mapping[str, Any],
        body_file: IO[bytes] | None = None,
    ) -> HTTPResponse | None:
        """Verify our vary headers match and construct a real urllib3
        HTTPResponse object.
        """
        # Special case the '*' Vary value as it means we cannot actually
        # determine if the cached response is suitable for this request.
        # This case is also handled in the controller code when creating
        # a cache entry, but is left here for backwards compatibility.
        if "*" in cached.get("vary", {}):
            return None

        # Ensure that the Vary headers for the cached response match our
        # request
        for header, value in cached.get("vary", {}).items():
            if request.headers.get(header.lower(), None) != value:
                return None

        body_raw = cached["response"].pop("body")

        headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(
            data=cached["response"]["headers"]
        )
        if headers.get("transfer-encoding", "") == "chunked":
            headers.pop("transfer-encoding")

        cached["response"]["headers"] = headers

        try:
            body: IO[bytes]
            if body_file is None:
                body = io.BytesIO(body_raw)
            else:
                body = body_file
        except TypeError:
            # This can happen if cachecontrol serialized to v1 format (pickle)
            # using Python 2. A Python 2 str(byte string) will be unpickled as
            # a Python 3 str (unicode string), which will cause the above to
            # fail with:
            #
            #     TypeError: 'str' does not support the buffer interface
            body = io.BytesIO(body_raw.encode("utf8"))

        # Discard any `strict` parameter serialized by older version of cachecontrol.
        cached["response"].pop("strict", None)

        return HTTPResponse(body=body, preload_content=False, **cached["response"])

    def _loads_v4(
        self,
        request: PreparedRequest,
        data: bytes,
        body_file: IO[bytes] | None = None,
    ) -> HTTPResponse | None:
        try:
            cached = msgpack.loads(data, raw=False)
        except ValueError:
            return None

        return self.prepare_response(request, cached, body_file)
