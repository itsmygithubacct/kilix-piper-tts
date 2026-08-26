from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from kilix_piper_tts import catalog, installer, paths


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        environment = mock.patch.dict(
            os.environ,
            {
                "KILIX_DATA_HOME": str(self.root / "data"),
                "KILIX_SESSION_HOME": str(self.root / "session"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

        self.payloads = {
            "tiny.onnx": b"fake model bytes",
            "tiny.onnx.json": b'{"audio":{"sample_rate":22050}}',
            "MODEL_CARD": b"public domain fixture\n",
        }
        artifacts = tuple(
            catalog.Artifact(
                filename,
                f"https://example.invalid/{filename}",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
            for filename, payload in self.payloads.items()
        )
        self.model = catalog.ModelSpec(
            catalog.MODEL_ID,
            "tiny",
            22050,
            "en_US",
            "test",
            "fixture",
            artifacts,
        )
        model_map = mock.patch.object(
            catalog, "MODEL_BY_ID", {catalog.MODEL_ID: self.model}
        )
        model_map.start()
        self.addCleanup(model_map.stop)

    def fake_download(
        self, url: str, destination: str, report, expected_sha256: str
    ) -> str:
        name = url.rsplit("/", 1)[-1]
        payload = self.payloads[name]
        self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
        pathlib.Path(destination).write_bytes(payload)
        report(f"fixture {name}")
        return destination

    def test_install_stages_verifies_and_publishes_atomically(self) -> None:
        reports: list[str] = []
        with mock.patch.object(installer, "_kilix_download", return_value=self.fake_download):
            directory, created = installer.install_model(report=reports.append)

        self.assertTrue(created)
        target = pathlib.Path(directory)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        for name, payload in self.payloads.items():
            artifact = target / name
            self.assertEqual(artifact.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
        manifest = json.loads((target / installer.MANIFEST_NAME).read_text())
        self.assertEqual(manifest["model"], catalog.MODEL_ID)
        self.assertTrue(installer.inspect_install(verify_hashes=True)[0])
        self.assertEqual(len(reports), len(self.payloads))
        self.assertEqual(list(target.parent.glob(".*.stage-*")), [])

    def test_verified_install_is_idempotent_without_downloading(self) -> None:
        with mock.patch.object(installer, "_kilix_download", return_value=self.fake_download):
            first, created = installer.install_model()
        self.assertTrue(created)
        with mock.patch.object(installer, "_kilix_download") as download:
            second, created = installer.install_model()
        self.assertFalse(created)
        self.assertEqual(first, second)
        download.assert_not_called()

    def test_invalid_existing_target_is_never_replaced(self) -> None:
        target = pathlib.Path(paths.model_dir(catalog.MODEL_ID))
        target.mkdir(parents=True)
        marker = target / "keep"
        marker.write_text("user data")
        with mock.patch.object(installer, "_kilix_download") as download, \
                self.assertRaises(installer.InstallError) as caught:
            installer.install_model()
        download.assert_not_called()
        self.assertEqual(marker.read_text(), "user data")
        self.assertIn("refusing to replace", str(caught.exception))

    def test_failed_download_leaves_no_partial_model(self) -> None:
        def fail(*_args, **_kwargs):
            raise RuntimeError("network fixture failed")

        with mock.patch.object(installer, "_kilix_download", return_value=fail), \
                self.assertRaises(installer.InstallError):
            installer.install_model()
        self.assertFalse(pathlib.Path(paths.model_dir(catalog.MODEL_ID)).exists())
        root = pathlib.Path(paths.models_dir())
        self.assertEqual(list(root.glob(".*.stage-*")), [])


if __name__ == "__main__":
    unittest.main()
