"""Module 5: publish a finished short to YouTube Shorts and/or Instagram Reels.

Gated behind `--publish`. Metadata is generated with Gemini and cached at
`work/<slug>/publish.json`. Platform ids land in `publish_result.json` so a
re-run can skip platforms that already succeeded.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Iterable

import httpx
from google import genai
from google.genai import types
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel, Field

from .config import Config
from .models import Story, read_json, write_json

log = logging.getLogger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_ENTERTAINMENT = "24"

_active_model: str | None = None

Platform = str  # "youtube" | "instagram"


class PublishMeta(BaseModel):
    youtube_title: str
    youtube_description: str
    youtube_tags: list[str] = Field(default_factory=list)
    instagram_caption: str


class PublishResult(BaseModel):
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    instagram_container_id: str | None = None
    instagram_media_id: str | None = None
    instagram_url: str | None = None
    r2_url: str | None = None


def parse_platforms(raw: str | None) -> list[Platform]:
    """Parse `--publish` CSV into a deduped ordered list."""
    if raw is None:
        return []
    allowed = {"youtube", "instagram"}
    out: list[Platform] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in allowed:
            raise ValueError(f"unknown publish platform {name!r}; use youtube,instagram")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("--publish needs at least one of: youtube,instagram")
    return out


def _is_missing_model(exc: Exception) -> bool:
    text = str(exc)
    return "NOT_FOUND" in text or "404" in text or "not available" in text


def _gemini_call(cfg: Config, **kwargs) -> object:
    global _active_model
    candidates = [_active_model] if _active_model else list(cfg.story.models)
    last_error: Exception | None = None
    client = genai.Client(api_key=cfg.gemini_api_key)

    for model in candidates:
        try:
            resp = client.models.generate_content(model=model, **kwargs)
            if _active_model != model:
                log.info("using Gemini model: %s", model)
                _active_model = model
            return resp
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _is_missing_model(exc):
                raise
            log.debug("model %s unavailable, trying next", model)

    raise RuntimeError(
        f"None of the configured Gemini models are available "
        f"({', '.join(cfg.story.models)}). Last error: {last_error}"
    ) from last_error


def _clamp_title(title: str, max_len: int = 100) -> str:
    title = " ".join(title.split())
    if "#shorts" not in title.lower():
        # Leave room for the hashtag suffix.
        budget = max_len - len(" #Shorts")
        title = title[:budget].rstrip(" -|:")
        title = f"{title} #Shorts"
    return title[:max_len]


def _fallback_meta(cfg: Config, story: Story) -> PublishMeta:
    brand = cfg.publish.brand_name
    title = _clamp_title(story.title_hinglish)
    tags = list(dict.fromkeys([*cfg.publish.youtube.default_tags, brand.replace(" ", "")]))
    description = (
        f"{story.title_hinglish}\n\n"
        f"{story.moral_hinglish}\n\n"
        f"Idea: {story.idea}\n\n"
        f"#{' #'.join(tags)}\n#Shorts"
    )
    caption = (
        f"{story.title_hinglish}\n\n"
        f"{story.moral_hinglish}\n\n"
        f"#{' #'.join(tags[:8])}"
    )
    max_chars = cfg.publish.instagram.caption_max_chars
    return PublishMeta(
        youtube_title=title,
        youtube_description=description,
        youtube_tags=tags,
        instagram_caption=caption[:max_chars],
    )


def build_metadata(cfg: Config, story: Story, cache_path: Path, *, force: bool) -> PublishMeta:
    if cache_path.exists() and not force:
        meta = read_json(cache_path, PublishMeta)
        log.info("reusing publish metadata")
        return meta

    brand = cfg.publish.brand_name
    default_tags = ", ".join(cfg.publish.youtube.default_tags)
    prompt = (
        f"Write social post metadata for a vertical Hindi animated short.\n\n"
        f"Brand: {brand}\n"
        f"Story title (Hinglish): {story.title_hinglish}\n"
        f"Story title (Hindi): {story.title_hindi}\n"
        f"Moral (Hinglish): {story.moral_hinglish}\n"
        f"Original idea: {story.idea}\n"
        f"Suggested tags to include: {default_tags}\n\n"
        f"Rules:\n"
        f"- youtube_title: catchy Hinglish, max 90 chars before hashtag, must work as a Shorts title.\n"
        f"- youtube_description: 2-4 short lines + moral tease + hashtags. Include #Shorts.\n"
        f"- youtube_tags: 8-12 lowercase-ish keywords, no # signs.\n"
        f"- instagram_caption: Hinglish, hook first line, moral tease, then hashtags. "
        f"Max {cfg.publish.instagram.caption_max_chars} characters.\n"
        f"- No spoilers of the twist ending.\n"
        f"- Do not invent fake URLs or @handles."
    )

    try:
        resp = _gemini_call(
            cfg,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PublishMeta,
                temperature=0.7,
            ),
        )
        meta = resp.parsed
        if meta is None:
            raise RuntimeError(f"Gemini returned no parsable publish metadata: {resp.text}")
    except Exception as exc:  # noqa: BLE001
        log.warning("metadata generation failed (%s); using story fields", exc)
        meta = _fallback_meta(cfg, story)

    meta = meta.model_copy(
        update={
            "youtube_title": _clamp_title(meta.youtube_title),
            "instagram_caption": meta.instagram_caption[: cfg.publish.instagram.caption_max_chars],
            "youtube_tags": list(
                dict.fromkeys([*meta.youtube_tags, *cfg.publish.youtube.default_tags])
            ),
        }
    )
    write_json(cache_path, meta)
    return meta


def youtube_credentials(cfg: Config, *, interactive: bool = False) -> Credentials:
    """Load cached YouTube OAuth credentials, refreshing or prompting as needed."""
    token_path = cfg.youtube_token_path()
    secrets_path = cfg.youtube_client_secrets()
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not interactive:
        raise RuntimeError(
            f"YouTube OAuth token missing/expired at {token_path}. "
            f"Run: python -m tools.auth_youtube"
        )

    if not secrets_path.exists():
        raise RuntimeError(
            f"YouTube client secrets not found at {secrets_path}. "
            "Download a Desktop OAuth client JSON from Google Cloud Console "
            "and place it there (see README Publish section)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), YOUTUBE_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    log.info("saved YouTube token to %s", token_path)
    return creds


def publish_youtube(cfg: Config, video_path: Path, meta: PublishMeta) -> tuple[str, str]:
    creds = youtube_credentials(cfg, interactive=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    privacy = cfg.publish.youtube.privacy
    if privacy not in {"public", "unlisted", "private"}:
        raise ValueError(f"invalid youtube privacy: {privacy}")

    body = {
        "snippet": {
            "title": meta.youtube_title,
            "description": meta.youtube_description,
            "tags": meta.youtube_tags,
            "categoryId": cfg.publish.youtube.category_id or YOUTUBE_CATEGORY_ENTERTAINMENT,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=8 * 1024 * 1024,
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("youtube upload %.0f%%", status.progress() * 100)

    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    log.info("youtube published: %s", url)
    return video_id, url


def _r2_client(cfg: Config):
    import boto3
    from botocore.config import Config as BotoConfig

    account = cfg.r2_account_id
    key = cfg.r2_access_key_id
    secret = cfg.r2_secret_access_key
    if not (account and key and secret and cfg.r2_bucket and cfg.r2_public_base_url):
        raise RuntimeError(
            "Instagram publish needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET, and R2_PUBLIC_BASE_URL"
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
        config=BotoConfig(signature_version="s3v4"),
    )


def upload_to_r2(cfg: Config, video_path: Path, slug: str) -> str:
    client = _r2_client(cfg)
    prefix = cfg.publish.instagram.r2_prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    key = f"{prefix}{slug}-{int(time.time())}.mp4"
    content_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"

    log.info("uploading to R2: s3://%s/%s", cfg.r2_bucket, key)
    client.upload_file(
        str(video_path),
        cfg.r2_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    url = f"{cfg.r2_public_base_url}/{key}"
    log.info("R2 public URL: %s", url)
    return url


def _graph_base(cfg: Config) -> str:
    return f"https://graph.facebook.com/{cfg.publish.instagram.graph_version}"


def _require_instagram(cfg: Config) -> tuple[str, str]:
    token = cfg.meta_access_token
    user_id = cfg.instagram_user_id
    if not token or not user_id:
        raise RuntimeError(
            "Instagram publish needs META_ACCESS_TOKEN and INSTAGRAM_USER_ID in .env"
        )
    return token, user_id


def publish_instagram(cfg: Config, public_url: str, meta: PublishMeta) -> tuple[str, str, str]:
    """Create a REELS container, wait until ready, then publish.

    Returns (container_id, media_id, permalink_guess).
    """
    token, user_id = _require_instagram(cfg)
    base = _graph_base(cfg)

    with httpx.Client(timeout=60.0) as client:
        create = client.post(
            f"{base}/{user_id}/media",
            params={
                "media_type": "REELS",
                "video_url": public_url,
                "caption": meta.instagram_caption,
                "share_to_feed": "true",
                "access_token": token,
            },
        )
        create.raise_for_status()
        container_id = create.json()["id"]
        log.info("instagram container: %s", container_id)

        deadline = time.monotonic() + cfg.publish.instagram.timeout_s
        status_code = "IN_PROGRESS"
        while time.monotonic() < deadline:
            status_resp = client.get(
                f"{base}/{container_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": token,
                },
            )
            status_resp.raise_for_status()
            payload = status_resp.json()
            status_code = payload.get("status_code", "")
            log.info("instagram container status: %s", status_code)
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                raise RuntimeError(f"Instagram container failed: {payload}")
            time.sleep(cfg.publish.instagram.poll_interval_s)
        else:
            raise TimeoutError(
                f"Instagram container {container_id} not ready after "
                f"{cfg.publish.instagram.timeout_s:.0f}s (last={status_code})"
            )

        publish = client.post(
            f"{base}/{user_id}/media_publish",
            params={
                "creation_id": container_id,
                "access_token": token,
            },
        )
        publish.raise_for_status()
        media_id = publish.json()["id"]

        permalink = f"https://www.instagram.com/reel/{media_id}/"
        try:
            media = client.get(
                f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
            )
            if media.is_success:
                permalink = media.json().get("permalink") or permalink
        except Exception:  # noqa: BLE001
            pass

    log.info("instagram published: %s", permalink)
    return container_id, media_id, permalink


def publish(
    cfg: Config,
    story: Story,
    video_path: Path,
    work_dir: Path,
    platforms: Iterable[Platform],
    *,
    force: bool = False,
) -> PublishResult:
    platforms = list(platforms)
    if not platforms:
        return PublishResult()

    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    meta = build_metadata(cfg, story, work_dir / "publish.json", force=force)

    result_path = work_dir / "publish_result.json"
    result = (
        read_json(result_path, PublishResult)
        if result_path.exists() and not force
        else PublishResult()
    )

    if "youtube" in platforms:
        if result.youtube_video_id and not force:
            log.info("skipping youtube (already published: %s)", result.youtube_url)
        else:
            video_id, url = publish_youtube(cfg, video_path, meta)
            result = result.model_copy(
                update={"youtube_video_id": video_id, "youtube_url": url}
            )
            write_json(result_path, result)

    if "instagram" in platforms:
        if result.instagram_media_id and not force:
            log.info("skipping instagram (already published: %s)", result.instagram_url)
        else:
            r2_url = result.r2_url
            if not r2_url or force:
                r2_url = upload_to_r2(cfg, video_path, story.slug)
                result = result.model_copy(update={"r2_url": r2_url})
                write_json(result_path, result)
            container_id, media_id, url = publish_instagram(cfg, r2_url, meta)
            result = result.model_copy(
                update={
                    "instagram_container_id": container_id,
                    "instagram_media_id": media_id,
                    "instagram_url": url,
                }
            )
            write_json(result_path, result)

    write_json(result_path, result)
    return result
