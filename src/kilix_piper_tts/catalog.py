"""Immutable Piper voice catalog; importing it performs no I/O."""

from __future__ import annotations

from dataclasses import dataclass


CATALOG_SCHEMA = "kilix.piper.models/v1"
MODEL_ID = "piper-en-us-kristin-medium"
VOICE_NAME = "en_US-kristin-medium"
PIPER_VERSION = "1.7.0"
PIPER_COMMIT = "7b8e8f7197a480047677715f00d3d78903b55a2a"
VOICE_REPOSITORY_COMMIT = "39ab474be869e9181350af6a65e4953eef67aaa0"
SAMPLE_RATE = 22050

_BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/"
    f"{VOICE_REPOSITORY_COMMIT}/en/en_US/kristin/medium"
)


@dataclass(frozen=True)
class Artifact:
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelSpec:
    catalog_id: str
    voice: str
    sample_rate: int
    language: str
    quality: str
    summary: str
    artifacts: tuple[Artifact, ...]

    @property
    def download_size(self) -> int:
        return sum(artifact.size for artifact in self.artifacts)

    @property
    def model_filename(self) -> str:
        return f"{self.voice}.onnx"

    @property
    def config_filename(self) -> str:
        return f"{self.voice}.onnx.json"


KRISTIN = ModelSpec(
    catalog_id=MODEL_ID,
    voice=VOICE_NAME,
    sample_rate=SAMPLE_RATE,
    language="en_US",
    quality="medium",
    summary="Piper medium-quality US English single-speaker Kristin voice",
    artifacts=(
        Artifact(
            f"{VOICE_NAME}.onnx",
            f"{_BASE_URL}/{VOICE_NAME}.onnx",
            63_531_379,
            "5849957f929cbf720c258f8458692d6103fff2f0e3d3b19c8259474bb06a18d4",
        ),
        Artifact(
            f"{VOICE_NAME}.onnx.json",
            f"{_BASE_URL}/{VOICE_NAME}.onnx.json",
            4_968,
            "5681426d4aead22195de70531eeeeddb46493cfaffc5764b2ea3db73428b651c",
        ),
        Artifact(
            "MODEL_CARD",
            f"{_BASE_URL}/MODEL_CARD",
            479,
            "8c181b1d5f7d8152b914faab941fcbe6b8f0495e63e5189d6f27b8c68c8393fc",
        ),
    ),
)

MODELS = (KRISTIN,)
MODEL_BY_ID = {model.catalog_id: model for model in MODELS}
MODEL_IDS = tuple(MODEL_BY_ID)


def get_model(catalog_id: str) -> ModelSpec:
    try:
        return MODEL_BY_ID[catalog_id]
    except KeyError as error:
        raise ValueError(
            f"unknown Piper model {catalog_id!r}; choose one of: "
            f"{', '.join(MODEL_IDS)}"
        ) from error


__all__ = [
    "Artifact",
    "CATALOG_SCHEMA",
    "KRISTIN",
    "MODEL_BY_ID",
    "MODEL_ID",
    "MODEL_IDS",
    "MODELS",
    "ModelSpec",
    "PIPER_COMMIT",
    "PIPER_VERSION",
    "SAMPLE_RATE",
    "VOICE_NAME",
    "VOICE_REPOSITORY_COMMIT",
    "get_model",
]
