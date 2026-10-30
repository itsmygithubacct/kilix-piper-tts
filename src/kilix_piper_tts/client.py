"""CLI-side provider client and on-demand service startup."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from . import catalog, paths, protocol


CONNECT_TIMEOUT_S = 8.0
SYNTHESIS_TIMEOUT_S = 180.0


class ClientError(RuntimeError):
    """The local provider is unavailable or returned an invalid response."""


def _connect() -> socket.socket:
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.settimeout(2.0)
    try:
        endpoint.connect(paths.socket_path())
    except BaseException:
        endpoint.close()
        raise
    return endpoint


def _start_service() -> None:
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kilix_piper_tts",
                "serve",
                "--idle-seconds",
                "300",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=environment,
        )
    except OSError as error:
        raise ClientError(f"cannot start the Piper provider: {error}") from error


def connect(*, autostart: bool) -> socket.socket:
    try:
        return _connect()
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError) as first:
        if not autostart:
            raise ClientError(f"Piper provider is not running: {first}") from first
    _start_service()
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            return _connect()
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError) as error:
            last_error = error
            time.sleep(0.05)
    raise ClientError(
        f"Piper provider did not start within {CONNECT_TIMEOUT_S:.0f} seconds: "
        f"{last_error}. Run `kilix-piper-tts serve` in a terminal for details."
    )


def status(*, require_running: bool = False) -> dict:
    try:
        endpoint = connect(autostart=not require_running)
    except ClientError:
        if require_running:
            raise
        raise
    with endpoint:
        protocol.send_frame(endpoint, {"op": "status"})
        response = protocol.recv_frame(endpoint)
    if not response.get("ok"):
        raise ClientError(str(response.get("error") or "provider status failed"))
    return response


def synthesize(
    text: str,
    *,
    model: str = catalog.MODEL_ID,
    rate: int = 170,
) -> tuple[bytes, dict]:
    request = protocol.validate_request(
        {"op": "synthesize", "model": model, "text": text, "rate": rate}
    )
    endpoint = connect(autostart=True)
    with endpoint:
        endpoint.settimeout(SYNTHESIS_TIMEOUT_S)
        protocol.send_frame(endpoint, request)
        response = protocol.recv_frame(endpoint)
        if not response.get("ok"):
            raise ClientError(str(response.get("error") or "Piper synthesis failed"))
        size = response.get("pcm_bytes")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > protocol.MAX_PCM_BYTES
        ):
            raise ClientError(f"provider returned invalid PCM size {size!r}")
        try:
            pcm = protocol.recv_exact(endpoint, size)
        except EOFError as error:
            raise ClientError("provider closed before sending all PCM audio") from error
    spec = catalog.get_model(model)
    if response.get("model") != model or response.get("voice") != spec.voice:
        raise ClientError("provider returned a different voice than requested")
    if response.get("sample_rate") != spec.sample_rate:
        raise ClientError("provider returned an unexpected sample rate")
    return pcm, response


__all__ = ["ClientError", "connect", "status", "synthesize"]
