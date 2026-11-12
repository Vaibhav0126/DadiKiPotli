"""Gemini TTS retry and no-fallback rules, with the client mocked out.

    .venv/bin/python -m unittest tests.test_tts
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import audio_engine
from src.config import Config


class _FakeError(Exception):
    """Carries the status text Gemini puts in its error messages."""


def _ok_response(pcm: bytes = b"\x00\x01" * 2400) -> mock.MagicMock:
    inline = mock.MagicMock(data=pcm, mime_type="audio/L16;codec=pcm;rate=24000")
    resp = mock.MagicMock()
    resp.candidates[0].content.parts[0].inline_data = inline
    return resp


def _cfg(*, allow_edge: bool = False) -> Config:
    cfg = Config()
    cfg.audio.models = ["tts-a", "tts-b"]
    cfg.audio.allow_edge_fallback = allow_edge
    return cfg


class TTSRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        audio_engine._active_tts_model = None
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

        self.sleep = mock.patch.object(audio_engine.time, "sleep").start()
        self.edge = mock.patch.object(audio_engine, "_synthesize_edge").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self._tmp.cleanup)

    def _client_with(self, side_effect) -> mock.MagicMock:
        client = mock.MagicMock()
        client.models.generate_content.side_effect = side_effect
        return mock.patch.object(audio_engine, "_client", return_value=client).start()

    def test_transient_errors_retry_with_backoff_then_succeed(self) -> None:
        self._client_with([
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("503 UNAVAILABLE"),
            _ok_response(),
        ])

        spoken = audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.assertEqual(spoken.backend, "gemini:tts-a")
        self.assertEqual(spoken.voice, _cfg().audio.voice)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 4])
        self.edge.assert_not_called()

    def test_persistent_capacity_errors_raise_and_never_touch_edge(self) -> None:
        self._client_with(_FakeError("503 UNAVAILABLE: high demand"))

        with self.assertRaises(RuntimeError):
            audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.edge.assert_not_called()
        # 5 tries per model -> 4 waits per model, 2 models: 2,4,8,16 twice.
        waits = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(waits, [2, 4, 8, 16, 2, 4, 8, 16])

    def test_backoff_is_capped(self) -> None:
        self.assertEqual(audio_engine._backoff_s(0), 2)
        self.assertEqual(audio_engine._backoff_s(4), 32)
        self.assertEqual(audio_engine._backoff_s(9), 32)

    def test_non_transient_error_raises_immediately(self) -> None:
        self._client_with(_FakeError("400 INVALID_ARGUMENT: bad voice"))

        with self.assertRaises(RuntimeError):
            audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.sleep.assert_not_called()
        self.edge.assert_not_called()

    def test_missing_model_moves_on_without_sleeping(self) -> None:
        self._client_with([_FakeError("404 NOT_FOUND: model gone"), _ok_response()])

        spoken = audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.assertEqual(spoken.backend, "gemini:tts-b")
        self.sleep.assert_not_called()

    def test_empty_audio_is_retried(self) -> None:
        empty = mock.MagicMock()
        empty.candidates[0].content.parts[0].inline_data = None
        self._client_with([empty, _ok_response()])

        spoken = audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.assertEqual(spoken.backend, "gemini:tts-a")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2])

    def test_response_without_candidates_is_retried(self) -> None:
        # Seen live: Gemini returned candidates=None for a scene.
        no_candidates = mock.MagicMock(candidates=None)
        self._client_with([no_candidates, _ok_response()])

        spoken = audio_engine._synthesize(_cfg(), "नमस्ते", self.dir / "scene_01")

        self.assertEqual(spoken.backend, "gemini:tts-a")
        self.edge.assert_not_called()

    def test_gemini_failure_is_fatal_by_default(self) -> None:
        self._client_with(_FakeError("503 UNAVAILABLE"))

        with self.assertRaisesRegex(RuntimeError, "edge-tts fallback is off"):
            audio_engine._synthesize(_cfg(allow_edge=False), "नमस्ते", self.dir / "s")

        self.edge.assert_not_called()

    def test_allow_edge_uses_edge_only_for_the_failed_scene(self) -> None:
        self._client_with(_FakeError("503 UNAVAILABLE"))
        self.edge.return_value = []

        spoken = audio_engine._synthesize(_cfg(allow_edge=True), "नमस्ते", self.dir / "s")

        self.assertEqual(spoken.backend, "edge-fallback")
        self.edge.assert_called_once()

    def test_gemini_is_kept_after_a_failed_scene(self) -> None:
        # One scene fails and falls back under --allow-edge. The next scene
        # must still go to Gemini, not stay on edge-tts.
        self._client_with([
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _FakeError("429 RESOURCE_EXHAUSTED"),
            _ok_response(),
        ])
        self.edge.return_value = []

        first = audio_engine._synthesize(_cfg(allow_edge=True), "एक", self.dir / "a")
        second = audio_engine._synthesize(_cfg(allow_edge=True), "दो", self.dir / "b")

        self.assertEqual(first.backend, "edge-fallback")
        self.assertTrue(second.backend.startswith("gemini"))
        self.assertEqual(self.edge.call_count, 1)


if __name__ == "__main__":
    unittest.main()
