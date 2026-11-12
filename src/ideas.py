"""Idea selection rules for the daily queue. Pure logic, no API calls.

- About half of posts retell a famous classic (prompts/classics.txt). Each
  classic is posted once.
- The character family and premise type never repeat from the previous post.
- Premises built on lawsuits, treason or "is it a sin?" debates are banned.
- Each posted idea is recorded in ideas_used.txt as
  `[kind|character|premise] idea`. Older lines have no prefix and are read as
  new stories, with the character inferred from their text.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from .characters import CHARACTER_NAMES, detect_character

KINDS = ("classic", "new")
PREMISE_TYPES = ("trick", "rescue", "test_of_wits", "comeuppance", "riddle")

# Chance of ignoring the performance weights and choosing uniformly.
EXPLORE = 0.25

MAX_IDEA_WORDS = 30

_PREFIX = re.compile(r"^\[(classic|new)\|([^|\]]*)\|([^|\]]*)\]\s*(.+)$")

# Patterns that produced the drift toward abstract "X sues Y" and "is it
# treason" premises. Matched case-insensitively against English ideas.
_BANNED = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bsu(es?|ed|ing)\b",
        r"\blawsuit\b",
        r"\bdispute\b",
        r"\bsettles?\b",
        r"\bjudg(e|es|ed|ing)\b",
        r"\btreason\b",
        r"\btraitor\b",
        r"\bvirtu(e|ous)\b",
        r"\bsin\b",
        r"\bsinned\b",
        r"\bguilty\b",
        r"\bverdict\b",
        r"\bmurderer\b",
        r"\bsacrilege\b",
        r"\breward or punishment\b",
        r"\basks? Vikram (if|whether)\b",
    )
]


@dataclass(frozen=True)
class Entry:
    """One line of ideas_used.txt, parsed."""

    idea: str
    kind: str = "new"
    character: str | None = None
    premise: str | None = None


@dataclass(frozen=True)
class Classic:
    character: str
    premise: str
    idea: str


@dataclass(frozen=True)
class Plan:
    """What the next post should be. `idea` is set only for classics."""

    kind: str
    character: str
    premise: str
    idea: str | None = None

    def record(self, idea: str) -> str:
        return f"[{self.kind}|{self.character}|{self.premise}] {idea}"


@dataclass
class Scores:
    """Per-option weights from past performance. Missing options weigh 1.0."""

    kind: dict[str, float] = field(default_factory=dict)
    character: dict[str, float] = field(default_factory=dict)
    premise: dict[str, float] = field(default_factory=dict)


def norm(text: str) -> str:
    return " ".join(text.casefold().split())


def parse_entry(line: str) -> Entry | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    match = _PREFIX.match(text)
    if match:
        kind, character, premise, idea = match.groups()
        return Entry(
            idea=idea.strip(),
            kind=kind,
            character=character.strip() or None,
            premise=premise.strip() or None,
        )
    return Entry(idea=text, kind="new", character=detect_character(text))


def parse_entries(lines: list[str]) -> list[Entry]:
    return [e for e in (parse_entry(line) for line in lines) if e is not None]


def load_classics(path: Path) -> list[Classic]:
    """Read `Character | premise | idea` rows, skipping comments and blanks."""
    classics: list[Classic] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = [p.strip() for p in text.split("|", 2)]
        if len(parts) != 3:
            continue
        character, premise, idea = parts
        if character in CHARACTER_NAMES and premise in PREMISE_TYPES:
            classics.append(Classic(character, premise, idea))
    return classics


def idea_problem(idea: str) -> str | None:
    """Why an invented idea is not allowed, or None if it passes."""
    for pattern in _BANNED:
        match = pattern.search(idea)
        if match:
            return f"banned pattern {match.group(0)!r}"
    if len(idea.split()) > MAX_IDEA_WORDS:
        return f"{len(idea.split())} words, limit is {MAX_IDEA_WORDS}"
    return None


def _pick(options: list[str], weights: dict[str, float], rng: random.Random) -> str:
    if len(options) == 1:
        return options[0]
    if rng.random() < EXPLORE:
        return rng.choice(options)
    w = [max(weights.get(o, 1.0), 0.05) for o in options]
    return rng.choices(options, weights=w, k=1)[0]


def choose_plan(
    history: list[Entry],
    classics: list[Classic],
    rng: random.Random,
    scores: Scores | None = None,
) -> Plan:
    """Pick kind, character and premise for the next post.

    Rotation is checked against the most recent entry only: the rule is about
    two days in a row. Constraints that would leave no options are relaxed
    rather than blocking the post.
    """
    scores = scores or Scores()
    last = history[-1] if history else None
    done = {norm(e.idea) for e in history}
    open_classics = [c for c in classics if norm(c.idea) not in done]

    # Equal weights give the 50/50 split between classics and new stories.
    kind_options = ["classic", "new"] if open_classics else ["new"]
    kind = _pick(kind_options, scores.kind, rng)

    if kind == "classic":
        pool = [c for c in open_classics if last is None or c.character != last.character]
        pool = pool or open_classics
        fresh = [c for c in pool if last is None or c.premise != last.premise]
        classic = rng.choice(fresh or pool)
        return Plan("classic", classic.character, classic.premise, classic.idea)

    characters = [c for c in CHARACTER_NAMES if last is None or c != last.character]
    premises = [p for p in PREMISE_TYPES if last is None or p != last.premise]
    return Plan(
        "new",
        _pick(characters or list(CHARACTER_NAMES), scores.character, rng),
        _pick(premises or list(PREMISE_TYPES), scores.premise, rng),
    )
