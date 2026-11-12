"""Module 2: narration audio and word-level caption timing.

Synthesis happens per scene rather than once for the whole story. That makes
each scene's duration an exact measured fact instead of something inferred by
splitting a single track, and it lets the composer lay out the timeline
without any alignment guesswork.

Gemini TTS is the only voice for scheduled runs (raw PCM, no word boundaries).
Caption timings are then spread evenly across the measured speech duration.

Capacity errors (429 / 5xx) are retried with exponential backoff. If Gemini
still fails, the scene raises: a skipped day beats a bad voice posted to
YouTube. edge-tts is never used silently. It is only reached through
`audio.provider: edge` (a deliberate config choice, used for weekly long-form)
or `--allow-edge` on a local run.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
import wave
from pathlib import Path

import edge_tts
from google import genai
from google.genai import types

from .config import Config
from .ffmpeg_util import probe_duration, run
from .gemini_util import is_capacity_error, is_missing_model
from .models import Line, Scene, SceneAudio, Story, WordTiming, tokenize

log = logging.getLogger(__name__)

_RATE_IN_MIME = re.compile(r"rate=(\d+)", re.IGNORECASE)

# edge-tts reports offsets in 100-nanosecond ticks.
TICKS_PER_SECOND = 10_000_000

# Gemini TTS retry policy: 5 tries, waiting 2s, 4s, 8s, 16s between them.
# The cap keeps any future change from sleeping absurdly long.
TTS_ATTEMPTS = 5
TTS_BACKOFF_BASE_S = 2.0
TTS_BACKOFF_CAP_S = 32.0

# Remember which TTS model worked this process; same pattern as script_engine.
_active_tts_model: str | None = None


class EmptyAudioError(RuntimeError):
    """Gemini answered without audio. Treated as transient, like a 503."""


def _backoff_s(attempt: int) -> float:
    return min(TTS_BACKOFF_BASE_S * 2**attempt, TTS_BACKOFF_CAP_S)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, EmptyAudioError) or is_capacity_error(exc)


class Boundary:
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


def _client(cfg: Config) -> genai.Client:
    return genai.Client(api_key=cfg.gemini_api_key)


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


def _request_pcm(cfg: Config, model: str, contents: str, voice: str) -> tuple[bytes, int]:
    """One Gemini TTS call. Returns (raw PCM, sample rate)."""
    speech_config = types.SpeechConfig(
        language_code=cfg.audio.language_code or None,
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name=voice,
            )
        ),
    )
    # Build a fresh client per call: google-genai closes its httpx client on
    # GC, and a one-liner Client() call can die mid-request.
    client = _client(cfg)
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=speech_config,
        ),
    )
    # Gemini sometimes answers with no candidates or no parts at all (seen in
    # practice). That is an empty answer, so it is retried, not a crash.
    candidates = resp.candidates or []
    content = candidates[0].content if candidates else None
    parts = content.parts if content and content.parts else []
    inline = parts[0].inline_data if parts else None
    if inline is None or not inline.data:
        raise EmptyAudioError(f"Gemini TTS ({model}) returned no audio data")
    pcm = _pcm_bytes(inline.data)
    if not pcm:
        raise EmptyAudioError(f"Gemini TTS ({model}) returned empty PCM")
    return pcm, _sample_rate(inline.mime_type)


def _synthesize_gemini(
    cfg: Config, text: str, dest: Path, voice: str, attempts: int = TTS_ATTEMPTS
) -> tuple[list[Boundary], str]:
    """Call Gemini TTS and write a mono WAV. Returns (no boundaries, model used).

    Transient failures (429, 5xx, empty audio) retry on the same model with
    exponential backoff, then move to the next configured TTS model. A missing
    model is skipped at once. Non-transient errors raise immediately. If every
    model is exhausted the error propagates, and the caller decides what happens
    next.
    """
    global _active_tts_model

    ordered: list[str] = []
    for model in [_active_tts_model, *cfg.audio.models]:
        if model and model not in ordered:
            ordered.append(model)

    contents = _tts_contents(cfg, text)
    last_error: Exception | None = None

    for model in ordered:
        for attempt in range(attempts):
            try:
                pcm, rate = _request_pcm(cfg, model, contents, voice)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if is_missing_model(exc):
                    log.warning("Gemini TTS model %s unavailable, trying next", model)
                    break
                if not _is_transient(exc):
                    raise
                if attempt + 1 == attempts:
                    log.warning(
                        "Gemini TTS %s still failing after %d tries (%s)",
                        model, attempts, exc,
                    )
                    break
                wait = _backoff_s(attempt)
                log.warning(
                    "Gemini TTS %s try %d/%d failed (%s), retrying in %.0fs",
                    model, attempt + 1, attempts, exc, wait,
                )
                time.sleep(wait)
                continue

            _write_wav(dest, pcm, rate)
            if _active_tts_model != model:
                log.info("using Gemini TTS model: %s", model)
                _active_tts_model = model
            return [], model

    raise RuntimeError(
        f"Gemini TTS failed on every model ({', '.join(ordered)}): {last_error}. "
        "Check GEMINI_API_KEY, audio.models and audio.voice in config.yaml."
    ) from last_error


async def _synthesize_edge(
    cfg: Config, text: str, dest: Path, attempts: int = 3
) -> list[Boundary]:
    """Synthesize with edge-tts and write MP3. Returns WordBoundary timings."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        audio = bytearray()
        boundaries: list[Boundary] = []
        try:
            comm = edge_tts.Communicate(
                text,
                voice=cfg.audio.fallback_voice,
                rate=cfg.audio.fallback_rate,
                pitch=cfg.audio.fallback_pitch,
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
            log.warning(
                "edge-tts attempt %d failed (%s), retrying in %ss",
                attempt + 1,
                exc,
                wait,
            )
            await asyncio.sleep(wait)

    raise RuntimeError(
        f"edge-tts failed after {attempts} attempts: {last_error}. "
        "If this is a 403, upgrade with: pip install -U 'edge-tts>=7.2.7'"
    ) from last_error


class SceneSpeech:
    """One scene's synthesized narration plus where it came from."""

    __slots__ = ("boundaries", "path", "backend", "voice")

    def __init__(self, boundaries: list[Boundary], path: Path, backend: str, voice: str) -> None:
        self.boundaries = boundaries
        self.path = path
        self.backend = backend
        self.voice = voice


def _synthesize(cfg: Config, text: str, dest_stem: Path, voice: str | None = None) -> SceneSpeech:
    """Synthesize one scene with Gemini TTS.

    Edge-tts runs only in two explicit cases, never as a silent fallback:
    `audio.provider: edge` (configured), or `allow_edge_fallback` after a Gemini
    failure (the `--allow-edge` local-testing flag). Otherwise a Gemini failure
    raises, which skips the scheduled run instead of posting a bad voice.
    """
    if cfg.audio.provider == "edge":
        dest = dest_stem.with_suffix(".mp3")
        boundaries = asyncio.run(_synthesize_edge(cfg, text, dest))
        return SceneSpeech(boundaries, dest, "edge", cfg.audio.fallback_voice)

    voice = voice or cfg.audio.voice
    dest = dest_stem.with_suffix(".wav")
    try:
        boundaries, model = _synthesize_gemini(cfg, text, dest, voice)
        return SceneSpeech(boundaries, dest, f"gemini:{model}", voice)
    except Exception as exc:
        if not cfg.audio.allow_edge_fallback:
            raise RuntimeError(
                f"Gemini TTS failed for this scene and edge-tts fallback is off: {exc}"
            ) from exc
        log.warning(
            "Gemini TTS failed (%s); --allow-edge set, using edge-tts (%s) for this scene",
            exc,
            cfg.audio.fallback_voice,
        )
        dest = dest_stem.with_suffix(".mp3")
        boundaries = asyncio.run(_synthesize_edge(cfg, text, dest))
        return SceneSpeech(boundaries, dest, "edge-fallback", cfg.audio.fallback_voice)


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


def voice_for(cfg: Config, speaker: str) -> str:
    """The Gemini voice for a line's speaker. Narration is the narrator voice."""
    if speaker in ("", "narrator"):
        return cfg.audio.voice
    return cfg.audio.character_voices.get(speaker, cfg.audio.dialogue_voice)


def _speed_up(path: Path, tempo: float) -> None:
    """Speed a rendered line up in place. atempo keeps the pitch."""
    if abs(tempo - 1.0) < 1e-6:
        return
    tmp = path.with_name(f"{path.stem}.fast{path.suffix}")
    run(["-i", str(path), "-af", f"atempo={tempo:.3f}", str(tmp)], label="tempo")
    os.replace(tmp, path)


def _speak_line(cfg: Config, line: Line, dest_stem: Path) -> SceneSpeech:
    """Voice one line with its speaker's voice, then apply the speed-up.

    If Gemini rejects a dialogue voice, the line is voiced by the narrator
    instead, so a bad voice name costs a line's character, not the whole day.
    """
    voice = voice_for(cfg, line.speaker)
    try:
        spoken = _synthesize(cfg, line.hindi, dest_stem, voice)
    except Exception as exc:
        if voice == cfg.audio.voice:
            raise
        log.warning(
            "voice %s failed for %s (%s); using the narrator voice for this line",
            voice, line.speaker, exc,
        )
        spoken = _synthesize(cfg, line.hindi, dest_stem, cfg.audio.voice)

    _speed_up(spoken.path, cfg.audio.tempo)
    if abs(cfg.audio.tempo - 1.0) > 1e-6:
        # Edge boundaries were measured on the unsped audio; rescale them.
        spoken.boundaries = [
            Boundary(b.text, b.start / cfg.audio.tempo, b.end / cfg.audio.tempo)
            for b in spoken.boundaries
        ]
    return spoken


def _concat_lines(paths: list[Path], dest: Path, gap_s: float) -> None:
    """Join a scene's line audio into one WAV, with a short gap between lines."""
    inputs: list[str] = []
    steps: list[str] = []
    labels: list[str] = []
    last = len(paths) - 1
    for i, path in enumerate(paths):
        inputs += ["-i", str(path)]
        pad = f",apad=pad_dur={gap_s:.3f}" if i < last and gap_s > 0 else ""
        steps.append(f"[{i}:a]aformat=sample_rates=24000:channel_layouts=mono{pad}[a{i}]")
        labels.append(f"[a{i}]")
    if len(paths) == 1:
        steps.append("[a0]anull[aout]")
    else:
        steps.append(f"{''.join(labels)}concat=n={len(paths)}:v=0:a=1[aout]")
    run(
        [*inputs, "-filter_complex", ";".join(steps), "-map", "[aout]",
         "-c:a", "pcm_s16le", str(dest)],
        label="scene lines",
    )


def _speak_scene(cfg: Config, scene: Scene, out_dir: Path) -> tuple[Path, float, list[WordTiming]]:
    """Voice every line of a scene. Returns (scene WAV, speech seconds, word timings).

    Word timings are computed per line from that line's measured length and then
    placed on the scene's own clock, so captions follow the real audio.
    """
    number = scene.scene_number
    paths: list[Path] = []
    line_durations: list[float] = []
    line_words: list[list[WordTiming]] = []

    for k, line in enumerate(scene.speech_lines(), start=1):
        spoken = _speak_line(cfg, line, out_dir / f"scene_{number:02d}_line_{k:02d}")
        duration = probe_duration(spoken.path)
        log.info(
            "scene %d line %d (%s): backend=%s voice=%s, %.2fs",
            number, k, line.speaker, spoken.backend, spoken.voice, duration,
        )
        line_words.append(
            _align(spoken.boundaries, tokenize(line.hindi), tokenize(line.hinglish), duration)
        )
        paths.append(spoken.path)
        line_durations.append(duration)

    scene_wav = out_dir / f"scene_{number:02d}.wav"
    _concat_lines(paths, scene_wav, cfg.audio.line_gap_s)
    speech = probe_duration(scene_wav)

    words: list[WordTiming] = []
    offset = 0.0
    for duration, timings in zip(line_durations, line_words):
        for w in timings:
            words.append(WordTiming(word=w.word, start=w.start + offset, end=w.end + offset))
        offset += duration + cfg.audio.line_gap_s
    return scene_wav, speech, words


def _build(cfg: Config, story: Story, out_dir: Path) -> list[SceneAudio]:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes: list[SceneAudio] = []
    cursor = 0.0

    for scene in story.scenes:
        dest, speech, words = _speak_scene(cfg, scene, out_dir)
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
        log.info(
            "scene %d: %.2fs speech, %d lines, %d words",
            scene.scene_number, speech, len(scene.speech_lines()), len(words),
        )
        cursor += slot

    return scenes


def generate_audio(cfg: Config, story: Story, out_dir: Path) -> list[SceneAudio]:
    return _build(cfg, story, out_dir)


def total_duration(scenes: list[SceneAudio]) -> float:
    return round(sum(s.duration for s in scenes), 3)
