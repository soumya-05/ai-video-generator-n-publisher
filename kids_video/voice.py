"""Hindi narration via ElevenLabs.

Veo clips always come with their own generated audio (kie.ai exposes no toggle
to disable it), so the assembler strips that track and replaces it with this
narration.
"""

from pathlib import Path
from typing import List, Optional

import requests

BASE_URL = "https://api.elevenlabs.io/v1"


class VoiceError(RuntimeError):
    pass


class HindiNarrator:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("voice")
        self.voice_id = config.get("voice.voice_id")
        self.model_id = config.get("voice.model_id", "eleven_multilingual_v2")
        self.output_format = config.get("voice.output_format", "mp3_44100_128")

    @property
    def _headers(self) -> dict:
        key = self.config.require_key("elevenlabs", "ELEVENLABS_API_KEY")
        return {"xi-api-key": key, "Content-Type": "application/json"}

    def narrate(self, text: str, destination: Path, label: str = "narration") -> Path:
        """Render one line of Hindi narration to an mp3 file."""
        body = {
            "text": text,
            "model_id": self.model_id,
            "language_code": "hi",
            # Off by default, but it materially improves how numbers and
            # abbreviations are spoken in non-English languages.
            "apply_language_text_normalization": True,
            "voice_settings": {
                "stability": self.config.get("voice.stability", 0.45),
                "similarity_boost": self.config.get("voice.similarity_boost", 0.75),
                "style": self.config.get("voice.style", 0.35),
                "use_speaker_boost": True,
                "speed": self.config.get("voice.speed", 1.0),
            },
        }
        resp = requests.post(
            f"{BASE_URL}/text-to-speech/{self.voice_id}",
            headers=self._headers,
            params={"output_format": self.output_format},
            json=body,
            timeout=180,
        )
        if resp.status_code >= 400:
            raise VoiceError(
                f"ElevenLabs {resp.status_code} for {label}: {resp.text[:300]}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(resp.content)
        if destination.stat().st_size == 0:
            raise VoiceError(f"ElevenLabs returned empty audio for {label}")
        return destination

    def list_hindi_voices(self, limit: int = 30) -> List[dict]:
        """Browse Hindi voices from the shared library, for picking voice_id."""
        resp = requests.get(
            f"{BASE_URL}/shared-voices",
            headers={"xi-api-key": self.config.require_key("elevenlabs", "ELEVENLABS_API_KEY")},
            params={
                "language": "hi",
                "use_cases": "narrative_story",
                "sort": "trending",
                "page_size": limit,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return [
            {
                "voice_id": voice.get("voice_id"),
                "name": voice.get("name"),
                "gender": voice.get("gender"),
                "accent": voice.get("accent"),
                "description": voice.get("descriptive"),
                "preview_url": voice.get("preview_url"),
            }
            for voice in resp.json().get("voices", [])
        ]
