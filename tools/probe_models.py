"""Find which Pollinations image models this key can actually generate with.

The catalogue's `paid_only: false` only means the model is not restricted to
paid accounts -- it still draws on a pollen balance, so a fresh account can get
402 on everything. This reports what really works, cheapest first.

    .venv/bin/python -m tools.probe_models
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT, load_config  # noqa: E402

PROMPT = (
    "flat 2D vector cartoon illustration, Amar Chitra Katha Indian comic book art, "
    "bold black outlines, flat cel shading, bright saturated colors. "
    "An Indian emperor on a golden throne in a marble court, wide shot"
)

# name, endpoint, listed pollen cost per image
CANDIDATES = [
    ("gptimage", "gen", "0.000006"),
    ("gpt-image-2", "gen", "0.0000225"),
    ("gptimage-large", "gen", "0.000032"),
    ("dreamshaper", "gen", "0.0001"),
    ("Spit-fires/flux-schnell", "gen", "0.001"),
    ("flux", "gen", "0.002"),
    ("zimage", "gen", "0.004"),
    ("klein", "gen", "0.005"),
    ("dreamshaper", "legacy", "free"),
]


def main() -> int:
    cfg = load_config()
    token = cfg.pollinations_token
    print(f"token: {'set' if token else 'NOT SET'}\n")

    out = ROOT / "work" / "_models"
    out.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    working: list[str] = []

    for model, endpoint, cost in CANDIDATES:
        if endpoint == "gen":
            url = (
                f"https://gen.pollinations.ai/image/{quote(PROMPT, safe='')}"
                f"?model={model}&width=1080&height=1920&seed=7&nologo=true"
            )
            hdrs = headers
        else:
            url = (
                f"https://image.pollinations.ai/prompt/{quote(PROMPT, safe='')}"
                f"?model={model}&width=1080&height=1920&seed=7&nologo=true"
            )
            hdrs = {}

        label = f"{model} [{endpoint}]"
        try:
            resp = httpx.get(url, headers=hdrs, timeout=180.0, follow_redirects=True)
            if resp.status_code != 200:
                note = resp.text[:90].replace("\n", " ")
                print(f"  {label:34} {cost:>10}  HTTP {resp.status_code}  {note}")
                continue
            dest = out / f"{model.replace('/', '_')}_{endpoint}.png"
            dest.write_bytes(resp.content)
            size = Image.open(dest).size
            print(f"  {label:34} {cost:>10}  OK  {size[0]}x{size[1]}")
            working.append(label)
        except Exception as exc:  # noqa: BLE001 - probe, keep going
            print(f"  {label:34} {cost:>10}  ERROR {type(exc).__name__}")

    print(f"\nusable: {working or 'none'}")
    print(f"images in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
