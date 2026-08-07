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

from .config import ROOT, Config
from .gemini_util import generate_with_fallback
from .models import Scene, Story, StoryDraft, slugify, tokenize
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
    return draft


def _repair_scene(cfg: Config, scene: Scene) -> Scene:
    """Ask for a fresh romanisation of one scene whose word counts disagreed."""
    hi_tokens = tokenize(scene.hindi_narration)
    resp = _call(
        cfg,
        contents=(
            "Romanise this Hindi sentence into Hinglish, word for word.\n\n"
            f"Hindi: {scene.hindi_narration}\n\n"
            f"It has exactly {len(hi_tokens)} words: {' | '.join(hi_tokens)}\n\n"
            f"Return exactly {len(hi_tokens)} space-separated Hinglish words in the same "
            "order, and nothing else. Do not translate to English. Spell it the way "
            "Indians type casually (kya, nahi, hai, raja)."
        ),
        config=types.GenerateContentConfig(temperature=0.2),
    )
    candidate = (resp.text or "").strip().replace("\n", " ")
    patched = scene.model_copy(update={"hinglish_caption": candidate})
    if patched.parity_ok():
        return patched

    # Last resort: a character mapper that is ugly but structurally exact.
    log.warning("scene %d: falling back to rule-based romanisation", scene.scene_number)
    return scene.model_copy(
        update={"hinglish_caption": fallback_romanize(scene.hindi_narration)}
    )


def _enforce_parity(cfg: Config, draft: StoryDraft) -> StoryDraft:
    fixed = []
    for scene in draft.scenes:
        if scene.parity_ok():
            fixed.append(scene)
            continue
        log.info(
            "scene %d: caption/narration word counts differ (%d vs %d), repairing",
            scene.scene_number,
            len(tokenize(scene.hindi_narration)),
            len(tokenize(scene.hinglish_caption)),
        )
        fixed.append(_repair_scene(cfg, scene))
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
        draft = _generate_draft(cfg, idea, nudge)
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
    draft = _generate_draft(cfg, story.idea, nudge)
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
