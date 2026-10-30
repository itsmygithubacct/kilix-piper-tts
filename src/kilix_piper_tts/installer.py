"""Checksum-pinned, atomic voice installation using Kilix Content."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from . import catalog, paths


MANIFEST_NAME = "kilix-piper-model.json"


class InstallError(RuntimeError):
    """The selected voice could not be installed or verified safely."""


def _kilix_download():
    """Return kilix-content's download primitive, including sibling checkouts."""
    try:
        return importlib.import_module("kilix_content.install").download
    except ImportError:
        sibling = Path(__file__).resolve().parents[2].parent / "kilix-content" / "src"
        if sibling.is_dir() and str(sibling) not in sys.path:
            sys.path.insert(0, str(sibling))
        try:
            return importlib.import_module("kilix_content.install").download
        except ImportError as error:
            raise InstallError(
                "kilix-content is required for model downloads. Install that "
                "Kilix module, or keep its checkout beside kilix-piper-tts."
            ) from error


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(model: catalog.ModelSpec) -> dict:
    manifest = {
        "schema": catalog.CATALOG_SCHEMA,
        "model": model.catalog_id,
        "voice": model.voice,
        "sample_rate": model.sample_rate,
        "piper_version": catalog.PIPER_VERSION,
        "piper_commit": catalog.PIPER_COMMIT,
        "voice_repository_commit": catalog.VOICE_REPOSITORY_COMMIT,
        "artifacts": [
            {
                "filename": artifact.filename,
                "size": artifact.size,
                "sha256": artifact.sha256,
            }
            for artifact in model.artifacts
        ],
    }
    if model.checkpoint is not None:
        manifest.pop("voice_repository_commit")
        manifest["checkpoint"] = model.checkpoint
        manifest["source"] = "local-export"
    return manifest


def inspect_install(
    catalog_id: str = catalog.MODEL_ID, *, verify_hashes: bool = False
) -> tuple[bool, str]:
    model = catalog.get_model(catalog_id)
    directory = paths.model_dir(model.catalog_id)
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return False, f"not installed; run: kilix-piper-tts install {model.catalog_id}"
    except OSError as error:
        return False, f"cannot inspect {directory}: {error}"
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False, f"{directory} is not a real model directory"
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    try:
        with open(manifest_path, encoding="utf-8") as stream:
            installed_manifest = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        return False, f"model manifest is missing or invalid: {error}"
    if installed_manifest != _manifest(model):
        return False, "model manifest does not match this provider catalog"
    for artifact in model.artifacts:
        filename = os.path.join(directory, artifact.filename)
        try:
            artifact_info = os.lstat(filename)
        except OSError as error:
            return False, f"required artifact {artifact.filename} is unavailable: {error}"
        if not stat.S_ISREG(artifact_info.st_mode) or stat.S_ISLNK(artifact_info.st_mode):
            return False, f"required artifact {artifact.filename} is not a regular file"
        if artifact_info.st_size != artifact.size:
            return False, (
                f"artifact {artifact.filename} has {artifact_info.st_size} bytes; "
                f"expected {artifact.size}"
            )
        if verify_hashes and _sha256(filename) != artifact.sha256:
            return False, f"artifact {artifact.filename} failed SHA-256 verification"
    return True, f"installed and {'fully verified' if verify_hashes else 'complete'}"


@contextlib.contextmanager
def _install_lock(model: catalog.ModelSpec) -> Iterator[None]:
    root = paths.ensure_private_dir(paths.models_dir())
    lock_path = os.path.join(root, f".{model.catalog_id}.install.lock")
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        raise InstallError(f"cannot lock model installation: {error}") from error
    try:
        yield
    finally:
        os.close(descriptor)


def _write_manifest(directory: str, model: catalog.ModelSpec) -> None:
    target = os.path.join(directory, MANIFEST_NAME)
    data = json.dumps(_manifest(model), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install_model(
    catalog_id: str = catalog.MODEL_ID,
    *,
    report: Callable[[str], None] = lambda _message: None,
    model_file: str | None = None,
    config_file: str | None = None,
) -> tuple[str, bool]:
    """Install one catalogued voice; return (directory, newly_installed)."""
    model = catalog.get_model(catalog_id)
    local = {}
    if model_file is not None or config_file is not None:
        if not model_file or not config_file or len(model.artifacts) != 2:
            raise InstallError("local installation requires both model and config files for a local-export voice")
        local = {model.model_filename: model_file, model.config_filename: config_file}
    if not local and any(not artifact.url for artifact in model.artifacts):
        # An already installed voice can still be verified idempotently below.
        if not inspect_install(catalog_id, verify_hashes=True)[0]:
            raise InstallError("This voice is a local export; supply --model-file and --config-file")
    target = paths.model_dir(model.catalog_id)
    with _install_lock(model):
        if os.path.lexists(target):
            installed, detail = inspect_install(model.catalog_id, verify_hashes=True)
            if installed:
                return target, False
            raise InstallError(
                f"refusing to replace the existing invalid model at {target}: {detail}. "
                "Move it aside and retry."
            )
        root = paths.models_dir()
        stage = tempfile.mkdtemp(prefix=f".{model.catalog_id}.stage-", dir=root)
        os.chmod(stage, 0o700)
        try:
            download = None if local else _kilix_download()
            for artifact in model.artifacts:
                destination = os.path.join(stage, artifact.filename)
                try:
                    if local:
                        source = os.open(local[artifact.filename], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                        with os.fdopen(source, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_size != artifact.size:
                                raise InstallError(f"{artifact.filename} is not the expected regular export file")
                            with open(destination, "xb") as output:
                                shutil.copyfileobj(stream, output, 1024 * 1024)
                        report(f"Imported {artifact.filename}")
                    else:
                        download(artifact.url, destination, report=report,
                                 expected_sha256=artifact.sha256)
                except Exception as error:
                    raise InstallError(
                        f"could not install {artifact.filename}: {error}"
                    ) from error
                actual_size = os.stat(destination).st_size
                if actual_size != artifact.size:
                    raise InstallError(
                        f"{artifact.filename} has {actual_size} bytes after download; "
                        f"expected {artifact.size}"
                    )
                if _sha256(destination) != artifact.sha256:
                    raise InstallError(f"{artifact.filename} failed SHA-256 verification")
                os.chmod(destination, 0o600)
                with open(destination, "rb") as stream:
                    os.fsync(stream.fileno())
            _write_manifest(stage, model)
            directory_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(stage, target)
            stage = ""
            parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
            raise
    verified, detail = inspect_install(model.catalog_id, verify_hashes=True)
    if not verified:
        raise InstallError(f"published model did not verify: {detail}")
    return target, True


__all__ = ["InstallError", "MANIFEST_NAME", "inspect_install", "install_model"]
