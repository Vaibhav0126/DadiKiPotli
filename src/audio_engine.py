"""Module 2: narration audio and word-level caption timing.

Synthesis happens per scene rather than once for the whole story. That makes
each scene's duration an exact measured fact instead of something inferred by
splitting a single track, and it lets the composer lay out the timeline
without any alignment guesswork.

edge-tts reports word boundaries against the *Devanagari* text. Those timings
are transferred onto the Hinglish caption tokens here, via a character-level
alignment that tolerates the boundary stream not landing 1:1 on whitespace
tokens (which happens around punctuation, numerals and conjuncts).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import edge_tts

from .config import Config
from .ffmpeg_util import probe_duration
from .models import SceneAudio, Story, WordTiming, tokenize

log = logging.getLogger(__name__)

# edge-tts reports offsets in 100-nanosecond ticks.
TICKS_PER_SECOND = 10_000_000


class Boundary:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


async def _synthesize(
    cfg: Config, text: str, dest: Path, attempts: int = 3
) -> list[Boundary]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        audio = bytearray()
        boundaries: list[Boundary] = []
        try:
            comm = edge_tts.Communicate(
                text,
                voice=cfg.audio.voice,
                rate=cfg.audio.rate,
                pitch=cfg.audio.pitch,
                # edge-tts 7.x defaults to SentenceBoundary, which yields one
                # event for the whole scene and would force captions onto
                # evenly-spaced timings. Word granularity is still available,
                # it just has to be asked for.
                boundary="WordBoundary",
            )
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / TICKS_PER_SECOND
                    end = start + chunk["duration"] / TICKS_PER_SECOND
                    boundaries.append(Boundary(chunk["text"], start, end))
            if not audio:
                raise RuntimeError("edge-tts returned no audio")
            dest.write_bytes(bytes(audio))
            return boundaries
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_error = exc
            wait = 2**attempt
            log.warning("edge-tts attempt %d failed (%s), retrying in %ss", attempt + 1, exc, wait)
            await asyncio.sleep(wait)

    raise RuntimeError(
        f"edge-tts failed after {attempts} attempts: {last_error}. "
        "If this is a 403, upgrade with: pip install -U 'edge-tts>=7.2.7'"
    ) from last_error


def _align(
    boundaries: list[Boundary],
    source_tokens: list[str],
    target_tokens: list[str],
    fallback_duration: float,
) -> list[WordTiming]:
    """Transfer boundary timings onto target tokens via character alignment.

    Boundary text and source tokens are flattened into one character stream so
    that a boundary spanning two tokens, or two boundaries covering one token,
    both resolve correctly.
    """
    if not target_tokens:
        return []

    # Evenly spaced fallback when the service returned no boundary events.
    if not boundaries or not source_tokens:
        step = fallback_duration / len(target_tokens)
        return [
            WordTiming(word=w, start=i * step, end=(i + 1) * step)
            for i, w in enumerate(target_tokens)
        ]

    # Usual case: one boundary event per whitespace token. Map straight across
    # and keep the reported times exactly.
    if len(boundaries) == len(target_tokens):
        return [
            WordTiming(word=t, start=b.start, end=b.end)
            for t, b in zip(target_tokens, boundaries)
        ]

    # Character timeline: every character of the boundary stream gets an
    # interval, interpolated linearly inside its boundary.
    char_times: list[tuple[float, float]] = []
    for b in boundaries:
        chars = b.text.strip()
        if not chars:
            continue
        span = max(b.end - b.start, 1e-6) / len(chars)
        for i in range(len(chars)):
            char_times.append((b.start + i * span, b.start + (i + 1) * span))

    if not char_times:
        step = fallback_duration / len(target_tokens)
        return [
            WordTiming(word=w, start=i * step, end=(i + 1) * step)
            for i, w in enumerate(target_tokens)
        ]

    timings: list[WordTiming] = []
    cursor = 0
    total_chars = len(char_times)
    for i, target in enumerate(target_tokens):
        # Source and target token lists are the same length (parity is enforced
        # in Module 1), so index i refers to the same word in both scripts.
        src_len = len(source_tokens[i]) if i < len(source_tokens) else len(target)
        start_idx = min(cursor, total_chars - 1)
        end_idx = min(cursor + max(src_len, 1), total_chars) - 1
        end_idx = max(end_idx, start_idx)
        timings.append(
            WordTiming(
                word=target,
                start=char_times[start_idx][0],
                end=char_times[end_idx][1],
            )
        )
        cursor += max(src_len, 1)

    # Enforce monotonicity; interpolation can produce tiny overlaps.
    for i in range(1, len(timings)):
        if timings[i].start < timings[i - 1].end:
            timings[i].start = timings[i - 1].end
        if timings[i].end <= timings[i].start:
            timings[i].end = timings[i].start + 0.08
    return timings


async def _build(cfg: Config, story: Story, out_dir: Path) -> list[SceneAudio]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[SceneAudio] = []
    cursor = 0.0

    for scene in story.scenes:
        dest = out_dir / f"scene_{scene.scene_number:02d}.mp3"
        boundaries = await _synthesize(cfg, scene.hindi_narration, dest)
        speech = probe_duration(dest)

        words = _align(
            boundaries,
            tokenize(scene.hindi_narration),
            tokenize(scene.hinglish_caption),
            speech,
        )
        # Clamp to the real audio length, then shift onto the global timeline.
        for w in words:
            w.start = min(w.start, speech)
            w.end = min(max(w.end, w.start + 0.05), speech)
            w.start += cursor
            w.end += cursor

        slot = speech + cfg.audio.scene_gap_s
        scenes.append(
            SceneAudio(
                scene_number=scene.scene_number,
                audio_path=str(dest),
                speech_duration=speech,
                duration=slot,
                start=cursor,
                words=words,
            )
        )
        log.info("scene %d: %.2fs narration, %d words", scene.scene_number, speech, len(words))
        cursor += slot

    return scenes


def generate_audio(cfg: Config, story: Story, out_dir: Path) -> list[SceneAudio]:
    return asyncio.run(_build(cfg, story, out_dir))


def total_duration(scenes: list[SceneAudio]) -> float:
    return round(sum(s.duration for s in scenes), 3)
