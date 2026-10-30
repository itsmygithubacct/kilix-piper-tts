from __future__ import annotations

import dataclasses
import hashlib
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from kilix_piper_tts import catalog, client, installer, runtime


class NamedVoiceTests(unittest.TestCase):
    def test_local_import_is_pinned_atomic_and_network_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "export"
            source.mkdir()
            payloads = (b"model export", b"config export")
            files = [source / "voice.onnx", source / "voice.onnx.json"]
            for path, data in zip(files, payloads):
                path.write_bytes(data)
            spec = dataclasses.replace(catalog.FEMALE_02, artifacts=tuple(
                dataclasses.replace(artifact, size=len(data), sha256=hashlib.sha256(data).hexdigest())
                for artifact, data in zip(catalog.FEMALE_02.artifacts, payloads)))
            with mock.patch.dict("os.environ", {"KILIX_DATA_HOME": str(root / "data")}), \
                    mock.patch.dict(catalog.MODEL_BY_ID, {spec.catalog_id: spec}), \
                    mock.patch.object(installer, "_kilix_download") as download:
                files[0].write_bytes(b"wrong export")
                with self.assertRaisesRegex(installer.InstallError, "SHA-256"):
                    installer.install_model(spec.catalog_id, model_file=str(files[0]), config_file=str(files[1]))
                self.assertFalse(Path(installer.paths.model_dir(spec.catalog_id)).exists())
                files[0].write_bytes(payloads[0])
                target, created = installer.install_model(spec.catalog_id, model_file=str(files[0]), config_file=str(files[1]))
                self.assertTrue(created)
                self.assertTrue(installer.inspect_install(spec.catalog_id, verify_hashes=True)[0])
                self.assertEqual(installer.install_model(spec.catalog_id), (target, False))
                download.assert_not_called()

    def test_worker_reuses_same_voice_and_replaces_different_voice(self):
        manager = runtime.WorkerManager()
        parent, child = mock.Mock(), mock.Mock()
        context = mock.Mock()
        context.Pipe.return_value = (parent, child)
        first, second = mock.Mock(), mock.Mock()
        first.is_alive.return_value = second.is_alive.return_value = True
        context.Process.side_effect = (first, second)
        manager._context = context
        endpoint, peer = socket.socketpair()
        with endpoint, peer, \
                mock.patch.object(installer, "inspect_install", return_value=(True, "verified")), \
                mock.patch.object(manager, "_wait", return_value={"ok": True, "ready": True}):
            manager._ensure_worker(endpoint, catalog.MODEL_ID)
            manager._ensure_worker(endpoint, catalog.MODEL_ID)
            self.assertEqual(context.Process.call_count, 1)
            manager._ensure_worker(endpoint, "kilix-female-02")
            self.assertEqual(context.Process.call_count, 2)
            first.kill.assert_called_once()
            self.assertEqual(context.Process.call_args.kwargs["args"][2], "kilix-female-02")
            self.assertEqual(manager.loaded_model, "kilix-female-02")
        manager.close()

    def test_female02_preserves_export_speed_and_disables_peak_normalization(self):
        connection = mock.Mock()
        connection.recv.side_effect = [{"text": "Hello", "rate": 200}, {"op": "close"}]
        voice = mock.Mock()
        voice.config.sample_rate = 22050
        voice.config.length_scale = 1.2
        voice.synthesize.return_value = [types.SimpleNamespace(sample_rate=22050, audio_int16_bytes=b"\0\0")]
        piper = types.SimpleNamespace(PiperVoice=mock.Mock(), SynthesisConfig=mock.Mock())
        piper.PiperVoice.load.return_value = voice
        with mock.patch.dict(sys.modules, {"piper": piper}):
            runtime._worker_main(connection, "/fixture", "kilix-female-02")
        kwargs = piper.SynthesisConfig.call_args.kwargs
        self.assertAlmostEqual(kwargs["length_scale"], 1.2 * 170 / 200)
        self.assertFalse(kwargs["normalize_audio"])
        self.assertTrue(connection.send.call_args.args[0]["ok"])

    def test_client_refuses_a_different_voice_in_response(self):
        endpoint = mock.MagicMock()
        with mock.patch.object(client, "connect", return_value=endpoint), \
                mock.patch.object(client.protocol, "send_frame"), \
                mock.patch.object(client.protocol, "recv_frame", return_value={
                    "ok": True, "pcm_bytes": 2, "sample_rate": 22050,
                    "model": "kilix-female-02", "voice": catalog.VOICE_NAME}), \
                mock.patch.object(client.protocol, "recv_exact", return_value=b"\0\0"):
            with self.assertRaisesRegex(client.ClientError, "different voice"):
                client.synthesize("Hello", model="kilix-female-02")
