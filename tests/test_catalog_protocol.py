from __future__ import annotations

import json
import socket
import unittest

from kilix_piper_tts import catalog, protocol


class CatalogTests(unittest.TestCase):
    def test_named_catalog_preserves_kristin(self) -> None:
        self.assertEqual(catalog.MODEL_IDS, ("piper-en-us-kristin-medium", "kilix-female-02"))
        model = catalog.KRISTIN
        self.assertEqual(model.voice, "en_US-kristin-medium")
        self.assertEqual(model.sample_rate, 22050)
        self.assertEqual(model.download_size, 63_536_826)
        self.assertEqual(
            {artifact.filename for artifact in model.artifacts},
            {
                "en_US-kristin-medium.onnx",
                "en_US-kristin-medium.onnx.json",
                "MODEL_CARD",
            },
        )

    def test_every_artifact_is_commit_pinned_and_has_sha256(self) -> None:
        for artifact in catalog.KRISTIN.artifacts:
            self.assertIn(catalog.VOICE_REPOSITORY_COMMIT, artifact.url)
            self.assertEqual(len(artifact.sha256), 64)
            int(artifact.sha256, 16)

    def test_female02_artifacts_are_the_kilix_voice_release(self) -> None:
        self.assertEqual(catalog.FEMALE_02.checkpoint, 80)
        self.assertEqual(
            tuple((item.filename, item.size, item.sha256, item.url) for item in catalog.FEMALE_02.artifacts),
            (
                (
                    "kilix-female-02.onnx",
                    63_522_221,
                    "263beafc9e5fa759878294e7c45bb3639d930075c845ea96c71331edeeb86d61",
                    "https://github.com/itsmygithubacct/kilix-voice/releases/download/kilix-female-02-epoch80/kilix-female-02.onnx",
                ),
                (
                    "kilix-female-02.onnx.json",
                    5_486,
                    "062ed73be776e0950198aef0171e9f3a1a50ba520b3a0a91ee5a3f11421e43b5",
                    "https://github.com/itsmygithubacct/kilix-voice/releases/download/kilix-female-02-epoch80/kilix-female-02.onnx.json",
                ),
            ),
        )


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip_preserves_utf8(self) -> None:
        first, second = socket.socketpair()
        with first, second:
            protocol.send_frame(first, {"text": "Hello, café 🐈"})
            self.assertEqual(
                protocol.recv_frame(second), {"text": "Hello, café 🐈"}
            )

    def test_registered_ordinary_text_is_normalised(self) -> None:
        request = protocol.validate_request(
            {
                "op": "synthesize",
                "model": catalog.MODEL_ID,
                "text": "  Hello, Kristin.  ",
                "rate": 200,
                "ignored": "never reaches runtime",
            }
        )
        self.assertEqual(
            request,
            {
                "op": "synthesize",
                "model": catalog.MODEL_ID,
                "text": "Hello, Kristin.",
                "rate": 200,
            },
        )

    def test_paths_urls_phonemes_and_free_rates_are_refused(self) -> None:
        valid = {
            "op": "synthesize",
            "model": catalog.MODEL_ID,
            "text": "hello",
            "rate": 170,
        }
        cases = (
            {**valid, "model": "/tmp/voice.onnx"},
            {**valid, "model": "https://example.invalid/voice.onnx"},
            {**valid, "text": "Say [[ h ə l oʊ ]]"},
            {**valid, "rate": 171},
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(
                protocol.ProtocolError
            ):
                protocol.validate_request(payload)

    def test_oversized_and_malformed_frames_are_bounded(self) -> None:
        first, second = socket.socketpair()
        with first, second:
            first.sendall(protocol.HEADER.pack(protocol.MAX_FRAME_BYTES + 1))
            with self.assertRaises(protocol.ProtocolError):
                protocol.recv_frame(second)

        first, second = socket.socketpair()
        with first, second:
            raw = json.dumps(["not", "an", "object"]).encode()
            first.sendall(protocol.HEADER.pack(len(raw)) + raw)
            with self.assertRaises(protocol.ProtocolError):
                protocol.recv_frame(second)


if __name__ == "__main__":
    unittest.main()
