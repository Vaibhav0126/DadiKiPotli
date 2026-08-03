"""Module 3a: scene stills.

Default provider is Cloudflare Workers AI (free 10k neurons/day). Pollinations
remains as a fallback when `images.provider: pollinations`.

Character consistency is approached three ways at once, because prompt text
alone is not enough:
  1. The character bible is appended verbatim to every scene prompt.
  2. The seed is locked per story, so the model's interpretation of a
     character stays stable across scenes.
  3. Optionally (consistency_mode: kontext) a generated character sheet is
     passed back as a reference image (Cloudflare klein / Pollinations kontext).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image

from . import cache
from .config import Config
from .models import SceneVisual, Story
from .script_engine import build_scene_image_prompt

log = logging.getLogger(__name__)

# Pollinations endpoints (fallback provider).
#
#   gen.pollinations.ai  requires a (free) API key and exposes the real models.
#   image.pollinations.ai works with no key, but serves DreamShaper 8 LCM only.
GEN_URL = "https://gen.pollinations.ai/image"
LEGACY_URL = "https://image.pollinations.ai/prompt"

CF_RUN = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/{model}"


class RateLimiter:
    """Simple gap between requests. Pollinations needs it; Cloudflare barely does."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < self.min_interval:
            delay = self.min_interval - elapsed
            log.debug("rate limit: sleeping %.1fs", delay)
            time.sleep(delay)
        self._last = time.monotonic()


