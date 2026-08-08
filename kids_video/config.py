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
    "IMGBB_API_KEY": "imgbb",
    "ELEVENLABS_API_KEY": "elevenlabs",
    "HEYGEN_API_KEY": "heygen",
    "YOUTUBE_CLIENT_ID": "youtube_client_id",
    "YOUTUBE_CLIENT_SECRET": "youtube_client_secret",
    "YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
    "YOUTUBE_DATA_API_KEY": "youtube_data_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}

DEFAULTS: Dict[str, Any] = {
    "pipeline": {
        # Which format runs on which weekday. Long stories are expensive, so
        # they run weekly; Shorts run every day.
        "short_days": [0, 1, 2, 3, 4, 5, 6],
        "long_days": [6],  # Sunday (Python weekday(): Mon=0 .. Sun=6)
        "language": "hi",
        "target_age_group": "4-8",
        # Content type rotation, indexed by Python weekday()
        "content_rotation": [
            "adventure_story",
            "moral_story",
            "fun_facts",
            "folk_tale",
            "science_wonder",
            "friendship_story",
            "magical_story",
        ],
        "history_size": 90,
    },
    "short": {
        "shot_count": 8,
        "friends_per_episode": 1,  # 8 shots has no room for a crowd
        "aspect_ratio": "9:16",
        "veo_model": "veo3_fast",
        "resolution": "1080p",
    },
    "long": {
        "shot_count": 38,
        "aspect_ratio": "16:9",
        "veo_model": "veo3_fast",
        "resolution": "1080p",
    },
    "kie": {
        "base_url": "https://api.kie.ai/api/v1",
        "character_image_model": "nano-banana-pro",
        "background_image_model": "seedream/5-pro-image-to-image",
        "max_parallel_jobs": 5,
        "poll_interval_seconds": 10,
        "max_poll_seconds": 900,
        # kie.ai hard-rejects over 20 requests / 10s
        "submit_delay_seconds": 0.6,
    },
    "voice": {
        "provider": "elevenlabs",
        "model_id": "eleven_multilingual_v2",
        "voice_id": "3AMU7jXQuQa3oRvRqUmb",  # Viraj - Hindi storyteller
        "output_format": "mp3_44100_128",
        "stability": 0.45,
        "similarity_boost": 0.75,
        "style": 0.35,
        "speed": 1.0,
    },
    "mascot": {
        # HeyGen v3 lip-synced 3D character for the intro/outro
        "enabled": True,
        "avatar_id": "",  # set after creating an avatar; blank disables
        "voice_id": "",  # 32-char hex from GET /v3/voices?language=Hindi
        "engine": "avatar_iv",
        "expressiveness": "high",
        "base_url": "https://api.heygen.com/v3",
        "poll_interval_seconds": 15,
        "max_poll_seconds": 1800,
    },
    "youtube": {
        "category_id": "27",  # Education
        "privacy_status": "public",
        "made_for_kids": True,
        "default_language": "hi",
    },
    # The permanent cast. Reference sheets are rendered once by `setup-cast`
    # and pinned in data/cast.json; after that these descriptions must not
    # change or the characters would drift. Extra cast members cost nothing per
    # episode (a Veo clip is the same price with one character or four), only a
    # few cents once for the reference sheet.
    "cast": {
        "chintu": {
            "name": "चिंटू",
            "role": "host",
            "catchphrase": "चलो, कहानी शुरू करते हैं!",
            "description": (
                "a cheerful 7-year-old Indian boy, round friendly face, warm brown "
                "skin, big expressive dark eyes, neat black hair with a small "
                "cowlick at the crown, wearing a bright yellow kurta with small "
                "orange embroidery, blue cotton pyjama trousers, brown sandals, "
                "a red thread bracelet on his right wrist, short slightly chubby build"
            ),
        },
        "mithu": {
            "name": "मिट्ठू",
            "role": "friend",
            "catchphrase": "मिट्ठू सब जानता है!",
            "description": (
                "a small chatty rose-ringed parakeet with brilliant emerald green "
                "feathers, a curved coral-red beak, a thin pink neck ring, bright "
                "amber eyes, and one cheeky tuft of feathers sticking up on his head"
            ),
        },
        "gullu": {
            "name": "गुल्लू",
            "role": "friend",
            "catchphrase": "गुल्लू मदद करेगा!",
            "description": (
                "a gentle baby Indian elephant, soft dusty grey wrinkled skin, "
                "oversized floppy ears, long curious trunk, kind hazel eyes with "
                "long lashes, a small string of orange marigold flowers around his "
                "neck, chubby and slightly clumsy"
            ),
        },
        "pari": {
            "name": "परी",
            "role": "friend",
            "catchphrase": "मुझे पता चल गया!",
            "description": (
                "a clever 6-year-old Indian girl, warm brown skin, two neat black "
                "braids tied with red ribbons, round wire spectacles, wearing a "
                "teal salwar kameez with tiny white polka dots and white canvas "
                "shoes, always carrying a small cloth notebook"
            ),
        },
        "bhola": {
            "name": "भोला",
            "role": "friend",
            "catchphrase": "भौं भौं!",
            "description": (
                "a scruffy friendly Indian street dog, sandy golden-brown fur with "
                "a white patch over one eye, one ear permanently flopped over, a "
                "long wagging tail, a faded blue collar, big trusting brown eyes"
            ),
        },
    },
    "story": {
        "model": "claude-opus-4-6",
        # Recurring friends featured per episode, on top of the host.
        "friends_per_episode": 2,
    },
    "paths": {
        "work_dir": "build",
        "history_file": "data/history.json",
        "cast_file": "data/cast.json",
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


class Config:
    def __init__(self, config_path: str = "config.yaml"):
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
