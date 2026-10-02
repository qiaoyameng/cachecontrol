# SPDX-FileCopyrightText: 2015 Eric Larson
#
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from typing import IO, TYPE_CHECKING, Any, Mapping, cast

import msgpack
from requests.structures import CaseInsensitiveDict
from urllib3 import HTTPResponse

if TYPE_CHECKING:
    from requests import PreparedRequest


class Serializer:
    serde_version = "4"
    index_version = "1"
    index_prefix = f"cc=variants={index_version},".encode()

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

        field_names = normalize_vary_fields(response_headers.get("vary"))

        # Construct our vary headers
        data["vary"] = {}
        request_headers = (
            CaseInsensitiveDict(data=request.headers)
            if isinstance(request.headers, Mapping)
            else request.headers
        )
        if field_names != ("*",):
            for header in field_names:
                header_value = request_headers.get(header, None)
                if header_value is not None:
                    header_value = str(header_value)
                data["vary"][header] = header_value

        return b",".join([f"cc={self.serde_version}".encode(), self.serialize(data)])

    def dumps_index(
        self,
        vary_fields: tuple[str, ...],
        variants: list[dict[str, Any]],
    ) -> bytes:
        data = {
            "version": int(self.index_version),
            "vary": list(vary_fields),
            "variants": variants,
        }
        return self.index_prefix + self.serialize(data)

    def variant_id(
        self,
        vary_fields: tuple[str, ...],
        headers: Mapping[str, Any],
    ) -> str:
        normalized_headers = CaseInsensitiveDict(data=dict(headers))
        values = [
            (field, normalize_header_value(normalized_headers.get(field, None)))
            for field in vary_fields
        ]
        serialized = msgpack.packb(values, use_bin_type=True)
        return hashlib.sha256(serialized).hexdigest()

    def serialize(self, data: dict[str, Any]) -> bytes:
        return cast(bytes, msgpack.dumps(data, use_bin_type=True))

    def loads_index(self, data: bytes) -> dict[str, Any] | None:
        if not data.startswith(self.index_prefix):
            return None

        try:
            cached = msgpack.loads(data[len(self.index_prefix) :], raw=False)
            fields = cached["vary"]
            variants = cached["variants"]
            if not isinstance(fields, list) or not isinstance(variants, list):
                return None

            normalized_fields = normalize_vary_fields(",".join(fields))
            if normalized_fields == ("*",):
                return None
            normalized_variants = []
            for variant in variants:
                variant_id = variant["id"]
                vary = variant["vary"]
                if not isinstance(variant_id, str) or not isinstance(vary, dict):
                    return None
                normalized_vary = {
                    str(field).lower(): normalize_header_value(value)
                    for field, value in vary.items()
                }
                if tuple(normalized_vary) != normalized_fields:
                    return None
                expected_id = self.variant_id(normalized_fields, normalized_vary)
                if variant_id != expected_id:
                    return None
                ordered_vary = {
                    field: normalized_vary[field] for field in normalized_fields
                }
                normalized_variants.append(
                    {"id": variant_id, "vary": ordered_vary}
                )

            return {"vary": normalized_fields, "variants": normalized_variants}
        except (KeyError, TypeError, ValueError):
            return None

    def loads_record(self, data: bytes) -> Mapping[str, Any] | None:
        prefix = f"cc={self.serde_version},".encode()
        if not data.startswith(prefix):
            return None

        try:
            return msgpack.loads(data[len(prefix) :], raw=False)
        except ValueError:
            return None

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

        data = data[5:]
        return self._loads_v4(request, data, body_file)

    def is_index(self, data: bytes | None) -> bool:
        return isinstance(data, bytes) and data.startswith(self.index_prefix)

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
        response_headers = CaseInsensitiveDict(
            data=cached.get("response", {}).get("headers", {})
        )
        if "*" in cached.get("vary", {}) or normalize_vary_fields(
            response_headers.get("vary")
        ) == ("*",):
            return None

        # Ensure that the Vary headers for the cached response match our
        # request
        request_headers = (
            CaseInsensitiveDict(data=request.headers)
            if isinstance(request.headers, Mapping)
            else request.headers
        )
        for header, value in cached.get("vary", {}).items():
            if request_headers.get(header, None) != value:
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

        return self.prepare_response(request, deepcopy(cached), body_file)


def normalize_header_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def normalize_vary_fields(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()

    fields = {
        field.strip().lower()
        for field in str(value).split(",")
        if field.strip()
    }
    if "*" in fields:
        return ("*",)
    return tuple(sorted(fields))
