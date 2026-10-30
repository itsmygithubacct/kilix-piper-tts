"""Persistent Piper worker process with cancellation and hard deadlines."""

from __future__ import annotations

import multiprocessing
import os
import select
import socket
import threading
import time
from multiprocessing.connection import Connection

from . import catalog, installer, paths


LOAD_TIMEOUT_S = 120.0
SYNTH_TIMEOUT_BASE_S = 60.0
SYNTH_TIMEOUT_PER_CHAR_S = 0.05


class RuntimeError(RuntimeError):
    """The Piper worker failed, timed out, or was cancelled."""


class ClientGone(RuntimeError):
    """The synthesis caller disconnected; no response should be sent."""


def _thread_count() -> int:
    raw = os.environ.get("KILIX_PIPER_THREADS", "2")
    try:
        return max(1, min(int(raw), 8))
    except ValueError:
        return 2


def _worker_main(connection: Connection, model_dir: str, model_id: str) -> None:
    """Own Piper and its model in a child that the service can kill safely."""
    try:
        threads = str(_thread_count())
        os.environ.setdefault("OMP_NUM_THREADS", threads)
        os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        from piper import PiperVoice, SynthesisConfig

        model = catalog.get_model(model_id)
        voice = PiperVoice.load(
            os.path.join(model_dir, model.model_filename),
            os.path.join(model_dir, model.config_filename),
            use_cuda=False,
        )
        if int(voice.config.sample_rate) != model.sample_rate:
            raise ValueError(
                f"voice reports {voice.config.sample_rate} Hz, expected "
                f"{model.sample_rate} Hz"
            )
        connection.send({"ok": True, "ready": True})
        while True:
            request = connection.recv()
            if request.get("op") == "close":
                return
            text = request["text"]
            rate = request["rate"]
            scale = 170.0 / float(rate)
            if model.checkpoint is not None:
                # Preserve the export's tuning and raw unity gain. Per-sentence
                # peak normalization changes the character and loudness.
                config = SynthesisConfig(
                    length_scale=voice.config.length_scale * scale,
                    normalize_audio=False,
                )
            else:
                config = SynthesisConfig(length_scale=scale)
            pieces: list[bytes] = []
            for chunk in voice.synthesize(text, config):
                if chunk.sample_rate != model.sample_rate:
                    raise ValueError(
                        f"Piper changed sample rate to {chunk.sample_rate} Hz"
                    )
                pieces.append(chunk.audio_int16_bytes)
            connection.send(
                {
                    "ok": True,
                    "pcm": b"".join(pieces),
                    "sample_rate": model.sample_rate,
                }
            )
    except EOFError:
        return
    except BaseException as error:
        try:
            connection.send(
                {"ok": False, "error": f"{type(error).__name__}: {error}"}
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class WorkerManager:
    """Serialise jobs through one reusable, killable CPU worker."""

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._job_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process: multiprocessing.Process | None = None
        self._pipe: Connection | None = None
        self._busy = False
        self._model_id: str | None = None

    @property
    def loaded_model(self) -> str | None:
        with self._state_lock:
            return self._model_id if self._process is not None and self._process.is_alive() else None

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return self._busy

    @property
    def loaded(self) -> bool:
        with self._state_lock:
            return bool(self._process is not None and self._process.is_alive())

    def _drop_worker(self) -> None:
        with self._state_lock:
            process, self._process = self._process, None
            pipe, self._pipe = self._pipe, None
            self._model_id = None
        if pipe is not None:
            pipe.close()
        if process is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    @staticmethod
    def _client_closed(client: socket.socket) -> bool:
        try:
            return client.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _wait(
        self, pipe: Connection, client: socket.socket, deadline: float
    ) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                readable, _, _ = select.select(
                    (pipe.fileno(), client.fileno()), (), (), min(remaining, 0.25)
                )
            except (OSError, ValueError) as error:
                raise RuntimeError(f"worker communication failed: {error}") from error
            if client.fileno() in readable and self._client_closed(client):
                raise ClientGone("synthesis client disconnected")
            if pipe.fileno() in readable:
                try:
                    message = pipe.recv()
                except (EOFError, OSError) as error:
                    raise RuntimeError("Piper worker exited without a result") from error
                if not isinstance(message, dict):
                    raise RuntimeError("Piper worker returned an invalid result")
                return message

    def _ensure_worker(self, client: socket.socket, model_id: str) -> Connection:
        with self._state_lock:
            process, pipe = self._process, self._pipe
            loaded_model = self._model_id
        if process is not None and pipe is not None and process.is_alive() and loaded_model == model_id:
            return pipe
        self._drop_worker()
        installed, detail = installer.inspect_install(model_id, verify_hashes=True)
        if not installed:
            raise RuntimeError(detail)
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_main,
            args=(child, paths.model_dir(model_id), model_id),
            name="kilix-piper-worker",
            daemon=True,
        )
        process.start()
        child.close()
        with self._state_lock:
            self._process = process
            self._pipe = parent
        try:
            message = self._wait(parent, client, time.monotonic() + LOAD_TIMEOUT_S)
        except TimeoutError as error:
            self._drop_worker()
            raise RuntimeError(
                f"Piper did not load {model_id} within {LOAD_TIMEOUT_S:.0f} seconds"
            ) from error
        if not message.get("ok") or not message.get("ready"):
            self._drop_worker()
            raise RuntimeError(
                f"Piper could not load {model_id}: " + str(message.get("error") or message)
            )
        with self._state_lock:
            self._model_id = model_id
        return parent

    def synthesize(
        self, text: str, rate: int, client: socket.socket, *, model: str = catalog.MODEL_ID
    ) -> tuple[bytes, int]:
        spec = catalog.get_model(model)
        installed, detail = installer.inspect_install(model)
        if not installed:
            raise RuntimeError(detail)
        while not self._job_lock.acquire(timeout=0.1):
            if self._client_closed(client):
                raise ClientGone("synthesis client disconnected while queued")
        try:
            with self._state_lock:
                self._busy = True
            try:
                pipe = self._ensure_worker(client, model)
                pipe.send({"op": "synthesize", "text": text, "rate": rate})
                timeout = SYNTH_TIMEOUT_BASE_S + len(text) * SYNTH_TIMEOUT_PER_CHAR_S
                try:
                    message = self._wait(pipe, client, time.monotonic() + timeout)
                except TimeoutError as error:
                    self._drop_worker()
                    raise RuntimeError(
                        f"Piper exceeded its {timeout:.1f}-second synthesis deadline"
                    ) from error
                if not message.get("ok"):
                    self._drop_worker()
                    raise RuntimeError(
                        "Piper synthesis failed: "
                        + str(message.get("error") or "unknown worker error")
                    )
                pcm = message.get("pcm")
                sample_rate = message.get("sample_rate")
                if not isinstance(pcm, bytes) or sample_rate != spec.sample_rate:
                    self._drop_worker()
                    raise RuntimeError("Piper worker returned invalid audio metadata")
                return pcm, sample_rate
            except ClientGone:
                self._drop_worker()
                raise
            except (BrokenPipeError, EOFError, OSError) as error:
                self._drop_worker()
                raise RuntimeError(f"Piper worker communication failed: {error}") from error
            finally:
                with self._state_lock:
                    self._busy = False
        finally:
            self._job_lock.release()

    def close(self) -> None:
        self._drop_worker()


__all__ = ["ClientGone", "RuntimeError", "WorkerManager"]
