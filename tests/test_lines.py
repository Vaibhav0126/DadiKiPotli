"""Per-line voices, line-level timing and script line sync. No API calls.

The scene-audio test runs real ffmpeg on synthetic silence, so it checks the
actual speed-up and join durations.

    .venv/bin/python -m unittest tests.test_lines
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import audio_engine, script_engine
from src.audio_engine import SceneSpeech
from src.config import Config
from src.ffmpeg_util import probe_duration
from src.models import Line, Scene

SILENCE_HALF_SECOND = b"\x00\x00" * 12000  # 0.5s of 24 kHz mono 16-bit


def _scene(lines: list[Line]) -> Scene:
    return Scene(
        scene_number=1,
        beat="hook",
        hindi_narration=" ".join(l.hindi for l in lines),
        hinglish_caption=" ".join(l.hinglish for l in lines),
        image_prompt="x",
        lines=lines,
    )


class VoiceChoiceTests(unittest.TestCase):
    def test_narrator_uses_narration_voice(self) -> None:
        cfg = Config()
        self.assertEqual(audio_engine.voice_for(cfg, "narrator"), cfg.audio.voice)

    def test_named_character_uses_their_configured_voice(self) -> None:
        cfg = Config()
        cfg.audio.character_voices = {"Birbal": "Charon"}
        self.assertEqual(audio_engine.voice_for(cfg, "Birbal"), "Charon")

    def test_unconfigured_character_uses_dialogue_voice(self) -> None:
        cfg = Config()
        self.assertEqual(audio_engine.voice_for(cfg, "Akbar"), cfg.audio.dialogue_voice)


class DialogueFallbackTests(unittest.TestCase):
    def test_rejected_dialogue_voice_falls_back_to_narrator(self) -> None:
        cfg = Config()
        line = Line(speaker="Akbar", hindi="हाँ", hinglish="haan")
        narrator_result = SceneSpeech([], Path("x.wav"), "gemini:m", cfg.audio.voice)

        with mock.patch.object(
            audio_engine,
            "_synthesize",
            side_effect=[RuntimeError("400 INVALID_ARGUMENT: unknown voice"), narrator_result],
        ) as synth:
            spoken = audio_engine._speak_line(cfg, line, Path("/tmp/unused_stem"))

        self.assertIs(spoken, narrator_result)
        self.assertEqual(synth.call_args_list[0].args[3], cfg.audio.dialogue_voice)
        self.assertEqual(synth.call_args_list[1].args[3], cfg.audio.voice)

    def test_narrator_failure_is_not_retried_with_another_voice(self) -> None:
        cfg = Config()
        line = Line(speaker="narrator", hindi="हाँ", hinglish="haan")
        with mock.patch.object(
            audio_engine, "_synthesize", side_effect=RuntimeError("503")
        ) as synth:
            with self.assertRaises(RuntimeError):
                audio_engine._speak_line(cfg, line, Path("/tmp/unused_stem"))
        self.assertEqual(synth.call_count, 1)


class SceneAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _fake_synthesize(self, cfg, text, dest_stem, voice=None):
        dest = dest_stem.with_suffix(".wav")
        audio_engine._write_wav(dest, SILENCE_HALF_SECOND, 24000)
        return SceneSpeech([], dest, "gemini:test", voice or cfg.audio.voice)

    def test_scene_length_matches_sped_up_lines_plus_gap(self) -> None:
        cfg = Config()
        cfg.audio.tempo = 1.10
        cfg.audio.line_gap_s = 0.12
        scene = _scene([
            Line(speaker="narrator", hindi="एक दो तीन", hinglish="ek do teen"),
            Line(speaker="Birbal", hindi="चार पाँच", hinglish="char paanch"),
        ])

        with mock.patch.object(audio_engine, "_synthesize", side_effect=self._fake_synthesize):
            wav, speech, words = audio_engine._speak_scene(cfg, scene, self.dir)

        expected = 2 * (0.5 / 1.10) + 0.12
        self.assertAlmostEqual(speech, expected, delta=0.05)
        self.assertAlmostEqual(probe_duration(wav), speech, delta=0.01)

    def test_word_timings_cover_every_word_in_order(self) -> None:
        cfg = Config()
        scene = _scene([
            Line(speaker="narrator", hindi="एक दो तीन", hinglish="ek do teen"),
            Line(speaker="Birbal", hindi="चार पाँच", hinglish="char paanch"),
        ])

        with mock.patch.object(audio_engine, "_synthesize", side_effect=self._fake_synthesize):
            _, speech, words = audio_engine._speak_scene(cfg, scene, self.dir)

        self.assertEqual([w.word for w in words], ["ek", "do", "teen", "char", "paanch"])
        starts = [w.start for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertLessEqual(max(w.end for w in words), speech + 1e-6)

    def test_second_line_starts_after_the_first_plus_gap(self) -> None:
        cfg = Config()
        cfg.audio.tempo = 1.0
        cfg.audio.line_gap_s = 0.2
        scene = _scene([
            Line(speaker="narrator", hindi="एक", hinglish="ek"),
            Line(speaker="Birbal", hindi="दो", hinglish="do"),
        ])

        with mock.patch.object(audio_engine, "_synthesize", side_effect=self._fake_synthesize):
            _, _, words = audio_engine._speak_scene(cfg, scene, self.dir)

        self.assertAlmostEqual(words[1].start, 0.5 + 0.2, delta=0.02)


class ScriptLineTests(unittest.TestCase):
    def test_scene_text_is_the_join_of_its_lines(self) -> None:
        scene = Scene(
            scene_number=1, beat="hook", hindi_narration="x", hinglish_caption="y", image_prompt="z",
            lines=[Line(hindi="एक", hinglish="ek"), Line(speaker="Birbal", hindi="दो", hinglish="do")],
        )
        synced = script_engine._sync_scene(scene)
        self.assertEqual(synced.hindi_narration, "एक दो")
        self.assertEqual(synced.hinglish_caption, "ek do")

    def test_scene_without_lines_becomes_one_narrator_line(self) -> None:
        scene = Scene(
            scene_number=1, beat="hook", hindi_narration="एक दो", hinglish_caption="ek do", image_prompt="z",
        )
        synced = script_engine._sync_scene(scene)
        self.assertEqual(len(synced.lines), 1)
        self.assertEqual(synced.lines[0].speaker, "narrator")

    def test_mismatched_line_is_repaired_so_parity_holds(self) -> None:
        scene = _scene([
            Line(speaker="narrator", hindi="एक दो तीन", hinglish="ek do"),
            Line(speaker="Birbal", hindi="चार", hinglish="char"),
        ])
        self.assertFalse(scene.parity_ok())

        repaired = script_engine._repair_lines(scene)

        self.assertTrue(repaired.parity_ok())
        self.assertEqual(repaired.lines[1].hinglish, "char")  # already correct, left alone


if __name__ == "__main__":
    unittest.main()
