"""Sound effects synthesized with ffmpeg, so no licensed sound files are needed.

Both effects are generated into assets/sfx/ the first time they are needed:

- whoosh: filtered pink noise with a fast fade in and out, on each scene change
- dhol: a low, pitch-dropping thump with an exponential decay, on the twist

They are mixed under the narration by composer.mix_audio.
"""

from __future__ import annotations

from pathlib import Path

from .config import ROOT
from .ffmpeg_util import run
from .models import Timeline

SFX_DIR = ROOT / "assets" / "sfx"

# name -> (ffmpeg input args, audio filter chain)
_SPECS: dict[str, tuple[list[str], str]] = {
    "whoosh": (
        ["-f", "lavfi", "-i", "anoisesrc=d=0.4:c=pink:r=48000:a=0.6"],
        "highpass=f=300,lowpass=f=3500,"
        "afade=t=in:st=0:d=0.12,afade=t=out:st=0.18:d=0.22,volume=0.9",
    ),
    "dhol": (
        [
            "-f", "lavfi", "-i",
            "aevalsrc='sin(2*PI*(60+120*exp(-30*t))*t)*exp(-7*t)':d=0.6:s=48000",
        ],
        "afade=t=out:st=0.3:d=0.3,volume=0.8",
    ),
}

# Gain applied to a cue at mix time, relative to the narration.
CUE_GAIN = {"whoosh": 0.55, "dhol": 0.7}


def ensure_sfx(directory: Path = SFX_DIR) -> dict[str, Path]:
    """Render any missing effect to disk and return name -> path."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, (inputs, chain) in _SPECS.items():
        dest = directory / f"{name}.wav"
        if not dest.exists():
            run(
                [*inputs, "-af", chain, "-ac", "2", "-ar", "48000", str(dest)],
                label=f"sfx {name}",
            )
        paths[name] = dest
    return paths


def cues(timeline: Timeline) -> list[tuple[float, str, float]]:
    """(start seconds, effect name, gain) for every sound in the video.

    A whoosh marks each cut between scenes. The dhol lands on the twist, the
    scene the whole video is built around.
    """
    beats = {s.scene_number: s.beat for s in timeline.story.scenes}
    out: list[tuple[float, str, float]] = []
    for audio in timeline.audio[1:]:
        out.append((audio.start, "whoosh", CUE_GAIN["whoosh"]))
    # A story can tag more than one scene as twist. The thump marks the first.
    twist = next((a for a in timeline.audio if beats.get(a.scene_number) == "twist"), None)
    if twist is not None:
        out.append((twist.start, "dhol", CUE_GAIN["dhol"]))
    return sorted(out)
