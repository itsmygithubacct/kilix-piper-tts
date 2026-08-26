from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock

from kilix_piper_tts import catalog, cli, paths, protocol, runtime, service


class FakeManager:
    loaded = True
    busy = False

    def synthesize(self, text: str, rate: int, _client) -> tuple[bytes, int]:
        return (text.encode("utf-8") + bytes([rate % 256])) * 2, 22050

    def close(self) -> None:
        pass


class ServiceTests(unittest.TestCase):
    def exchange(self, request: dict) -> tuple[dict, bytes]:
        provider = service.ProviderService()
        provider.manager = FakeManager()
        server_endpoint, client_endpoint = socket.socketpair()
        self.assertTrue(provider.client_slots.acquire(blocking=False))
        worker = threading.Thread(target=provider._handle, args=(server_endpoint,))
        worker.start()
        with client_endpoint:
            protocol.send_frame(client_endpoint, request)
            response = protocol.recv_frame(client_endpoint)
            pcm = (
                protocol.recv_exact(client_endpoint, response["pcm_bytes"])
                if response.get("ok") and "pcm_bytes" in response
                else b""
            )
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        return response, pcm

    def test_synthesis_response_has_bounded_pcm_metadata(self) -> None:
        response, pcm = self.exchange(
            {
                "op": "synthesize",
                "model": catalog.MODEL_ID,
                "text": "hello",
                "rate": 200,
            }
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["voice"], catalog.VOICE_NAME)
        self.assertEqual(response["sample_rate"], 22050)
        self.assertEqual(response["pcm_bytes"], len(pcm))

    def test_invalid_raw_phonemes_never_reach_runtime(self) -> None:
        response, pcm = self.exchange(
            {
                "op": "synthesize",
                "model": catalog.MODEL_ID,
                "text": "[[ unsafe ]]",
                "rate": 170,
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(pcm, b"")
        self.assertIn("raw Piper", response["error"])

    def test_stale_socket_is_replaced_but_live_service_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"KILIX_SESSION_HOME": directory}
        ):
            paths.ensure_private_dir(paths.session_dir())
            socket_path = paths.socket_path()
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(socket_path)
            stale.close()

            listener, _identity = service._prepare_socket(socket_path)
            try:
                self.assertTrue(stat.S_ISSOCK(os.lstat(socket_path).st_mode))
                self.assertEqual(stat.S_IMODE(os.lstat(socket_path).st_mode), 0o600)
                with self.assertRaises(service.ServiceError) as caught:
                    service._prepare_socket(socket_path)
                self.assertIn("already listening", str(caught.exception))
            finally:
                listener.close()
                if os.path.lexists(socket_path):
                    os.unlink(socket_path)

    def test_a_disconnected_queued_client_never_reaches_piper(self) -> None:
        manager = runtime.WorkerManager()
        manager._job_lock.acquire()
        provider_end, client_end = socket.socketpair()
        failures: list[BaseException] = []

        def queued() -> None:
            try:
                manager.synthesize("cancel me", 170, provider_end)
            except BaseException as error:
                failures.append(error)

        with mock.patch.object(
            runtime.installer, "inspect_install", return_value=(True, "complete")
        ):
            worker = threading.Thread(target=queued)
            worker.start()
            client_end.close()
            worker.join(timeout=2)
        manager._job_lock.release()
        provider_end.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], runtime.ClientGone)
        self.assertFalse(manager.loaded)


class CliTests(unittest.TestCase):
    def test_models_json_is_download_free(self) -> None:
        output = io.StringIO()
        with mock.patch.object(cli.installer, "inspect_install", return_value=(False, "missing")), \
                mock.patch.object(cli.installer, "install_model") as install, \
                contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["models", "--json"]), 0)
        install.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], catalog.CATALOG_SCHEMA)
        self.assertEqual(payload["models"][0]["id"], catalog.MODEL_ID)

    def test_synthesize_writes_private_no_overwrite_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "speech.wav"
            metadata = {
                "voice": catalog.VOICE_NAME,
                "rate": 170,
                "sample_rate": 22050,
            }
            with mock.patch.object(
                cli.client, "synthesize", return_value=(b"\x01\x00" * 20, metadata)
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli.main(["synthesize", "hello", "--output", str(target)]), 0
                )
            self.assertEqual(target.read_bytes()[:4], b"RIFF")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            original = target.read_bytes()
            with mock.patch.object(
                cli.client, "synthesize", return_value=(b"\x02\x00", metadata)
            ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["synthesize", "again", "--output", str(target)])
            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
