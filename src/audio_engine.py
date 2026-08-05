"""Module 2: narration audio and word-level caption timing.

Synthesis happens per scene rather than once for the whole story. That makes
each scene's duration an exact measured fact instead of something inferred by
splitting a single track, and it lets the composer lay out the timeline
without any alignment guesswork.

Gemini TTS returns raw PCM (no word-boundary stream), so caption timings are
spread evenly across the measured speech duration. That is close enough for
karaoke grouping; the important invariant is still Module 1's Hindi/Hinglish
token parity.
"""

from __future__ import annotations

import base64
import logging
import re
import time
import wave
from pathlib import Path

from google import genai
from google.genai import types

from .config import Config
from .ffmpeg_util import probe_duration
from .models import SceneAudio, Story, WordTiming, tokenize

log = logging.getLogger(__name__)

_RATE_IN_MIME = re.compile(r"rate=(\d+)", re.IGNORECASE)

# Remember which TTS model worked this process; same pattern as script_engine.
_active_tts_model: str | None = None


class Boundary:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


def _client(cfg: Config) -> genai.Client:
    return genai.Client(api_key=cfg.gemini_api_key)


def _is_missing_model(exc: Exception) -> bool:
    text = str(exc)
    return "NOT_FOUND" in text or "404" in text or "not available" in text


def _pcm_bytes(data: bytes | str) -> bytes:
    if isinstance(data, str):
        return base64.b64decode(data)
    return data


def _sample_rate(mime_type: str | None) -> int:
    if mime_type:
        match = _RATE_IN_MIME.search(mime_type)
        if match:
            return int(match.group(1))
    return 24000


def _write_wav(dest: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def _tts_contents(cfg: Config, text: str) -> str:
    """Wrap narration in a short director's note so Gemini sounds storyteller-like.

    The quoted block is the only spoken content; the lead-in steers delivery.
    """
    style = (cfg.audio.style_prompt or "").strip()
    if not style:
        return text
    return f'{style}\n"{text}"'


def _synthesize(cfg: Config, text: str, dest: Path, attempts: int = 3) -> list[Boundary]:
    """Call Gemini TTS and write a mono WAV. Returns no word boundaries."""
    global _active_tts_model

    candidates = [_active_tts_model] if _active_tts_model else list(cfg.audio.models)
    last_error: Exception | None = None

    speech_config = types.SpeechConfig(
        language_code=cfg.audio.language_code or None,
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=cfg.audio.voice,
            )
        ),
    )
    contents = _tts_contents(cfg, text)

    for model in candidates:
        for attempt in range(attempts):
            try:
                # Keep a strong reference: google-genai closes its httpx client
                # on GC, and a one-liner Client() call can die mid-request.
                client = _client(cfg)
                resp = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=speech_config,
                    ),
                )
                part = resp.candidates[0].content.parts[0]
                inline = part.inline_data
                if inline is None or not inline.data:
                    raise RuntimeError("Gemini TTS returned no audio data")
                pcm = _pcm_bytes(inline.data)
                if not pcm:
                    raise RuntimeError("Gemini TTS returned empty PCM")
                _write_wav(dest, pcm, _sample_rate(inline.mime_type))
                if _active_tts_model != model:
                    log.info("using Gemini TTS model: %s (%s)", model, cfg.audio.voice)
                    _active_tts_model = model
                return []
            except Exception as exc:  # noqa: BLE001 - retried / model-fallback
                last_error = exc
                if _is_missing_model(exc):
                    log.debug("TTS model %s unavailable, trying next", model)
                    break
                wait = 2**attempt
                log.warning(
                    "Gemini TTS attempt %d on %s failed (%s), retrying in %ss",
                    attempt + 1,
                    model,
                    exc,
                    wait,
                )
                time.sleep(wait)

    raise RuntimeError(
        f"Gemini TTS failed after trying {', '.join(cfg.audio.models)}: {last_error}. "
        "Check GEMINI_API_KEY and audio.models / audio.voice in config.yaml."
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
    both resolve correctly. With Gemini TTS, boundaries is usually empty and
    this falls through to even spacing across the measured duration.
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


def _build(cfg: Config, story: Story, out_dir: Path) -> list[SceneAudio]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[SceneAudio] = []
    cursor = 0.0

    for scene in story.scenes:
        dest = out_dir / f"scene_{scene.scene_number:02d}.wav"
        boundaries = _synthesize(cfg, scene.hindi_narration, dest)
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
    return _build(cfg, story, out_dir)


def total_duration(scenes: list[SceneAudio]) -> float:
    return round(sum(s.duration for s in scenes), 3)
