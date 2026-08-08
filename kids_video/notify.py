"""Telegram notifications. Never allowed to fail the pipeline."""

from typing import List, Optional

import requests


class TelegramNotifier:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("notify")
        self.bot_token = config.key("telegram_bot_token")
        self.chat_id = config.key("telegram_chat_id")

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def success(self, story: dict, video_format: str, youtube_id: str, cost: float) -> None:
        label = "5 मिनट की कहानी" if video_format == "long" else "60 सेकंड Short"
        self._send(
            f"✅ *{label} publish हो गई!*\n\n"
            f"📝 {story.get('title', '')}\n"
            f"💡 {story.get('moral', '')}\n"
            f"🎬 {len(story.get('shots', []))} shots\n"
            f"💰 ~${cost:.2f}\n\n"
            f"📺 https://youtu.be/{youtube_id}"
        )

    def failure(self, video_format: str, error: str) -> None:
        self._send(
            f"❌ *{video_format} video failed*\n\n"
            f"💥 `{error[:500]}`\n\n"
            f"Check the workflow logs."
        )

    def dry_run(self, story: dict, video_format: str, cost: float) -> None:
        shots: List[dict] = story.get("shots", [])
        preview = "\n".join(
            f"  {shot['shot_id']}: {shot['narration_hi'][:60]}" for shot in shots[:3]
        )
        self._send(
            f"🧪 *Dry run — no credits spent*\n\n"
            f"📝 {story.get('title', '')}\n"
            f"🎬 {len(shots)} shots ({video_format})\n"
            f"💰 would cost ~${cost:.2f}\n\n"
            f"{preview}"
        )

    def _send(self, text: str) -> None:
        if not self.enabled:
            self.logger.info("Telegram not configured; skipping notification")
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=30,
            )
        except requests.RequestException as exc:
            self.logger.warning("Telegram notification failed: %s", exc)
