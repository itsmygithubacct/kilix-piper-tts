"""Kilix-owned durable model and ephemeral service paths."""

from __future__ import annotations

import os
import stat


DIR_MODE = 0o700
SOCKET_MODE = 0o600
SOCKET_NAME = "piper.sock"


class PathError(RuntimeError):
    """A path is outside the expected private Kilix layout or is unsafe."""


def _expand(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def gpu_terminal_home() -> str:
    return _expand(
        os.environ.get("GPU_TERMINAL_HOME")
        or os.path.join(os.path.expanduser("~"), ".local", "gpu_terminal")
    )


def storage_home() -> str:
    return _expand(
        os.environ.get("KILIX_STORAGE_HOME")
        or os.path.join(gpu_terminal_home(), "kilix")
    )


def session_home() -> str:
    return _expand(
        os.environ.get("KILIX_SESSION_HOME")
        or os.path.join(storage_home(), "session")
    )


def data_home() -> str:
    return _expand(
        os.environ.get("KILIX_DATA_HOME") or os.path.join(storage_home(), "data")
    )


def session_dir() -> str:
    return os.path.join(session_home(), "voice")


def socket_path() -> str:
    return os.path.join(session_dir(), SOCKET_NAME)


def models_dir() -> str:
    return os.path.join(data_home(), "voice", "models")


def model_dir(catalog_id: str) -> str:
    from .catalog import get_model

    model = get_model(catalog_id)
    return os.path.join(models_dir(), model.catalog_id)


def ensure_private_dir(path: str) -> str:
    """Create a mode-0700 directory and reject links, foreign owners or modes."""
    target = _expand(path)
    parent = os.path.dirname(target)
    try:
        if parent and parent != target:
            os.makedirs(parent, mode=DIR_MODE, exist_ok=True)
        created = False
        try:
            os.mkdir(target, DIR_MODE)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(
            target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise PathError(
            f"{target} is not a usable private directory: {error}. It must be "
            "a real directory owned by this user."
        ) from error
    try:
        info = os.fstat(descriptor)
        if created:
            os.fchmod(descriptor, DIR_MODE)
        elif info.st_uid != os.geteuid():
            raise PathError(
                f"{target} is owned by uid {info.st_uid}, not uid {os.geteuid()}"
            )
        elif stat.S_IMODE(info.st_mode) != DIR_MODE:
            raise PathError(
                f"{target} has mode {stat.S_IMODE(info.st_mode):04o}; expected 0700"
            )
    finally:
        os.close(descriptor)
    return target


__all__ = [
    "DIR_MODE",
    "PathError",
    "SOCKET_MODE",
    "data_home",
    "ensure_private_dir",
    "model_dir",
    "models_dir",
    "session_dir",
    "socket_path",
]
