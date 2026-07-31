"""Binary framing shared by the Python Coworker and Rust Desktop."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

VERSION = 1
MAX_INNER_FRAME = 1024 * 1024
_HEADER = struct.Struct("!BBII")


class FrameType(IntEnum):
    CLIENT_PROOF_CHALLENGE = 1
    CLIENT_PROOF = 2
    CLIENT_READY = 3
    REQUEST_START = 10
    REQUEST_BODY = 11
    REQUEST_END = 12
    REQUEST_CANCEL = 13
    RESPONSE_START = 20
    RESPONSE_BODY = 21
    RESPONSE_END = 22
    RESPONSE_ERROR = 23
    PING = 30
    PONG = 31


@dataclass(slots=True)
class Frame:
    kind: FrameType
    stream_id: int
    payload: bytes = b""

    def encode(self) -> bytes:
        if len(self.payload) > MAX_INNER_FRAME:
            raise ValueError("inner Relay frame is too large")
        return _HEADER.pack(VERSION, int(self.kind), self.stream_id, len(self.payload)) + self.payload


class FrameDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buffer.extend(chunk)
        result: list[Frame] = []
        while len(self._buffer) >= _HEADER.size:
            version, raw_kind, stream_id, length = _HEADER.unpack_from(self._buffer)
            if version != VERSION or length > MAX_INNER_FRAME:
                raise ValueError("invalid inner Relay frame header")
            total = _HEADER.size + length
            if len(self._buffer) < total:
                break
            try:
                kind = FrameType(raw_kind)
            except ValueError as error:
                raise ValueError("unknown inner Relay frame type") from error
            payload = bytes(self._buffer[_HEADER.size : total])
            del self._buffer[:total]
            result.append(Frame(kind=kind, stream_id=stream_id, payload=payload))
        return result


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_json(value: bytes) -> Any:
    return json.loads(value.decode("utf-8"))
