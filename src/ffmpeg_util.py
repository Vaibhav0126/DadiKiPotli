"""Thin ffmpeg/ffprobe wrappers, plus discovery of a build that can render text.

Homebrew's default `ffmpeg` formula is a minimal build with no libass and no
libfreetype, so it cannot burn subtitles at all. `ffmpeg-full` has them but
installs unlinked. Rather than relinking the user's system, resolve a capable
binary at runtime and remember it.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

# Checked in order; the first build exposing the `ass` filter wins.
CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
]


class FFmpegError(RuntimeError):
    pass


def _has_ass_filter(binary: str) -> bool:
    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        line.split()[1] == "ass" for line in proc.stdout.splitlines() if len(line.split()) > 1
    )


@functools.cache
def ffmpeg_binary() -> str:
    override = os.getenv("FFMPEG_BINARY")
    if override:
        if not _has_ass_filter(override):
            raise FFmpegError(
                f"FFMPEG_BINARY={override} has no `ass` filter, so captions cannot be "
                "burned. Install a full build with: brew install ffmpeg-full"
            )
        return override

    for candidate in CANDIDATES:
        if Path(candidate).exists() and _has_ass_filter(candidate):
            return candidate

    on_path = shutil.which("ffmpeg")
    if on_path and _has_ass_filter(on_path):
        return on_path

    if on_path:
        raise FFmpegError(
            f"The ffmpeg on your PATH ({on_path}) was built without libass, so it "
            "cannot burn captions. Install a full build with:\n"
            "    brew install ffmpeg-full\n"
            "It installs alongside your existing ffmpeg and is picked up automatically."
        )
    raise FFmpegError("ffmpeg not found. Install it with: brew install ffmpeg-full")


@functools.cache
def ffprobe_binary() -> str:
    sibling = Path(ffmpeg_binary()).with_name("ffprobe")
    if sibling.exists():
        return str(sibling)
    found = shutil.which("ffprobe")
    if not found:
        raise FFmpegError("ffprobe not found. Install it with: brew install ffmpeg-full")
    return found


def ensure_available() -> str:
    return ffmpeg_binary()


def run(args: list[str], *, label: str = "ffmpeg") -> None:
    cmd = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-25:]
        raise FFmpegError(f"{label} failed:\n" + "\n".join(tail))


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}:\n{proc.stderr}")
    return float(json.loads(proc.stdout)["format"]["duration"])


def escape_filter_path(path: Path | str) -> str:
    """Escape a path for use as a value inside a filtergraph.

    Filtergraphs treat ':' as an argument separator and '\\' as an escape, so
    absolute macOS paths need escaping before `ass=` will accept them.
    """
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
