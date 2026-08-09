"""Telegram notifications, the human approval gate, and on-demand requests.

Notifications are never allowed to fail the pipeline. The approval gate is the
one exception: it is a deliberate spend control, so if it cannot reach you it
refuses rather than guesses.

Telegram's getUpdates is destructive - acknowledging an update deletes it - so
every read routes through _absorb(), which parks any /make request in memory.
Otherwise a topic typed while a build was waiting for approval would vanish.
"""

import time
from typing import List, Optional

import requests

API = "https://api.telegram.org/bot{token}/{method}"

REQUEST_COMMAND = "/make"


class TelegramNotifier:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("notify")
        self.bot_token = config.key("telegram_bot_token")
        self.chat_id = config.key("telegram_chat_id")
        self._requests: List[dict] = []

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def success(self, story: dict, video_format: str, youtube_id: str, cost: float) -> None:
        label = "5-minute explainer" if video_format == "long" else "60s Short"
        self._send(
            f"✅ *{label} published*\n\n"
            f"📝 {story.get('title', '')}\n"
            f"🔬 {story.get('subject', '')}\n"
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
            f"  {shot['shot_id']}: {shot['subtitle_en'][:70]}" for shot in shots[:3]
        )
        self._send(
            f"🧪 *Dry run — no credits spent*\n\n"
            f"📝 {story.get('title', '')}\n"
            f"🔬 {story.get('subject', '')}\n"
            f"🎬 {len(shots)} shots ({video_format})\n"
            f"💰 would cost ~${cost:.2f}\n\n"
            f"{preview}"
        )

    # ── Approval gate ─────────────────────────────────────────────────────

    def request_approval(
        self, story: dict, video_format: str, cost: float, timeout_seconds: int
    ) -> bool:
        """Ask for approval before spending, and block until answered.

        Returns True only on an explicit approval. A timeout means no, because
        the run is unattended and the alternative is spending money nobody
        agreed to.
        """
        if not self.enabled:
            self.logger.warning(
                "Approval required but Telegram is not configured; refusing to spend"
            )
            return False

        label = "5-minute explainer" if video_format == "long" else "60s Short"
        text = (
            f"🎬 *Approve this {label}?*\n\n"
            f"📝 {story.get('title', '')}\n\n"
            f"🔬 *Subject*\n{story.get('subject', '')}\n\n"
            f"📄 *Description*\n{story.get('youtube', {}).get('description', '')}\n\n"
            f"🎞 {len(story.get('shots', []))} shots · 💰 ~${cost:.2f}\n\n"
            f"_Nothing is spent until you approve. "
            f"No answer in {timeout_seconds // 60} min = skipped._"
        )

        # Consume anything already queued so a stale button press from an
        # earlier run cannot approve this one.
        offset = self._drain_updates()
        message_id = self._send(
            text,
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": "approve"},
                    {"text": "❌ Skip", "callback_data": "reject"},
                ]]
            },
        )
        if message_id is None:
            self.logger.warning("Could not send the approval request; refusing to spend")
            return False

        self.logger.info(
            "Waiting up to %d min for Telegram approval", timeout_seconds // 60
        )
        decision = self._await_decision(offset, timeout_seconds)

        if decision is True:
            self._send("✅ Approved — generating now.")
        elif decision is False:
            self._send("❌ Skipped. Nothing was spent.")
        else:
            self._send("⌛️ No answer in time — skipped. Nothing was spent.")
        return decision is True

    def _await_decision(self, offset: Optional[int], timeout_seconds: int) -> Optional[bool]:
        """Poll for a button press or a typed yes/no. None means it timed out."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Telegram long-polls, so this loop is nearly free.
            updates = self._get_updates(offset, poll_seconds=30)
            for update in updates:
                offset = update["update_id"] + 1
                answer = _read_answer(update)
                if answer is not None:
                    if "callback_query" in update:
                        self._ack_callback(update["callback_query"]["id"])
                    return answer
            if not updates:
                time.sleep(1)
        return None

    def _get_updates(self, offset: Optional[int], poll_seconds: int) -> List[dict]:
        params = {"timeout": poll_seconds, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.post(
                API.format(token=self.bot_token, method="getUpdates"),
                json=params,
                timeout=poll_seconds + 15,
            )
            updates = resp.json().get("result", []) if resp.ok else []
        except requests.RequestException as exc:
            self.logger.warning("Telegram poll failed: %s", exc)
            time.sleep(5)
            return []
        for update in updates:
            self._absorb(update)
        return updates

    def _drain_updates(self) -> Optional[int]:
        """Return the offset just past everything currently queued."""
        pending = self._get_updates(None, poll_seconds=0)
        return pending[-1]["update_id"] + 1 if pending else None

    # ── On-demand topic requests ──────────────────────────────────────────

    def poll_requests(self) -> List[dict]:
        """Read anything waiting in the chat and return the /make requests."""
        if not self.enabled:
            self.logger.info("Telegram not configured; no requests to poll")
            return []
        offset = self._drain_updates()
        # Acknowledge, so the same request is not picked up by the next run.
        if offset is not None:
            self._get_updates(offset, poll_seconds=0)
        return self.take_requests()

    def take_requests(self) -> List[dict]:
        """Hand over every request seen so far and forget them."""
        collected, self._requests = self._requests, []
        return collected

    def _absorb(self, update: dict) -> None:
        parsed = _read_request(update)
        if not parsed:
            return
        # Only the configured chat can spend money.
        sender = str(update.get("message", {}).get("chat", {}).get("id", ""))
        if sender != str(self.chat_id):
            self.logger.warning("Ignoring /make from unknown chat %s", sender)
            return
        self.logger.info("Queued request: %s", parsed["topic"])
        self._requests.append(parsed)

    def _ack_callback(self, callback_id: str) -> None:
        """Stop Telegram showing a loading spinner on the pressed button."""
        try:
            requests.post(
                API.format(token=self.bot_token, method="answerCallbackQuery"),
                json={"callback_query_id": callback_id},
                timeout=15,
            )
        except requests.RequestException:
            pass

    # ── Transport ─────────────────────────────────────────────────────────

    def _send(self, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
        if not self.enabled:
            self.logger.info("Telegram not configured; skipping notification")
            return None
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            resp = requests.post(
                API.format(token=self.bot_token, method="sendMessage"),
                json=payload,
                timeout=30,
            )
            if not resp.ok:
                self.logger.warning("Telegram send failed: %s", resp.text[:200])
                return None
            return resp.json()["result"]["message_id"]
        except requests.RequestException as exc:
            self.logger.warning("Telegram notification failed: %s", exc)
            return None


def _read_request(update: dict) -> Optional[dict]:
    """Parse '/make <topic> | <description>' out of a chat message.

    The description is optional; without it the topic alone steers the script.
    A trailing 'short' or 'long' word picks the format.
    """
    text = (update.get("message", {}).get("text") or "").strip()
    if not text.lower().startswith(REQUEST_COMMAND):
        return None
    body = text[len(REQUEST_COMMAND):].strip()
    if not body:
        return None

    video_format = "short"
    if body.lower().startswith("long "):
        video_format, body = "long", body[5:].strip()
    elif body.lower().startswith("short "):
        body = body[6:].strip()

    topic, _, description = body.partition("|")
    return {
        "topic": topic.strip(),
        "description": description.strip(),
        "format": video_format,
    } if topic.strip() else None


def _read_answer(update: dict) -> Optional[bool]:
    """True/False if this update is a decision, None if it is unrelated chatter."""
    if "callback_query" in update:
        data = update["callback_query"].get("data", "")
        if data in ("approve", "reject"):
            return data == "approve"
        return None
    word = (update.get("message", {}).get("text") or "").strip().lower()
    if word in ("yes", "y", "ok", "approve", "haan", "ha", "/approve"):
        return True
    if word in ("no", "n", "skip", "reject", "nahi", "/skip"):
        return False
    return None
