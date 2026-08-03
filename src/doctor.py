"""Preflight checks. `python -m src.main --check`

Verifies the things that fail confusingly or silently: an ffmpeg without
libass, a missing caption font, an absent key, or an image provider that is
misconfigured (Cloudflare credentials / Pollinations fallback).
"""

from __future__ import annotations

import os

import httpx

from .config import Config
from .ffmpeg_util import FFmpegError, ffmpeg_binary

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.rows.append((status, name, detail))

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)

    def render(self) -> str:
        width = max(len(n) for _, n, _ in self.rows)
        lines = [f"[{s}] {n.ljust(width)}  {d}" for s, n, d in self.rows]
        return "\n".join(lines)


def _check_ffmpeg(report: Report) -> None:
    try:
        report.add(OK, "ffmpeg", f"{ffmpeg_binary()} (libass present)")
    except FFmpegError as exc:
        report.add(FAIL, "ffmpeg", str(exc).splitlines()[0])


def _check_font(cfg: Config, report: Report) -> None:
    path = cfg.font_path()
    if path.exists():
        report.add(OK, "caption font", f"{path.name} ({path.stat().st_size // 1024} KB)")
    else:
        report.add(
            FAIL,
            "caption font",
            f"missing {path}. Download Poppins-Bold.ttf into assets/fonts/",
        )


def _check_gemini(report: Report) -> None:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        report.add(FAIL, "GEMINI_API_KEY", "not set -- required. https://aistudio.google.com/apikey")
        return
    report.add(OK, "GEMINI_API_KEY", "set")


def _check_images(cfg: Config, report: Report) -> None:
    if cfg.images.provider == "cloudflare":
        if cfg.cloudflare_account_id and cfg.cloudflare_api_token:
            report.add(
                OK,
                "images",
                f"cloudflare / {cfg.images.model} @ {cfg.images.width}x{cfg.images.height}",
            )
        else:
            report.add(
                FAIL,
                "images",
                "provider=cloudflare but CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing",
            )
        return

    if cfg.pollinations_token:
        report.add(OK, "images", f"pollinations / {cfg.images.model} (token set)")
        return
    report.add(
        WARN,
        "images",
        f"pollinations without token -- limited to {cfg.images.anonymous_model}. "
        "Prefer images.provider: cloudflare for free quality.",
    )


def _check_fal(cfg: Config, report: Report) -> None:
    if not cfg.motion.enabled:
        report.add(OK, "FAL_KEY", "not needed (motion disabled)")
        return
    if cfg.fal_key:
        per_video = cfg.motion.usd_per_second * cfg.motion.clip_seconds * cfg.motion.hero_scene_count
        report.add(
            OK,
            "FAL_KEY",
            f"set -- ~${per_video:.2f}/video, capped at ${cfg.motion.max_cost_usd:.2f}",
        )
    else:
        report.add(WARN, "FAL_KEY", "not set -- run with --no-motion, or scenes fall back to Ken Burns")


def _check_music(cfg: Config, report: Report) -> None:
    directory = cfg.music_dir()
    tracks = (
        [p for p in directory.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}]
        if directory.exists()
        else []
    )
    if tracks:
        report.add(OK, "music", f"{len(tracks)} track(s) in {directory.name}/")
    else:
        report.add(WARN, "music", f"none in {directory} -- video will have voice only")


def _check_youtube_publish(cfg: Config, report: Report) -> None:
    secrets = cfg.youtube_client_secrets()
    token = cfg.youtube_token_path()
    if not secrets.exists():
        report.add(
            WARN,
            "youtube auth",
            f"no client secrets at {secrets.name} -- needed for --publish youtube "
            "(python -m tools.auth_youtube)",
        )
        return
    if not token.exists():
        report.add(
            WARN,
            "youtube auth",
            f"client secrets OK; run python -m tools.auth_youtube to create {token.name}",
        )
        return
    try:
        from google.oauth2.credentials import Credentials
        from .publish_engine import YOUTUBE_SCOPES

        creds = Credentials.from_authorized_user_file(str(token), YOUTUBE_SCOPES)
        if creds.valid or (creds.refresh_token and creds.expired):
            report.add(OK, "youtube auth", f"token at {token.name}")
        else:
            report.add(WARN, "youtube auth", "token invalid -- re-run python -m tools.auth_youtube")
    except Exception as exc:  # noqa: BLE001
        report.add(WARN, "youtube auth", f"could not read token: {exc}")


def _check_instagram_publish(cfg: Config, report: Report) -> None:
    token = cfg.meta_access_token
    user_id = cfg.instagram_user_id
    if not token or not user_id:
        report.add(
            WARN,
            "instagram auth",
            "META_ACCESS_TOKEN / INSTAGRAM_USER_ID not set -- needed for --publish instagram",
        )
        return
    report.add(OK, "instagram auth", f"user id {user_id}")

    missing = [
        name
        for name, val in [
            ("R2_ACCOUNT_ID", cfg.r2_account_id),
            ("R2_ACCESS_KEY_ID", cfg.r2_access_key_id),
            ("R2_SECRET_ACCESS_KEY", cfg.r2_secret_access_key),
            ("R2_BUCKET", cfg.r2_bucket),
            ("R2_PUBLIC_BASE_URL", cfg.r2_public_base_url),
        ]
        if not val
    ]
    if missing:
        report.add(
            WARN,
            "R2 hosting",
            f"missing {', '.join(missing)} -- Instagram needs a public video URL via R2",
        )
        return

    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=cfg.r2_access_key_id,
            aws_secret_access_key=cfg.r2_secret_access_key,
            region_name="auto",
            config=BotoConfig(signature_version="s3v4"),
        )
        client.head_bucket(Bucket=cfg.r2_bucket)
        report.add(OK, "R2 hosting", f"bucket {cfg.r2_bucket} reachable")
    except Exception as exc:  # noqa: BLE001
        report.add(WARN, "R2 hosting", f"could not reach bucket: {exc}")


def _check_network(cfg: Config, report: Report) -> None:
    if cfg.images.provider == "cloudflare" and cfg.cloudflare_account_id:
        # Hit the Workers AI run endpoint with an empty body — auth failures
        # come back as 401/403; a model validation 400 means the token works.
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{cfg.cloudflare_account_id}/ai/run/@cf/{cfg.images.model}"
        )
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {cfg.cloudflare_api_token}"},
                files={"prompt": (None, "ping")},
                timeout=30.0,
            )
            if resp.status_code in {401, 403}:
                report.add(FAIL, "network", f"cloudflare auth failed ({resp.status_code})")
            else:
                report.add(OK, "network", f"cloudflare Workers AI reachable ({resp.status_code})")
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            report.add(WARN, "network", f"could not reach cloudflare: {exc}")
        return
    try:
        resp = httpx.get("https://image.pollinations.ai/models", timeout=20.0)
        resp.raise_for_status()
        report.add(OK, "network", "pollinations reachable")
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        report.add(WARN, "network", f"could not reach pollinations: {exc}")


def run_checks(cfg: Config) -> int:
    report = Report()
    _check_ffmpeg(report)
    _check_font(cfg, report)
    _check_gemini(report)
    _check_images(cfg, report)
    _check_fal(cfg, report)
    _check_music(cfg, report)
    _check_youtube_publish(cfg, report)
    _check_instagram_publish(cfg, report)
    _check_network(cfg, report)

    print(report.render())
    if report.failed:
        print("\nFix the FAIL rows above before running the pipeline.")
        return 1
    print("\nReady. Try:  python -m src.main \"Akbar asks Birbal for an honest answer\" --no-motion")
    return 0
