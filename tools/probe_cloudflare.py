"""Probe Cloudflare Workers AI for resolution and style adherence.

    .venv/bin/python -m tools.probe_cloudflare
"""

from __future__ import annotations

import base64
import io
import os
import sys
import time
from pathlib import Path

import httpx
from PIL import Image
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT  # noqa: E402

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


def _decode(resp: httpx.Response) -> Image.Image:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "image/" in ctype:
        return Image.open(io.BytesIO(resp.content))
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    result = data.get("result")
    b64 = result["image"] if isinstance(result, dict) else result
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def probe_klein(account_id: str, api_token: str, width: int, height: int, prompt: str) -> tuple[Path, float]:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/ai/run/@cf/black-forest-labs/flux-2-klein-4b"
    )
    headers = {"Authorization": f"Bearer {api_token}"}
    files = {
        "prompt": (None, prompt),
        "width": (None, str(width)),
        "height": (None, str(height)),
        "seed": (None, "42"),
    }
    started = time.time()
    resp = httpx.post(url, headers=headers, files=files, timeout=180.0)
    elapsed = time.time() - started
    img = _decode(resp)
    out = ROOT / "work" / "_probe_cloudflare"
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"klein_{width}x{height}.png"
    img.convert("RGB").save(dest, "PNG")
    return dest, elapsed


def main() -> int:
    load_dotenv(ROOT / ".env")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not api_token:
        print("ERROR: Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env")
        return 1

    full_prompt = f"{STYLE}. {SCENE}. {CHARACTER}. vertical 9:16 composition, no text"
    trials = [(768, 1344), (832, 1472), (896, 1600)]

    print(f"Testing flux-2-klein-4b\n{full_prompt}\n")
    print(f"{'Resolution':<15} {'Got':<15} {'Time'}")
    print("-" * 40)
    for w, h in trials:
        try:
            dest, elapsed = probe_klein(account_id, api_token, w, h, full_prompt)
            img = Image.open(dest)
            print(f"{w}x{h:<10} {img.size[0]}x{img.size[1]:<10} {elapsed:5.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"{w}x{h:<10} FAILED: {exc}")

    print(f"\nImages saved to {ROOT / 'work' / '_probe_cloudflare'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
