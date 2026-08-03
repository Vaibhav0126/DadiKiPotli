"""Probe Pollinations for honoured resolutions and prompt-ordering effects.

Not part of the pipeline. Used to tune config.yaml's image size and the
art_style placement, since Flux weights early tokens much more heavily than
trailing ones.

    .venv/bin/python -m tools.probe_images
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT, load_config  # noqa: E402
from src.visual_engine import RateLimiter, _build_url, _fetch  # noqa: E402

SCENE = (
    "Akbar seated on a golden throne in a marble court, wide establishing shot, "
    "warm morning light"
)
CHARACTER = (
    "Akbar, a 45-year-old Mughal emperor, thick black beard, large white turban "
    "with a red ruby, emerald-green silk tunic with gold embroidery"
)
STYLE = (
    "flat 2D vector cartoon illustration, Amar Chitra Katha Indian comic book art, "
    "bold black outlines, flat cel shading, no gradients, bright saturated colors, "
    "storybook illustration"
)


def style_last(cfg) -> str:
    return f"{SCENE}. {CHARACTER}. {cfg.story.art_style}. vertical 9:16, no text"


def style_first(cfg) -> str:
    return f"{STYLE}. {SCENE}. {CHARACTER}. vertical 9:16 composition, no text"


def main() -> int:
    cfg = load_config()
    out = ROOT / "work" / "_probe"
    out.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(cfg.images.min_request_interval_s)

    trials = [
        ("a_832x1472_style_last", 832, 1472, style_last(cfg), cfg.images.model),
        ("b_1080x1920_style_last", 1080, 1920, style_last(cfg), cfg.images.model),
        ("c_1080x1920_style_first", 1080, 1920, style_first(cfg), cfg.images.model),
        ("d_1088x1920_style_first", 1088, 1920, style_first(cfg), cfg.images.model),
        ("e_turbo_1080x1920_first", 1080, 1920, style_first(cfg), "turbo"),
    ]

    for name, w, h, prompt, model in trials:
        cfg.images.width, cfg.images.height, cfg.images.model = w, h, model
        dest = out / f"{name}.png"
        started = time.time()
        try:
            _fetch(cfg, _build_url(cfg, prompt, seed=424242, reference=None), dest, limiter)
            size = Image.open(dest).size
            print(f"{name:28} requested {w}x{h:<5} got {size[0]}x{size[1]:<5} "
                  f"({time.time() - started:.0f}s)")
        except Exception as exc:  # noqa: BLE001 - probe script, keep going
            print(f"{name:28} FAILED: {exc}")

    print(f"\nimages in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
