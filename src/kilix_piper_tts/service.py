"""Private AF_UNIX service that keeps the Piper worker warm."""

from __future__ import annotations

import os
import signal
import socket
import stat
import struct
import threading
import time

from . import __version__, catalog, installer, paths, protocol, runtime


REQUEST_TIMEOUT_S = 5.0
MAX_CLIENTS = 8
_UCRED = struct.Struct("iII")


class ServiceError(RuntimeError):
    """The local provider service cannot start or serve safely."""


def _peer_uid(endpoint: socket.socket) -> int:
    raw = endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    _pid, uid, _gid = _UCRED.unpack(raw)
    return uid


def _prepare_socket(path: str) -> tuple[socket.socket, tuple[int, int]]:
    paths.ensure_private_dir(paths.session_dir())
    if len(os.fsencode(path)) >= 104:
        raise ServiceError(
            f"provider socket path is too long: {path}. Set KILIX_SESSION_HOME "
            "to a shorter private directory."
        )
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None:
        if info.st_uid != os.geteuid() or not stat.S_ISSOCK(info.st_mode):
            raise ServiceError(
                f"refusing to replace {path}: it is not this user's stale socket"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        except TimeoutError as error:
            raise ServiceError(
                f"the existing Piper socket at {path} is busy; refusing to "
                "replace a possibly live service"
            ) from error
        except OSError as error:
            raise ServiceError(
                f"cannot verify the existing Piper socket at {path}: {error}"
            ) from error
        else:
            raise ServiceError(f"a Piper provider is already listening at {path}")
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        os.chmod(path, paths.SOCKET_MODE)
        listener.listen(MAX_CLIENTS)
        listener.settimeout(1.0)
        info = os.lstat(path)
        return listener, (info.st_dev, info.st_ino)
    except BaseException:
        listener.close()
        raise


class ProviderService:
    def __init__(self, *, idle_seconds: float = 300.0) -> None:
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.path = paths.socket_path()
        self.manager = runtime.WorkerManager()
        self.stop = threading.Event()
        self.last_activity = time.monotonic()
        self.client_slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.listener: socket.socket | None = None
        self.socket_identity: tuple[int, int] | None = None

    def _status(self) -> dict:
        installed, detail = installer.inspect_install(catalog.MODEL_ID)
        return {
            "ok": True,
            "version": __version__,
            "model": catalog.MODEL_ID,
            "voice": catalog.VOICE_NAME,
            "sample_rate": catalog.SAMPLE_RATE,
            "installed": installed,
            "detail": detail,
            "loaded": self.manager.loaded,
            "busy": self.manager.busy,
            "cpu_only": True,
        }

    def _handle(self, endpoint: socket.socket) -> None:
        try:
            endpoint.settimeout(REQUEST_TIMEOUT_S)
            if _peer_uid(endpoint) != os.geteuid():
                raise ServiceError("provider connections must come from the service UID")
            request = protocol.validate_request(protocol.recv_frame(endpoint))
            self.last_activity = time.monotonic()
            if request["op"] == "status":
                protocol.send_frame(endpoint, self._status())
                return
            endpoint.settimeout(None)
            pcm, sample_rate = self.manager.synthesize(
                request["text"], request["rate"], endpoint
            )
            if len(pcm) > protocol.MAX_PCM_BYTES:
                raise ServiceError(
                    f"generated audio is {len(pcm)} bytes; provider limit is "
                    f"{protocol.MAX_PCM_BYTES}"
                )
            protocol.send_frame(
                endpoint,
                {
                    "ok": True,
                    "model": request["model"],
                    "voice": catalog.VOICE_NAME,
                    "rate": request["rate"],
                    "sample_rate": sample_rate,
                    "pcm_bytes": len(pcm),
                },
            )
            endpoint.sendall(pcm)
        except runtime.ClientGone:
            pass
        except (ServiceError, protocol.ProtocolError, runtime.RuntimeError) as error:
            try:
                protocol.send_frame(endpoint, {"ok": False, "error": str(error)})
            except (BrokenPipeError, EOFError, OSError, protocol.ProtocolError):
                pass
        except (EOFError, TimeoutError, ConnectionError, OSError):
            pass
        finally:
            self.last_activity = time.monotonic()
            endpoint.close()
            self.client_slots.release()

    def serve(self) -> None:
        listener, identity = _prepare_socket(self.path)
        self.listener = listener
        self.socket_identity = identity
        try:
            while not self.stop.is_set():
                if (
                    not self.manager.busy
                    and time.monotonic() - self.last_activity >= self.idle_seconds
                ):
                    return
                try:
                    endpoint, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop.is_set():
                        return
                    raise
                if not self.client_slots.acquire(blocking=False):
                    try:
                        protocol.send_frame(
                            endpoint,
                            {"ok": False, "error": "provider client limit reached"},
                        )
                    finally:
                        endpoint.close()
                    continue
                threading.Thread(
                    target=self._handle,
                    args=(endpoint,),
                    name="kilix-piper-client",
                    daemon=True,
                ).start()
        finally:
            listener.close()
            self.manager.close()
            try:
                info = os.lstat(self.path)
                if (info.st_dev, info.st_ino) == identity:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass

    def request_stop(self, _signum: int | None = None, _frame=None) -> None:
        self.stop.set()
        if self.listener is not None:
            self.listener.close()


def run_service(*, idle_seconds: float = 300.0) -> None:
    service = ProviderService(idle_seconds=idle_seconds)
    signal.signal(signal.SIGTERM, service.request_stop)
    signal.signal(signal.SIGINT, service.request_stop)
    service.serve()


__all__ = ["ProviderService", "ServiceError", "run_service"]