def story_seed(story: Story) -> int:
    """Stable per-story seed, so re-runs reproduce the same cast."""
    digest = hashlib.sha256(story.slug.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 1_000_000


def _validate(data: bytes) -> Image.Image:
    """Reject HTML error pages and truncated downloads before they reach ffmpeg."""
    img = Image.open(io.BytesIO(data))
    img.load()
    if img.width < 64 or img.height < 64:
        raise ValueError(f"suspiciously small image: {img.width}x{img.height}")
    return img


def _save_rgb(img: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(dest, "PNG")


# ---------------------------------------------------------------------------
# Cloudflare Workers AI
# ---------------------------------------------------------------------------

def _cf_headers(cfg: Config) -> dict[str, str]:
    token = cfg.cloudflare_api_token
    if not token or not cfg.cloudflare_account_id:
        raise RuntimeError(
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required when "
            "images.provider is cloudflare. Get them from https://dash.cloudflare.com/"
        )
    return {"Authorization": f"Bearer {token}"}


def _cf_url(cfg: Config) -> str:
    return CF_RUN.format(account=cfg.cloudflare_account_id, model=cfg.images.model)


def _cf_decode(resp: httpx.Response) -> Image.Image:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "image/" in ctype:
        return _validate(resp.content)

    data = resp.json()
    if data.get("errors"):
        msgs = "; ".join(
            e.get("message", str(e)) if isinstance(e, dict) else str(e)
            for e in data["errors"]
        )
        raise RuntimeError(f"Cloudflare AI error: {msgs}")

    result = data.get("result")
    b64: str | None = None
    if isinstance(result, dict) and "image" in result:
        b64 = result["image"]
    elif isinstance(result, str):
        b64 = result
    if not b64:
        raise RuntimeError(f"unexpected Cloudflare response shape: {list(data)[:8]}")
    return _validate(base64.b64decode(b64))


def _cf_generate(
    cfg: Config,
    prompt: str,
    seed: int,
    dest: Path,
    limiter: RateLimiter,
    reference_path: Path | None = None,
) -> None:
    """flux-2-klein-4b requires multipart; reference images must be <512x512."""
    last_error: Exception | None = None
    for attempt in range(cfg.images.max_retries):
        limiter.wait()
        try:
            files: dict[str, tuple[None, str] | tuple[str, bytes, str]] = {
                "prompt": (None, prompt),
                "width": (None, str(cfg.images.width)),
                "height": (None, str(cfg.images.height)),
                "seed": (None, str(seed)),
            }
            if reference_path and reference_path.exists():
                ref = Image.open(reference_path)
                ref.thumbnail((512, 512))
                buf = io.BytesIO()
                ref.convert("RGB").save(buf, format="PNG")
                files["input_image_0"] = ("ref.png", buf.getvalue(), "image/png")

            resp = httpx.post(
                _cf_url(cfg),
                headers=_cf_headers(cfg),
                files=files,
                timeout=180.0,
            )
            if resp.status_code == 429:
                raise RuntimeError("rate limited (429)")
            # 400 with a model error still has a JSON body we can surface.
            if resp.status_code >= 500:
                resp.raise_for_status()
            img = _cf_decode(resp)
            _save_rgb(img, dest)
            return
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_error = exc
            backoff = min(2**attempt * 5, 60)
            log.warning(
                "image attempt %d/%d failed (%s), retrying in %ss",
                attempt + 1,
                cfg.images.max_retries,
                exc,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"image generation failed after {cfg.images.max_retries} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Pollinations (fallback)
# ---------------------------------------------------------------------------

def _pollinations_url(cfg: Config, prompt: str, seed: int, reference: str | None) -> str:
    keyed = bool(cfg.pollinations_token)
    model = "kontext" if reference else (cfg.images.model if keyed else cfg.images.anonymous_model)
    params = [
        f"model={model}",
        f"width={cfg.images.width}",
        f"height={cfg.images.height}",
        f"seed={seed}",
        "nologo=true",
        "private=true",
    ]
    if reference:
        params.append(f"image={quote(reference, safe='')}")
    base = GEN_URL if keyed else LEGACY_URL
    return f"{base}/{quote(prompt, safe='')}?{'&'.join(params)}"


def _pollinations_fetch(cfg: Config, url: str, dest: Path, limiter: RateLimiter) -> None:
    headers = {}
    if cfg.pollinations_token:
        headers["Authorization"] = f"Bearer {cfg.pollinations_token}"

    last_error: Exception | None = None
    for attempt in range(cfg.images.max_retries):
        limiter.wait()
        try:
            resp = httpx.get(url, headers=headers, timeout=180.0, follow_redirects=True)
            if resp.status_code == 429:
                raise RuntimeError("rate limited (429)")
            resp.raise_for_status()
            _save_rgb(_validate(resp.content), dest)
            return
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
            last_error = exc
            backoff = min(2**attempt * 5, 60)
            log.warning(
                "image attempt %d/%d failed (%s), retrying in %ss",
                attempt + 1,
                cfg.images.max_retries,
                exc,
                backoff,
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"image generation failed after {cfg.images.max_retries} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _character_sheet(
    cfg: Config, story: Story, out_dir: Path, limiter: RateLimiter, seed: int
) -> Path | str | None:
    """Build a cast reference. Cloudflare returns a local path; Pollinations a URL."""
    if not story.characters:
        return None
    descriptions = ". ".join(c.visual_description for c in story.characters[:2])
    prompt = (
        f"Character reference sheet, full body, neutral standing pose, plain background. "
        f"{descriptions}. {cfg.story.art_style}"
    )
    dest = out_dir / "character_sheet.png"
    key = cache.key_for(
        prompt=prompt, seed=seed, model=cfg.images.model, provider=cfg.images.provider
    )
    if cache.is_fresh(dest, key):
        log.info("character sheet cached")
    else:
        log.info("generating character reference sheet")
        if cfg.images.provider == "cloudflare":
            _cf_generate(cfg, prompt, seed, dest, limiter)
        else:
            url = _pollinations_url(cfg, prompt, seed, reference=None)
            _pollinations_fetch(cfg, url, dest, limiter)
        cache.mark_fresh(dest, key)

    if cfg.images.provider == "cloudflare":
        return dest
    # Pollinations URLs are deterministic given a seed, so the sheet's own URL
    # can be handed straight back as a kontext reference without hosting it.
    return _pollinations_url(cfg, prompt, seed, reference=None)


def generate_images(
    cfg: Config, story: Story, out_dir: Path, consistency_mode: str = "seed"
) -> list[SceneVisual]:
    out_dir.mkdir(parents=True, exist_ok=True)
    limiter = RateLimiter(cfg.images.min_request_interval_s)
    seed = story_seed(story)
    provider = cfg.images.provider

    if provider == "cloudflare":
        log.info("images: %s via Cloudflare Workers AI", cfg.images.model)
    elif cfg.pollinations_token:
        log.info("images: %s via gen.pollinations.ai", cfg.images.model)
    else:
        log.warning(
            "No POLLINATIONS_TOKEN set, so only %s is available and output is "
            "capped near 576x1024 with weak style adherence. Prefer "
            "images.provider: cloudflare with a free Workers AI token.",
            cfg.images.anonymous_model,
        )

    reference: Path | str | None = None
    if consistency_mode == "kontext":
        reference = _character_sheet(cfg, story, out_dir, limiter, seed)

    visuals: list[SceneVisual] = []
    for scene in story.scenes:
        dest = out_dir / f"scene_{scene.scene_number:02d}.png"
        prompt = build_scene_image_prompt(cfg, story, scene)
        key = cache.key_for(
            prompt=prompt,
            seed=seed,
            model=cfg.images.model,
            provider=provider,
            size=(cfg.images.width, cfg.images.height),
            reference=str(reference) if reference else None,
        )
        if cache.is_fresh(dest, key):
            log.info("scene %d: image cached", scene.scene_number)
        else:
            log.info("scene %d: generating image", scene.scene_number)
            if provider == "cloudflare":
                ref_path = reference if isinstance(reference, Path) else None
                _cf_generate(cfg, prompt, seed, dest, limiter, reference_path=ref_path)
            else:
                ref_url = reference if isinstance(reference, str) else None
                _pollinations_fetch(
                    cfg, _pollinations_url(cfg, prompt, seed, ref_url), dest, limiter
                )
            cache.mark_fresh(dest, key)
        visuals.append(SceneVisual(scene_number=scene.scene_number, image_path=str(dest)))

    return visuals


def contact_sheet(visuals: list[SceneVisual], dest: Path, columns: int = 5) -> Path:
    """One QC image showing every still, reviewed before any money is spent."""
    thumbs = []
    for v in visuals:
        img = Image.open(v.image_path)
        img.thumbnail((320, 320))
        thumbs.append(img)
    if not thumbs:
        raise ValueError("no visuals to lay out")

    cw = max(t.width for t in thumbs)
    ch = max(t.height for t in thumbs)
    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (cw * columns, ch * rows), "black")
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % columns) * cw, (i // columns) * ch))
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return dest
