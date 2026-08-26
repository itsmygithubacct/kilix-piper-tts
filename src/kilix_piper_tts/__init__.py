"""Persistent, sandboxed Piper synthesis for Kilix."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def _version() -> str:
    try:
        return metadata.version("kilix-piper-tts")
    except metadata.PackageNotFoundError:
        try:
            return Path(__file__).resolve().parents[2].joinpath("VERSION").read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            return "0.0.0"


__version__ = _version()

__all__ = ["__version__"]
