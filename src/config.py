"""Config loading and shared filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
OUTPUT = ROOT / "output"


class StoryCfg(BaseModel):
    models: list[str] = [
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.0-flash",
        "gemini-flash-latest",
    ]
    scene_count: int = 8
    max_hindi_words: int = 90
    min_duration_s: float = 28.0
    max_duration_s: float = 40.0
    max_script_retries: int = 2
    art_style: str = ""
    # Relative to project root. short vs long use different story shapes.
    prompt_file: str = "prompts/story_system.md"
    # Appended to every image prompt so the model frames for the output ratio.
    aspect_label: str = "vertical 9:16 composition"


class AudioCfg(BaseModel):
    # gemini = Gemini TTS with edge-tts fallback; edge = edge-tts only.
    provider: str = "gemini"
    # Tried in order; first reachable Gemini TTS model is remembered.
    models: list[str] = [
        "gemini-3.1-flash-tts-preview",
        "gemini-2.5-flash-preview-tts",
        "gemini-2.5-pro-preview-tts",
    ]
    # Prebuilt Gemini voice. Sulafat = warm storyteller; Achird = friendly;
    # Kore = firm; Achernar = soft. Full list in Gemini TTS docs.
    voice: str = "Sulafat"
    language_code: str = "hi"
    # Director's note wrapped around each scene's Devanagari. Leave empty to
    # speak the narration with no style steering.
    style_prompt: str = (
        "Speak warmly and naturally as a Hindi YouTube storyteller. "
        "Conversational and engaging, not robotic. Recite exactly:"
    )
    # edge-tts fallback when Gemini TTS fails (quota, context window, outage).
    fallback_voice: str = "hi-IN-SwaraNeural"
    fallback_rate: str = "+20%"
    fallback_pitch: str = "+0Hz"
    scene_gap_s: float = 0.25

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        name = (value or "gemini").strip().lower()
        if name not in {"gemini", "edge"}:
            raise ValueError("audio.provider must be 'gemini' or 'edge'")
        return name


class ImagesCfg(BaseModel):
    # cloudflare (free, preferred) | pollinations (fallback)
    provider: str = "cloudflare"
    # Cloudflare: black-forest-labs/flux-2-klein-4b (true 9:16 via multipart)
    # Pollinations keyed: flux | Pollinations anonymous: dreamshaper
    model: str = "black-forest-labs/flux-2-klein-4b"
    anonymous_model: str = "dreamshaper"  # pollinations keyless only
    # klein supports custom dims; 768x1344 is a reliable free-tier 9:16 size.
    # ffmpeg scales these up to video.width x video.height on compose.
    width: int = 768
    height: int = 1344
    min_request_interval_s: float = 0.5
    max_retries: int = 4


class MotionCfg(BaseModel):
    enabled: bool = True
    provider: str = "fal"
    model: str = "fal-ai/wan-25-preview/image-to-video"
    resolution: str = "480p"
    clip_seconds: int = 5
    hero_scene_count: int = 4
    usd_per_second: float = 0.05
    max_cost_usd: float = 1.20
    poll_interval_s: float = 3.0
    timeout_s: float = 420.0


class VideoCfg(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    transition: str = "fade"
    transition_s: float = 0.3


class CaptionsCfg(BaseModel):
    font_file: str = "assets/fonts/Poppins-Bold.ttf"
    font_name: str = "Poppins"
    font_size: int = 84
    vertical_position: float = 0.56
    max_words_per_group: int = 4
    max_group_duration_s: float = 2.2
    primary_colour: str = "&H00FFFFFF&"
    highlight_colour: str = "&H0000E5FF&"
    outline_colour: str = "&H00000000&"
    outline: int = 5
    shadow: int = 2
    uppercase: bool = True


class MusicCfg(BaseModel):
    enabled: bool = True
    directory: str = "assets/music"
    volume_db: float = -25.0
    duck_db: float = -8.0


class YouTubePublishCfg(BaseModel):
    privacy: str = "public"  # public | unlisted | private
    category_id: str = "24"  # Entertainment
    default_tags: list[str] = [
        "Shorts",
        "HindiStories",
        "AkbarBirbal",
        "TenaliRaman",
        "moralstories",
    ]


class PublishCfg(BaseModel):
    brand_name: str = "Hindi Stories"
    # short → #Shorts metadata; long → standard YouTube upload (no #Shorts).
    format: str = "short"  # short | long
    youtube: YouTubePublishCfg = YouTubePublishCfg()

    @property
    def is_shorts(self) -> bool:
        return self.format.strip().lower() != "long"


class Config(BaseModel):
    story: StoryCfg = StoryCfg()
    audio: AudioCfg = AudioCfg()
    images: ImagesCfg = ImagesCfg()
    motion: MotionCfg = MotionCfg()
    video: VideoCfg = VideoCfg()
    captions: CaptionsCfg = CaptionsCfg()
    music: MusicCfg = MusicCfg()
    publish: PublishCfg = PublishCfg()

    # --- secrets, from .env rather than config.yaml ---
    @property
    def gemini_api_key(self) -> str:
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add a key "
                "from https://aistudio.google.com/apikey (free tier is enough)."
            )
        return key

    @property
    def pollinations_token(self) -> str | None:
        return os.getenv("POLLINATIONS_TOKEN", "").strip() or None

    @property
    def cloudflare_account_id(self) -> str | None:
        return os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() or None

    @property
    def cloudflare_api_token(self) -> str | None:
        return os.getenv("CLOUDFLARE_API_TOKEN", "").strip() or None

    @property
    def fal_key(self) -> str | None:
        return os.getenv("FAL_KEY", "").strip() or None

    def _env_path(self, key: str, default: str) -> Path:
        raw = os.getenv(key, default).strip() or default
        path = Path(raw)
        return path if path.is_absolute() else ROOT / path

    def youtube_client_secrets(self) -> Path:
        return self._env_path("YOUTUBE_CLIENT_SECRETS", "secrets/youtube_client_secret.json")

    def youtube_token_path(self) -> Path:
        return self._env_path("YOUTUBE_TOKEN_PATH", "secrets/youtube_token.json")

    def font_path(self) -> Path:
        return ROOT / self.captions.font_file

    def music_dir(self) -> Path:
        return ROOT / self.music.directory


def resolve_config_path(profile: str | None = None, path: Path | None = None) -> Path:
    """Map --profile / --config to a yaml file under the project root."""
    if path is not None:
        return path if path.is_absolute() else ROOT / path
    name = (profile or "short").strip().lower()
    if name in {"", "short", "shorts", "default"}:
        return ROOT / "config.yaml"
    candidate = ROOT / f"config.{name}.yaml"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Unknown profile {profile!r}: expected {candidate.name} next to config.yaml"
        )
    return candidate


def load_config(path: Path | None = None, profile: str | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = resolve_config_path(profile=profile, path=path)
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)

    # Keyed Pollinations does not throttle secret keys; keep a polite gap.
    if (
        cfg.images.provider == "pollinations"
        and cfg.pollinations_token
        and cfg.images.min_request_interval_s > 1.0
    ):
        cfg.images.min_request_interval_s = 1.0
    return cfg


def work_dir(slug: str) -> Path:
    d = WORK / slug
    d.mkdir(parents=True, exist_ok=True)
    return d
