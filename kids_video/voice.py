"""Narration via ElevenLabs.

Veo clips always come with their own generated audio (kie.ai exposes no toggle
to disable it), so the assembler strips that track and replaces it with this
narration.
"""

from datetime import date
from pathlib import Path
from typing import List, Optional

import requests

BASE_URL = "https://api.elevenlabs.io/v1"


class VoiceError(RuntimeError):
    pass


class Narrator:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("voice")
        self.voice_id = config.get("voice.voice_id")
        self.model_id = config.get("voice.model_id", "eleven_multilingual_v2")
        self.output_format = config.get("voice.output_format", "mp3_44100_128")
        self.language = config.get("pipeline.language", "en")

    @property
    def _headers(self) -> dict:
        key = self.config.require_key("elevenlabs", "ELEVENLABS_API_KEY")
        return {"xi-api-key": key, "Content-Type": "application/json"}

    def voice_for(self, day: date) -> str:
        """The narrator for a given day.

        A daily channel narrated by one voice starts to sound like the same
        episode repeated, so voice.rotation cycles a different storyteller per
        weekday. Leave the list empty to pin a single narrator instead.
        """
        rotation = self.config.get("voice.rotation") or []
        if not rotation:
            return self.voice_id
        return rotation[day.weekday() % len(rotation)]

    def narrate(
        self,
        text: str,
        destination: Path,
        label: str = "narration",
        voice_id: Optional[str] = None,
    ) -> Path:
        """Render one line of narration to an mp3 file."""
        body = {
            "text": text,
            "model_id": self.model_id,
            "language_code": self.language,
            # apply_language_text_normalization is deliberately not sent:
            # ElevenLabs rejects it with a 400 for some languages, including hi.
            "voice_settings": {
                "stability": self.config.get("voice.stability", 0.45),
                "similarity_boost": self.config.get("voice.similarity_boost", 0.75),
                "style": self.config.get("voice.style", 0.35),
                "use_speaker_boost": True,
                "speed": self.config.get("voice.speed", 1.0),
            },
        }
        resp = requests.post(
            f"{BASE_URL}/text-to-speech/{voice_id or self.voice_id}",
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

    def my_voices(self) -> List[dict]:
        """Voices actually on this account.

        Only these work for text-to-speech. A voice browsed in the shared
        library must be added to the account first, which is the usual cause
        of a 400 from narrate().
        """
        resp = requests.get(
            f"{BASE_URL}/voices",
            headers={"xi-api-key": self.config.require_key("elevenlabs", "ELEVENLABS_API_KEY")},
            timeout=60,
        )
        resp.raise_for_status()
        return [
            {"voice_id": v.get("voice_id"), "name": v.get("name"),
             "labels": v.get("labels", {})}
            for v in resp.json().get("voices", [])
        ]

    def list_library_voices(self, limit: int = 30) -> List[dict]:
        """Browse the shared library in this channel's language, for picking voice_id."""
        resp = requests.get(
            f"{BASE_URL}/shared-voices",
            headers={"xi-api-key": self.config.require_key("elevenlabs", "ELEVENLABS_API_KEY")},
            params={
                "language": self.language,
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
