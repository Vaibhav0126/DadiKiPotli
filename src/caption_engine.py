"""Module 4a: Hinglish karaoke captions as an ASS subtitle file.

Words are shown in small groups, with the word currently being spoken picked
out in colour and scaled up slightly. That is done by emitting one Dialogue
event per word — each showing the whole group, with a different word
highlighted — rather than using ASS `\\k` karaoke tags, which can only colour
everything *up to* the current word and cannot highlight one word alone.

Captions are Latin script, so this needs no font shaping and works with any
libass build.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .models import SceneAudio, WordTiming

log = logging.getLogger(__name__)


def _timestamp(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _split_colour(value: str) -> tuple[str, str]:
    """Return (alpha, bbggrr) from an ASS colour literal."""
    raw = value.strip().removeprefix("&H").removeprefix("&h").rstrip("&")
    if len(raw) >= 8:
        return raw[:2], raw[2:8]
    return "00", raw.rjust(6, "0")


def style_colour(value: str) -> str:
    alpha, bgr = _split_colour(value)
    return f"&H{alpha}{bgr}"


def inline_colour(value: str) -> str:
    _, bgr = _split_colour(value)
    return f"&H{bgr}&"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def group_words(cfg: Config, words: list[WordTiming]) -> list[list[WordTiming]]:
    """Chunk one scene's words into readable on-screen groups.

    Groups also break on a long pause, so a caption never spans a beat of
    silence with stale text sitting on screen.
    """
    groups: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    for word in words:
        if current:
            too_many = len(current) >= cfg.captions.max_words_per_group
            too_long = word.end - current[0].start > cfg.captions.max_group_duration_s
            big_gap = word.start - current[-1].end > 0.6
            if too_many or too_long or big_gap:
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _header(cfg: Config) -> str:
    c = cfg.captions
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {cfg.video.width}",
            f"PlayResY: {cfg.video.height}",
            # 0 = balanced auto-wrap. WrapStyle 2 disables wrapping entirely,
            # which lets a long word group run off both edges of the frame.
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            (
                f"Style: Main,{c.font_name},{c.font_size},"
                f"{style_colour(c.primary_colour)},{style_colour(c.highlight_colour)},"
                f"{style_colour(c.outline_colour)},&H90000000,"
                f"-1,0,0,0,100,100,0,0,1,{c.outline},{c.shadow},5,80,80,60,1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text",
        ]
    )


def build_ass(cfg: Config, scenes: list[SceneAudio], dest: Path) -> Path:
    c = cfg.captions
    x = cfg.video.width // 2
    y = int(cfg.video.height * c.vertical_position)
    highlight = inline_colour(c.highlight_colour)

    lines = [_header(cfg)]
    events = 0

    for scene in scenes:
        for group in group_words(cfg, scene.words):
            rendered = [
                _escape(w.word.upper() if c.uppercase else w.word) for w in group
            ]
            for i, word in enumerate(group):
                # Each event shows the full group; only the active word is
                # recoloured and nudged larger, then {\r} restores the style.
                parts = [
                    f"{{\\c{highlight}\\fscx112\\fscy112}}{token}{{\\r}}"
                    if i == j
                    else token
                    for j, token in enumerate(rendered)
                ]
                text = f"{{\\an5\\pos({x},{y})}}" + " ".join(parts)
                lines.append(
                    f"Dialogue: 0,{_timestamp(word.start)},{_timestamp(word.end)},"
                    f"Main,,0,0,0,,{text}"
                )
                events += 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("captions: %d events -> %s", events, dest.name)
    return dest
