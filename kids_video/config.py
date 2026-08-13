"""Configuration loading for the kids video pipeline.

config.yaml holds non-secret settings and is committed only as
config.yaml.example. Real API keys always come from environment variables
(GitHub Actions secrets), which override anything in the file.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

# env var -> config.api_keys key
ENV_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic",
    "KIE_API_TOKEN": "kie",
    "ELEVENLABS_API_KEY": "elevenlabs",
    "YOUTUBE_CLIENT_ID": "youtube_client_id",
    "YOUTUBE_CLIENT_SECRET": "youtube_client_secret",
    "YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
    "YOUTUBE_DATA_API_KEY": "youtube_data_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}

DEFAULTS: Dict[str, Any] = {
    "pipeline": {
        # One thoroughly made 10-minute explainer every fortnight, instead of a
        # daily Short. Shorts are still published, but cut from clips the long
        # video already paid for (see pipeline._cut_shorts).
        "short_days": [],
        "long_days": [6],  # Sunday (Python weekday(): Mon=0 .. Sun=6)
        "long_every_weeks": 2,  # 1 = weekly; 2 = alternate ISO weeks
        "language": "en",
        "target_audience": "curious adults",
        # Subject area rotation, indexed by Python weekday(). These are areas,
        # not topics: Claude picks a specific mechanism inside one each run.
        "content_rotation": [
            "how_everyday_machines_work",
            "physics_and_energy",
            "space_and_astronomy",
            "technology_and_computing",
            "biology_and_the_human_body",
            "medical_science",
            "engineering_and_infrastructure",
        ],
        "history_size": 90,
    },
    "short": {
        "shot_count": 8,
        "aspect_ratio": "9:16",
        "veo_model": "veo3_fast",
        "resolution": "1080p",
    },
    "long": {
        "shot_count": 75,  # 75 x 8s = ~10 minutes
        "aspect_ratio": "16:9",
        "veo_model": "veo3",  # Quality: $1.275/clip, ~$96 per video
        "resolution": "1080p",
    },
    "kie": {
        "base_url": "https://api.kie.ai/api/v1",
        "max_parallel_jobs": 5,
        "poll_interval_seconds": 10,
        "max_poll_seconds": 900,
        # kie.ai hard-rejects over 20 requests / 10s
        "submit_delay_seconds": 0.6,
        # Veo's safety filter blocks the same wholesome prompt at random. A
        # blocked request renders nothing and is not billed, so retry it.
        "clip_attempts": 3,
    },
    "voice": {
        "provider": "elevenlabs",
        "model_id": "eleven_multilingual_v2",
        # A single authoritative documentary narrator. An explainer channel
        # builds trust through a recognisable voice, so unlike a story channel
        # there is deliberately no per-weekday rotation.
        "voice_id": "nPczCjzI2devNBz1zQrb",  # Brian - deep, resonant, neutral US
        "rotation": [],
        "output_format": "mp3_44100_128",
        # Lower style and higher stability than storytelling: an explainer
        # should sound measured and consistent, not performed.
        "stability": 0.55,
        "similarity_boost": 0.8,
        "style": 0.2,
        "speed": 1.0,
    },
    # Captions burned into the picture. Shorts do not reliably surface a CC
    # track, so the text has to be part of the frame. Sizes and margins are
    # literal pixels at the target resolution (see assemble._write_ass).
    "subtitles": {
        "enabled": True,
        # libass falls back through fontconfig, so a missing font still renders.
        "font": "DejaVu Sans",
        "font_size": {"9:16": 62, "16:9": 50, "1:1": 54},
        # ASS colours are &HAABBGGRR - alpha first, then blue/green/red.
        "primary_colour": "&H00FFFFFF",  # opaque white
        "outline_colour": "&H00000000",
        "back_colour": "&HFF000000",  # fully transparent: no box behind the text
        # 1 = outline + drop shadow, so only the letters sit over the picture.
        # 3 would draw an opaque box and hide a third of the frame.
        "border_style": 1,
        "outline": 3,   # black stroke width around each glyph
        "shadow": 1,
        "margin_h": 120,
        # Clear of the Shorts UI overlay, but low in frame like a caption track.
        "margin_v": {"9:16": 300, "16:9": 70, "1:1": 100},
        # A whole 8-second line at once is a wall of text, so each shot's
        # narration is split into short cues timed across the clip.
        "words_per_cue": 5,
    },
    "youtube": {
        "category_id": "28",  # Science & Technology
        "privacy_status": "public",
        "made_for_kids": False,
        "default_language": "en",
    },
    # Human sign-off over Telegram, between the cheap script and expensive Veo
    # clips. No answer means no, because the run is unattended.
    "approval": {
        "enabled": True,
        "timeout_seconds": 3600,
    },
    "story": {
        "model": "claude-opus-5",
        # A 38-shot script on a reasoning model takes several minutes.
        "timeout_seconds": 900,
        # Regenerate when a shot comes back missing a field, which happens
        # occasionally on the 38-shot long format.
        "max_attempts": 3,
    },
    "paths": {
        "work_dir": "build",
        "history_file": "data/history.json",
    },
    "logging": {
        "level": "INFO",
        "file": "logs/pipeline.log",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_dotenv(path: Path) -> None:
    """Load KEY=value lines from a local .env into the environment.

    Convenience for running the CLI by hand; GitHub Actions injects real
    secrets directly, and .env is gitignored. A non-empty exported variable
    always wins, but a variable exported as an empty string does not, since
    a blank export carries no value and would otherwise mask the file.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not os.environ.get(key.strip(), "").strip():
            os.environ[key.strip()] = value.strip().strip("'\"")


class Config:
    def __init__(self, config_path: str = "config.yaml"):
        _load_dotenv(Path(".env"))
        file_data = {}
        path = Path(config_path)
        if path.exists():
            file_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        self.data = _deep_merge(DEFAULTS, file_data)

        api_keys = dict(self.data.get("api_keys") or {})
        for env_var, key in ENV_KEYS.items():
            value = os.environ.get(env_var, "").strip()
            if value:
                api_keys[key] = value
        self.data["api_keys"] = api_keys

        self.logger = self._setup_logging()

    def _setup_logging(self) -> logging.Logger:
        level = os.environ.get("LOG_LEVEL", self.get("logging.level", "INFO"))
        log_file = Path(self.get("logging.file", "logs/pipeline.log"))
        log_file.parent.mkdir(parents=True, exist_ok=True)

        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
        )
        return logging.getLogger("pipeline")

    def get(self, dotted_key: str, default=None):
        value = self.data
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return default if value is None else value

    def key(self, name: str) -> str:
        """Return an API key, or empty string if unset."""
        return (self.data.get("api_keys") or {}).get(name, "") or ""

    def require_key(self, name: str, env_var: str) -> str:
        value = self.key(name)
        if not value:
            raise RuntimeError(
                f"Missing API key '{name}'. Set the {env_var} environment variable "
                f"(GitHub Actions: add it under Settings > Secrets > Actions)."
            )
        return value

    @property
    def work_dir(self) -> Path:
        path = Path(self.get("paths.work_dir", "build"))
        path.mkdir(parents=True, exist_ok=True)
        return path
