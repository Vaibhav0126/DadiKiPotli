"""Module 5: publish a finished video to YouTube.

Gated behind `--publish`. Shorts get #Shorts metadata; long-form (`publish.format:
long`) uploads as a standard landscape video. Metadata is generated with Gemini
and cached at `work/<slug>/publish.json`. Platform ids land in
`publish_result.json` so a re-run can skip uploads that already succeeded.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google import genai
from google.genai import types
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel, Field

from .config import Config
from .gemini_util import generate_with_fallback
from .models import Story, read_json, write_json

log = logging.getLogger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_ENTERTAINMENT = "24"

_active_model: str | None = None


class PublishMeta(BaseModel):
    youtube_title: str
    youtube_description: str
    youtube_tags: list[str] = Field(default_factory=list)


class PublishResult(BaseModel):
    youtube_video_id: str | None = None
    youtube_url: str | None = None


def _gemini_call(cfg: Config, **kwargs) -> object:
    global _active_model
    client = genai.Client(api_key=cfg.gemini_api_key)

    def _one(model: str) -> object:
        return client.models.generate_content(model=model, **kwargs)

    resp, model = generate_with_fallback(
        models=list(cfg.story.models),
        call=_one,
        active_model=_active_model,
        label="Gemini publish",
    )
    if _active_model != model:
        log.info("using Gemini model: %s", model)
        _active_model = model
    return resp


def _clamp_title(title: str, *, shorts: bool, max_len: int = 100) -> str:
    title = " ".join(title.split())
    if shorts and "#shorts" not in title.lower():
        # Leave room for the hashtag suffix.
        budget = max_len - len(" #Shorts")
        title = title[:budget].rstrip(" -|:")
        title = f"{title} #Shorts"
    return title[:max_len]


def _fallback_meta(cfg: Config, story: Story) -> PublishMeta:
    brand = cfg.publish.brand_name
    shorts = cfg.publish.is_shorts
    title = _clamp_title(story.title_hinglish, shorts=shorts)
    tags = list(dict.fromkeys([*cfg.publish.youtube.default_tags, brand.replace(" ", "")]))
    hash_line = f"#{' #'.join(tags)}"
    if shorts:
        hash_line += "\n#Shorts"
    description = (
        f"{story.title_hinglish}\n\n"
        f"{story.moral_hinglish}\n\n"
        f"Idea: {story.idea}\n\n"
        f"{hash_line}"
    )
    return PublishMeta(
        youtube_title=title,
        youtube_description=description,
        youtube_tags=tags,
    )


def build_metadata(cfg: Config, story: Story, cache_path: Path, *, force: bool) -> PublishMeta:
    if cache_path.exists() and not force:
        meta = read_json(cache_path, PublishMeta)
        log.info("reusing publish metadata")
        return meta

    brand = cfg.publish.brand_name
    shorts = cfg.publish.is_shorts
    default_tags = ", ".join(cfg.publish.youtube.default_tags)
    if shorts:
        prompt = (
            f"Write YouTube Shorts metadata for a vertical Hindi animated short.\n\n"
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
            f"- No spoilers of the twist ending.\n"
            f"- Do not invent fake URLs or @handles."
        )
    else:
        prompt = (
            f"Write YouTube metadata for a 16:9 landscape Hindi animated folk tale "
            f"(about 6+ minutes, NOT a Short — do not use #Shorts).\n\n"
            f"Brand: {brand}\n"
            f"Story title (Hinglish): {story.title_hinglish}\n"
            f"Story title (Hindi): {story.title_hindi}\n"
            f"Moral (Hinglish): {story.moral_hinglish}\n"
            f"Original idea: {story.idea}\n"
            f"Suggested tags to include: {default_tags}\n\n"
            f"Rules:\n"
            f"- youtube_title: catchy Hinglish, max 100 chars, no #Shorts.\n"
            f"- youtube_description: 4-8 lines with a hook, soft moral tease, and hashtags. "
            f"Never include #Shorts or the word Shorts.\n"
            f"- youtube_tags: 8-12 keywords, no # signs, no Shorts.\n"
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
            "youtube_title": _clamp_title(meta.youtube_title, shorts=shorts),
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
        try:
            creds.refresh(Request())
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except RefreshError as exc:
            # Testing-mode refresh tokens expire after ~7 days (or get revoked).
            # Fall through to interactive re-consent when allowed.
            log.warning("YouTube refresh token unusable (%s)", exc)
            if not interactive:
                raise RuntimeError(
                    f"YouTube OAuth token expired/revoked at {token_path}. "
                    f"Run: python -m tools.auth_youtube"
                ) from exc
            creds = None

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


def publish(
    cfg: Config,
    story: Story,
    video_path: Path,
    work_dir: Path,
    *,
    force: bool = False,
) -> PublishResult:
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")

    meta = build_metadata(cfg, story, work_dir / "publish.json", force=force)

    result_path = work_dir / "publish_result.json"
    result = (
        read_json(result_path, PublishResult)
        if result_path.exists() and not force
        else PublishResult()
    )

    if result.youtube_video_id and not force:
        log.info("skipping youtube (already published: %s)", result.youtube_url)
        return result

    video_id, url = publish_youtube(cfg, video_path, meta)
    result = result.model_copy(
        update={"youtube_video_id": video_id, "youtube_url": url}
    )
    write_json(result_path, result)
    return result
