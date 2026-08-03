"""Shared data model. Every module reads and writes these types."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Beat = Literal["hook", "setup", "conflict", "twist", "moral"]

# Devanagari danda and double danda, plus the usual Latin punctuation that
# Gemini sprinkles into Hinglish. Stripped before comparing word counts.
_PUNCT = re.compile(r"[।॥,.!?;:\"'“”‘’()\-—…]")


def tokenize(text: str) -> list[str]:
    """Whitespace tokens with punctuation stripped.

    Used for the Devanagari/Hinglish parity check and for caption grouping,
    so both scripts must go through this same function.
    """
    return [t for t in _PUNCT.sub(" ", text).split() if t]


class Character(BaseModel):
    """Described once, then injected verbatim into every scene prompt.

    Prompt-level consistency is weak on its own, which is why visual_engine
    also locks the seed and conditions on a reference sheet.
    """

    name: str
    visual_description: str


class Scene(BaseModel):
    scene_number: int
    beat: Beat
    hindi_narration: str
    hinglish_caption: str
    image_prompt: str
    characters_present: list[str] = Field(default_factory=list)
    # Gemini's own 1-5 read on how much the scene would gain from motion.
    # motion_engine animates the top N and leaves the rest on Ken Burns.
    visual_impact: int = 3
    is_hero: bool = False

    def parity_ok(self) -> bool:
        """Whether caption tokens line up 1:1 with narration tokens.

        Caption timings come from edge-tts word boundaries measured against
        the Devanagari, so index i of each must refer to the same word.
        """
        return len(tokenize(self.hindi_narration)) == len(tokenize(self.hinglish_caption))


class StoryDraft(BaseModel):
    """Exactly what Gemini is asked to return."""

    title_hindi: str
    title_hinglish: str
    characters: list[Character]
    scenes: list[Scene]
    moral_hinglish: str


class Story(StoryDraft):
    slug: str
    idea: str

    @classmethod
    def from_draft(cls, draft: StoryDraft, slug: str, idea: str) -> Story:
        return cls(**draft.model_dump(), slug=slug, idea=idea)


class WordTiming(BaseModel):
    """One caption word, positioned on the final timeline."""

    word: str  # Hinglish, already display-cased
    start: float
    end: float


class SceneAudio(BaseModel):
    scene_number: int
    audio_path: str
    speech_duration: float  # narration only
    duration: float  # narration + trailing gap; this is the scene's slot length
    start: float  # absolute offset in the finished video
    words: list[WordTiming] = Field(default_factory=list)


class SceneVisual(BaseModel):
    scene_number: int
    image_path: str
    clip_path: str | None = None  # set when the scene was animated

    @property
    def animated(self) -> bool:
        return self.clip_path is not None


class Timeline(BaseModel):
    """The fully-resolved plan handed to the composer."""

    story: Story
    audio: list[SceneAudio]
    visuals: list[SceneVisual]
    total_duration: float

    def visual_for(self, scene_number: int) -> SceneVisual:
        return next(v for v in self.visuals if v.scene_number == scene_number)


def write_json(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def read_json[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    return model_cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


def slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "story"
