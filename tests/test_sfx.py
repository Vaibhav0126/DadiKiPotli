"""Sound-effect cue placement. No ffmpeg needed.

    .venv/bin/python -m unittest tests.test_sfx
"""

from __future__ import annotations

import unittest

from src.models import Character, Scene, SceneAudio, Story, Timeline
from src.sfx import cues


def _timeline(beats: list[str]) -> Timeline:
    scenes = [
        Scene(
            scene_number=i + 1,
            beat=beat,
            hindi_narration="शब्द",
            hinglish_caption="shabd",
            image_prompt="x",
        )
        for i, beat in enumerate(beats)
    ]
    story = Story(
        slug="t",
        idea="t",
        title_hindi="t",
        title_hinglish="t",
        characters=[Character(name="Birbal", visual_description="x")],
        scenes=scenes,
        moral_hinglish="t",
        )
    audio = [
        SceneAudio(
            scene_number=i + 1,
            audio_path="x.wav",
            speech_duration=4.0,
            duration=4.25,
            start=i * 4.25,
        )
        for i in range(len(beats))
    ]
    return Timeline(story=story, audio=audio, visuals=[], total_duration=4.25 * len(beats))


class CueTests(unittest.TestCase):
    def test_whoosh_on_every_cut_but_not_the_first_scene(self) -> None:
        names = [name for _, name, _ in cues(_timeline(["hook", "setup", "conflict"]))]
        self.assertEqual(names.count("whoosh"), 2)

    def test_dhol_lands_once_even_if_two_scenes_are_twist(self) -> None:
        timeline = _timeline(["hook", "twist", "twist", "moral"])
        dhols = [start for start, name, _ in cues(timeline) if name == "dhol"]
        self.assertEqual(dhols, [timeline.audio[1].start])

    def test_no_dhol_without_a_twist(self) -> None:
        names = [name for _, name, _ in cues(_timeline(["hook", "setup", "moral"]))]
        self.assertNotIn("dhol", names)


if __name__ == "__main__":
    unittest.main()
