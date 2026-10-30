"""Length-delimited local protocol shared by the provider and its CLI client."""

from __future__ import annotations

import json
import re
import socket
import struct

from . import catalog


HEADER = struct.Struct("!I")
MAX_FRAME_BYTES = 64 * 1024
MAX_TEXT_BYTES = 32 * 1024
MAX_TEXT_CHARS = 8_192
MAX_PCM_BYTES = 32 * 1024 * 1024
RATE_CHOICES = (120, 150, 170, 200, 240)
_PHONEME_MARKER = re.compile(r"\[\[|\]\]")


class ProtocolError(ValueError):
    """A peer sent an invalid or unsafe provider message."""


def recv_exact(endpoint: socket.socket, size: int) -> bytes:
    if size < 0:
        raise ProtocolError("negative byte count")
    result = bytearray()
    while len(result) < size:
        block = endpoint.recv(size - len(result))
        if not block:
            raise EOFError("peer closed the connection")
        result.extend(block)
    return bytes(result)


def send_frame(endpoint: socket.socket, payload: dict) -> None:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise ProtocolError(f"message is not JSON serialisable: {error}") from error
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"encoded frame is {len(encoded)} bytes; limit is {MAX_FRAME_BYTES}"
        )
    endpoint.sendall(HEADER.pack(len(encoded)) + encoded)


def recv_frame(endpoint: socket.socket) -> dict:
    size = HEADER.unpack(recv_exact(endpoint, HEADER.size))[0]
    if not size or size > MAX_FRAME_BYTES:
        raise ProtocolError(
            f"frame length is {size}; expected 1-{MAX_FRAME_BYTES} bytes"
        )
    raw = recv_exact(endpoint, size)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"frame is not valid UTF-8 JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProtocolError("frame must contain one JSON object")
    return payload


def validate_request(payload: dict) -> dict:
    op = payload.get("op")
    if op == "status":
        if "model" in payload:
            model = payload["model"]
            if not isinstance(model, str) or model not in catalog.MODEL_IDS:
                raise ProtocolError("status requires a registered model")
            return {"op": "status", "model": model}
        return {"op": "status"}
    if op != "synthesize":
        raise ProtocolError("op must be 'synthesize' or 'status'")
    model = payload.get("model")
    if not isinstance(model, str) or model not in catalog.MODEL_IDS:
        raise ProtocolError(f"model must be one of: {', '.join(catalog.MODEL_IDS)}")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("synthesize requires non-empty text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProtocolError(f"text is not valid Unicode: {error}") from error
    if len(text) > MAX_TEXT_CHARS or len(encoded) > MAX_TEXT_BYTES:
        raise ProtocolError(
            f"text exceeds the {MAX_TEXT_CHARS}-character / "
            f"{MAX_TEXT_BYTES}-byte provider limit"
        )
    if _PHONEME_MARKER.search(text):
        raise ProtocolError(
            "raw Piper [[phoneme]] blocks are disabled; send ordinary text"
        )
    rate = payload.get("rate", 170)
    if (
        not isinstance(rate, int)
        or isinstance(rate, bool)
        or rate not in RATE_CHOICES
    ):
        raise ProtocolError(
            f"rate must be one of: {', '.join(str(item) for item in RATE_CHOICES)}"
        )
    return {"op": op, "model": model, "text": text.strip(), "rate": rate}


__all__ = [
    "MAX_PCM_BYTES",
    "MAX_TEXT_BYTES",
    "MAX_TEXT_CHARS",
    "ProtocolError",
    "RATE_CHOICES",
    "recv_exact",
    "recv_frame",
    "send_frame",
    "validate_request",
]
