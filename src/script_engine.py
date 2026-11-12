"""Module 1: idea -> structured story.

Gemini returns the narration in Devanagari (for TTS) and a word-for-word
Hinglish romanisation (for captions) in one structured response. The parity
between those two is load-bearing for caption timing, so it is validated here
and repaired per-scene rather than trusted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google import genai
from google.genai import types

from .characters import detect_character
from .config import ROOT, Config
from .gemini_util import generate_with_fallback
from .models import Line, Scene, Story, StoryDraft, slugify, tokenize
from .romanize import fallback_romanize

log = logging.getLogger(__name__)

# Resolved once per process, then reused.
_active_model: str | None = None


def _call(cfg: Config, **kwargs) -> object:
    """Invoke Gemini, falling back through the configured model list.

    Google retires model IDs on a schedule that outpaces most codebases, and a
    retired ID returns 404 rather than redirecting, so pinning a single name
    turns into a hard failure months later. Capacity 503s also hop to the next
    model after a short backoff.
    """
    global _active_model

    def _one(model: str) -> object:
        client = _client(cfg)
        return client.models.generate_content(model=model, **kwargs)

    resp, model = generate_with_fallback(
        models=list(cfg.story.models),
        call=_one,
        active_model=_active_model,
        label="Gemini story",
    )
    if _active_model != model:
        log.info("using Gemini model: %s", model)
        _active_model = model
    return resp


def _system_prompt(cfg: Config) -> str:
    path = ROOT / cfg.story.prompt_file
    tmpl = path.read_text(encoding="utf-8")
    return tmpl.replace("{scene_count}", str(cfg.story.scene_count)).replace(
        "{max_words}", str(cfg.story.max_hindi_words)
    )


def _client(cfg: Config) -> genai.Client:
    return genai.Client(api_key=cfg.gemini_api_key)


def _generate_draft(cfg: Config, idea: str, nudge: str = "") -> StoryDraft:
    prompt = f"Story idea: {idea}"
    if nudge:
        prompt += f"\n\n{nudge}"

    resp = _call(
        cfg,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_system_prompt(cfg),
            response_mime_type="application/json",
            response_schema=StoryDraft,
            temperature=1.0,
        ),
    )
    draft = resp.parsed
    if draft is None:
        raise RuntimeError(f"Gemini returned no parsable story. Raw text:\n{resp.text}")
    return draft.model_copy(update={"scenes": [_sync_scene(s) for s in draft.scenes]})


# Openers that made the first seconds of every video look the same. Checked in
# both scripts, case-insensitively, against scene 1 only.
BANNED_OPENERS = (
    "क्या आप जानते हैं",
    "क्या आपको पता है",
    "एक बार की बात है",
    "kya aap jaante hain",
    "kya aapko pata hai",
    "ek baar ki baat hai",
)


def _is_betaal(draft: StoryDraft, idea: str) -> bool:
    if detect_character(idea) == "Vikram-Betaal":
        return True
    return any(detect_character(c.name) == "Vikram-Betaal" for c in draft.characters)


def rule_problems(draft: StoryDraft, idea: str) -> list[str]:
    """Hard rules from the prompt that regenerating can fix. Empty when all pass."""
    problems: list[str] = []
    if not draft.scenes:
        return ["the story has no scenes"]

    first = draft.scenes[0]
    scene_text = f"{first.hindi_narration} {first.hinglish_caption}".casefold()
    for opener in BANNED_OPENERS:
        if opener.casefold() in scene_text:
            problems.append(f"scene 1 uses the banned opener '{opener}'")

    last = draft.scenes[-1]
    if _is_betaal(draft, idea):
        if "comment" not in last.hinglish_caption.casefold() and "कमेंट" not in last.hindi_narration:
            problems.append("a Vikram-Betaal story must end by asking the viewer to comment")
    elif "?" not in last.hindi_narration:
        problems.append("the story must end with a question to the viewer")
    return problems


def _checked_draft(cfg: Config, idea: str, nudge: str = "") -> StoryDraft:
    """Generate a draft that passes the hard rules, regenerating when it does not.

    After the retries are spent this raises rather than returning a script that
    breaks the rules. The daily run then skips the day, which beats posting it.
    """
    rules_note = ""
    problems: list[str] = []
    for attempt in range(cfg.story.max_script_retries + 1):
        draft = _generate_draft(cfg, idea, nudge + rules_note)
        problems = rule_problems(draft, idea)
        if not problems:
            return draft
        log.warning("draft %d broke script rules: %s", attempt + 1, "; ".join(problems))
        rules_note = (
            "\n\nYour last draft broke these rules. Fix every one: "
            + "; ".join(problems)
            + "."
        )
    raise RuntimeError(f"script kept breaking the rules: {'; '.join(problems)}")


def _sync_scene(scene: Scene) -> Scene:
    """Make the scene-level text the join of its lines, so the two never disagree."""
    lines = scene.speech_lines()
    return scene.model_copy(
        update={
            "lines": lines,
            "hindi_narration": " ".join(line.hindi for line in lines),
            "hinglish_caption": " ".join(line.hinglish for line in lines),
        }
    )


def _repair_lines(scene: Scene) -> Scene:
    """Rebuild the Hinglish of any line whose word count disagrees with its Hindi.

    Each bad line is romanised by the rule-based mapper, which is ugly but keeps
    the count exact. Line-level repair is the cheapest reliable fix: it leaves
    the rest of the scene untouched and needs no extra Gemini call.
    """
    fixed: list[Line] = []
    for line in scene.speech_lines():
        if line.parity_ok():
            fixed.append(line)
            continue
        log.info("scene %d: line word counts differ, romanising that line by rule", scene.scene_number)
        fixed.append(line.model_copy(update={"hinglish": fallback_romanize(line.hindi)}))
    return _sync_scene(scene.model_copy(update={"lines": fixed}))


def _enforce_parity(cfg: Config, draft: StoryDraft) -> StoryDraft:
    fixed = [scene if scene.parity_ok() else _repair_lines(scene) for scene in draft.scenes]
    return draft.model_copy(update={"scenes": fixed})


def _mark_hero_scenes(cfg: Config, draft: StoryDraft) -> StoryDraft:
    """Flag the highest-impact scenes for animation.

    The twist beat is always included regardless of how Gemini rated it, since
    it is the moment the whole video is built around.
    """
    n = cfg.motion.hero_scene_count if cfg.motion.enabled else 0
    ranked = sorted(
        draft.scenes,
        key=lambda s: (s.beat == "twist", s.visual_impact, -s.scene_number),
        reverse=True,
    )
    chosen = {s.scene_number for s in ranked[:n]}
    return draft.model_copy(
        update={
            "scenes": [
                s.model_copy(update={"is_hero": s.scene_number in chosen})
                for s in draft.scenes
            ]
        }
    )


def build_scene_image_prompt(cfg: Config, story: Story, scene: Scene) -> str:
    """Compose the full image prompt: house style, then action, then characters.

    Style goes first on purpose. Diffusion models weight leading tokens far
    more heavily, and trailing a long prompt it gets diluted to the point of
    being ignored — probing produced painterly, photographic output from a
    prompt that ended in "flat 2D vector cartoon illustration".
    """
    parts = [cfg.story.art_style.strip(), scene.image_prompt.rstrip(". ")]
    for name in scene.characters_present:
        char = next((c for c in story.characters if c.name == name), None)
        if char:
            parts.append(char.visual_description.rstrip(". "))
    parts.append(f"{cfg.story.aspect_label}, no text, no letters, no watermark")
    return ". ".join(p for p in parts if p)


def generate_story(cfg: Config, idea: str) -> Story:
    nudge = ""
    draft: StoryDraft | None = None

    for attempt in range(cfg.story.max_script_retries + 1):
        draft = _checked_draft(cfg, idea, nudge)
        words = sum(len(tokenize(s.hindi_narration)) for s in draft.scenes)
        log.info(
            "draft %d: %d scenes, %d Hindi words", attempt + 1, len(draft.scenes), words
        )
        if words <= cfg.story.max_hindi_words:
            break
        # Word count is the only proxy for duration we have before TTS runs.
        nudge = (
            f"Your previous attempt used {words} Hindi words, over the "
            f"{cfg.story.max_hindi_words} limit. Rewrite it tighter — cut adjectives "
            "and combine scenes rather than dropping the twist."
        )

    assert draft is not None
    draft = _enforce_parity(cfg, draft)
    draft = _mark_hero_scenes(cfg, draft)

    # Renumber defensively; downstream modules index scenes by this.
    draft = draft.model_copy(
        update={
            "scenes": [
                s.model_copy(update={"scene_number": i})
                for i, s in enumerate(draft.scenes, start=1)
            ]
        }
    )
    # Slug comes from the idea, not the title, so a resumed run lands in the
    # same work directory even when the model retitles the story.
    return Story.from_draft(draft, slug=slugify(idea), idea=idea)


def revise_for_duration(cfg: Config, story: Story, measured: float) -> Story:
    """Regenerate after TTS revealed the narration runs too long or too short."""
    target = (cfg.story.min_duration_s + cfg.story.max_duration_s) / 2
    direction = "shorter" if measured > cfg.story.max_duration_s else "longer"
    ratio = target / measured if measured else 1.0
    words = sum(len(tokenize(s.hindi_narration)) for s in story.scenes)
    lo, hi = cfg.story.min_duration_s, cfg.story.max_duration_s
    nudge = (
        f"A previous version of this story was narrated aloud and ran {measured:.1f} "
        f"seconds, which is outside the {lo:.0f}-{hi:.0f} second target. Write it "
        f"{direction}: aim for about {int(words * ratio)} Hindi words total. Keep the "
        "same beats and the same twist."
    )
    draft = _checked_draft(cfg, story.idea, nudge)
    draft = _enforce_parity(cfg, draft)
    draft = _mark_hero_scenes(cfg, draft)
    draft = draft.model_copy(
        update={
            "scenes": [
                s.model_copy(update={"scene_number": i})
                for i, s in enumerate(draft.scenes, start=1)
            ]
        }
    )
    return Story.from_draft(draft, slug=story.slug, idea=story.idea)
