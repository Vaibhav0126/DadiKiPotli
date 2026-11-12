"""Idea rotation, classic tracking and the banned-premise filter. No API calls.

    .venv/bin/python -m unittest tests.test_ideas
"""

from __future__ import annotations

import random
import unittest

from src.characters import CHARACTER_NAMES, detect_character
from src.config import ROOT
from src.ideas import (
    PREMISE_TYPES,
    Classic,
    Entry,
    Plan,
    Scores,
    choose_plan,
    idea_problem,
    load_classics,
    parse_entries,
    parse_entry,
)

CLASSICS = [
    Classic("Akbar-Birbal", "trick", "Classic A about a crow count."),
    Classic("Tenali Raman", "rescue", "Classic B about a ghost."),
    Classic("Vikram-Betaal", "riddle", "Classic C about three husbands."),
]


class ParseTests(unittest.TestCase):
    def test_prefixed_line_round_trips_through_plan_record(self) -> None:
        plan = Plan("new", "Tenali Raman", "comeuppance")
        line = plan.record("Tenali makes a boaster pay for his own boast.")

        entry = parse_entry(line)

        self.assertEqual(entry.kind, "new")
        self.assertEqual(entry.character, "Tenali Raman")
        self.assertEqual(entry.premise, "comeuppance")
        self.assertEqual(entry.idea, "Tenali makes a boaster pay for his own boast.")

    def test_legacy_line_without_prefix_is_new_with_detected_family(self) -> None:
        entry = parse_entry("Birbal must settle a dispute where a tailor sues a child.")

        self.assertEqual(entry.kind, "new")
        self.assertEqual(entry.character, "Akbar-Birbal")
        self.assertIsNone(entry.premise)

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        self.assertIsNone(parse_entry("# header"))
        self.assertIsNone(parse_entry("   "))

    def test_real_classics_file_is_valid(self) -> None:
        classics = load_classics(ROOT / "prompts" / "classics.txt")

        self.assertGreaterEqual(len(classics), 4)
        for classic in classics:
            self.assertIn(classic.character, CHARACTER_NAMES)
            self.assertIn(classic.premise, PREMISE_TYPES)

    def test_detect_character_handles_hindi_and_english(self) -> None:
        self.assertEqual(detect_character("बीरबल ने कहा"), "Akbar-Birbal")
        self.assertEqual(detect_character("Betaal asks Vikram"), "Vikram-Betaal")
        self.assertEqual(detect_character("a merchant in a market"), None)


class RotationTests(unittest.TestCase):
    def test_never_repeats_character_or_premise_from_previous_post(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            last = Entry("earlier idea", "new", rng.choice(CHARACTER_NAMES), rng.choice(PREMISE_TYPES))
            plan = choose_plan([last], [], rng)
            self.assertNotEqual(plan.character, last.character)
            self.assertNotEqual(plan.premise, last.premise)

    def test_each_classic_is_used_once(self) -> None:
        rng = random.Random(3)
        history: list[Entry] = []
        seen: list[str] = []
        for _ in range(40):
            plan = choose_plan(history, CLASSICS, rng)
            if plan.kind == "classic":
                self.assertNotIn(plan.idea, seen)
                seen.append(plan.idea)
                history.append(Entry(plan.idea, "classic", plan.character, plan.premise))
            else:
                history.append(Entry("new idea %d" % len(history), "new", plan.character, plan.premise))

        self.assertEqual(sorted(seen), sorted(c.idea for c in CLASSICS))

    def test_once_classics_are_exhausted_only_new_stories_are_planned(self) -> None:
        history = [Entry(c.idea, "classic", c.character, c.premise) for c in CLASSICS]
        rng = random.Random(1)
        for _ in range(50):
            self.assertEqual(choose_plan(history, CLASSICS, rng).kind, "new")

    def test_classic_and_new_are_split_roughly_evenly(self) -> None:
        rng = random.Random(11)
        kinds = [choose_plan([], CLASSICS, rng).kind for _ in range(2000)]
        share = kinds.count("classic") / len(kinds)
        self.assertGreater(share, 0.42)
        self.assertLess(share, 0.58)

    def test_weights_can_shift_the_character_choice(self) -> None:
        rng = random.Random(5)
        scores = Scores(character={"Tenali Raman": 8.0})
        picks = [choose_plan([], [], rng, scores).character for _ in range(2000)]
        self.assertGreater(picks.count("Tenali Raman"), picks.count("Akbar-Birbal"))

    def test_plan_is_always_valid_after_any_single_entry(self) -> None:
        rng = random.Random(2)
        for name in CHARACTER_NAMES:
            for premise in PREMISE_TYPES:
                plan = choose_plan([Entry("x", "new", name, premise)], [], rng)
                self.assertIn(plan.character, CHARACTER_NAMES)
                self.assertIn(plan.premise, PREMISE_TYPES)


class BanTests(unittest.TestCase):
    def test_rejects_abstract_lawsuit_and_treason_premises(self) -> None:
        banned = [
            "Birbal must settle a dispute where a weaver sues a musician.",
            "Tenali Raman settles a lawsuit where a thief sues a homeowner.",
            "Betaal asks Vikram if a king who saved an enemy is a traitor.",
            "Betaal asks Vikram whether a healer who withheld a cure sinned.",
            "Birbal must judge a sculptor who sued a woodcutter.",
        ]
        for idea in banned:
            self.assertIsNotNone(idea_problem(idea), idea)

    def test_accepts_concrete_stakes_and_funny_premises(self) -> None:
        good = [
            "Birbal has one hour to escape the palace before the emperor's guards find his stolen ring.",
            "Tenali Raman tricks a greedy merchant into eating his own rotten mangoes.",
        ]
        for idea in good:
            self.assertIsNone(idea_problem(idea), idea)

    def test_rejects_overlong_ideas(self) -> None:
        self.assertIsNotNone(idea_problem(" ".join(["word"] * 40)))


class ParseEntriesTests(unittest.TestCase):
    def test_mixed_file_keeps_order_and_kinds(self) -> None:
        lines = [
            "# header",
            "[classic|Tenali Raman|trick] Classic one.",
            "Akbar tasks Birbal to bring a flower.",
        ]
        entries = parse_entries(lines)

        self.assertEqual([e.kind for e in entries], ["classic", "new"])
        self.assertEqual(entries[0].character, "Tenali Raman")


if __name__ == "__main__":
    unittest.main()
