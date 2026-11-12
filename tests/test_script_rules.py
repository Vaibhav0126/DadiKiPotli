"""Opener ban, ending rules, and regeneration of scripts that break them.

    .venv/bin/python -m unittest tests.test_script_rules
"""

from __future__ import annotations

import unittest
from unittest import mock

from src import script_engine
from src.config import Config
from src.models import Character, Scene, StoryDraft


def _scene(number: int, hindi: str, hinglish: str, beat: str = "setup") -> Scene:
    return Scene(
        scene_number=number,
        beat=beat,
        hindi_narration=hindi,
        hinglish_caption=hinglish,
        image_prompt="a test image",
        characters_present=["Birbal"],
    )


def _draft(first_hindi: str, first_hinglish: str, last_hindi: str, last_hinglish: str) -> StoryDraft:
    scenes = [
        _scene(1, first_hindi, first_hinglish, beat="hook"),
        _scene(2, "बीरबल ने कहा", "Birbal ne kaha"),
        _scene(3, last_hindi, last_hinglish, beat="moral"),
    ]
    return StoryDraft(
        title_hindi="शीर्षक",
        title_hinglish="Title",
        characters=[Character(name="Birbal", visual_description="a wise minister")],
        scenes=scenes,
        moral_hinglish="Sachai jeet-ti hai.",
    )


GOOD_FIRST = ("राजा ने कहा, कल सुबह तक खजाना लौटाओ!", "Raja ne kaha, kal subah tak khazana lautao!")
GOOD_LAST = ("क्या आपको लगता है कि बीरबल जीतेगा? कमेंट में लिखिए।", "Kya aapko lagta hai Birbal jeetega? Comment mein likhiye.")
OPENER_FIRST = ("क्या आप जानते हैं, राजा ने क्या किया?", "Kya aap jaante hain, raja ne kya kiya?")


class RuleTests(unittest.TestCase):
    def test_each_banned_opener_is_flagged_in_scene_one(self) -> None:
        for opener in script_engine.BANNED_OPENERS:
            draft = _draft(f"{opener}, राजा ने कहा।", f"{opener}, raja ne kaha.", *GOOD_LAST)
            problems = script_engine.rule_problems(draft, "Akbar asks Birbal for gold")
            self.assertTrue(any("banned opener" in p for p in problems), opener)

    def test_opener_in_a_later_scene_is_not_flagged(self) -> None:
        draft = _draft(*GOOD_FIRST, *GOOD_LAST)
        draft.scenes[1] = _scene(2, "क्या आप जानते हैं", "kya aap jaante hain")

        problems = script_engine.rule_problems(draft, "Akbar asks Birbal for gold")

        self.assertEqual(problems, [])

    def test_regular_story_must_end_with_a_question(self) -> None:
        draft = _draft(*GOOD_FIRST, "सच की हमेशा जीत होती है।", "Sach ki hamesha jeet hoti hai.")

        problems = script_engine.rule_problems(draft, "Akbar asks Birbal for gold")

        self.assertTrue(any("question" in p for p in problems))

    def test_vikram_betaal_must_ask_viewer_to_comment(self) -> None:
        betaal_idea = "Betaal tells Vikram of a woman with three husbands."
        no_comment = _draft(*GOOD_FIRST, "आप बताइए?", "Aap bataiye?")
        with_comment = _draft(*GOOD_FIRST, *GOOD_LAST)

        self.assertTrue(script_engine.rule_problems(no_comment, betaal_idea))
        self.assertEqual(script_engine.rule_problems(with_comment, betaal_idea), [])

    def test_clean_story_passes(self) -> None:
        draft = _draft(*GOOD_FIRST, *GOOD_LAST)
        self.assertEqual(script_engine.rule_problems(draft, "Akbar asks Birbal for gold"), [])


class RegenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.cfg.story.max_script_retries = 2

    def test_bad_opener_triggers_regeneration_with_a_rule_note(self) -> None:
        bad = _draft(*OPENER_FIRST, *GOOD_LAST)
        good = _draft(*GOOD_FIRST, *GOOD_LAST)
        with mock.patch.object(script_engine, "_generate_draft", side_effect=[bad, good]) as gen:
            draft = script_engine._checked_draft(self.cfg, "Akbar asks Birbal for gold")

        self.assertIs(draft, good)
        self.assertEqual(gen.call_count, 2)
        second_nudge = gen.call_args_list[1].args[2]
        self.assertIn("banned opener", second_nudge)

    def test_gives_up_with_an_error_instead_of_posting_a_broken_script(self) -> None:
        bad = _draft(*OPENER_FIRST, *GOOD_LAST)
        with mock.patch.object(script_engine, "_generate_draft", return_value=bad) as gen:
            with self.assertRaisesRegex(RuntimeError, "kept breaking the rules"):
                script_engine._checked_draft(self.cfg, "Akbar asks Birbal for gold")

        self.assertEqual(gen.call_count, self.cfg.story.max_script_retries + 1)


if __name__ == "__main__":
    unittest.main()
