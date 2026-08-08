"""Published-story history, used to guarantee stories never repeat.

The file is committed back to the repo by the GitHub Actions workflow so the
next run knows what has already aired.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List


class History:
    def __init__(self, path: str, max_entries: int = 90):
        self.path = Path(path)
        self.max_entries = max_entries
        self.entries: List[dict] = []

        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = data.get("entries", [])
            except (json.JSONDecodeError, OSError):
                # A corrupt history file must never block today's video.
                self.entries = []

    def recent_summaries(self, limit: int = 40) -> List[str]:
        """Short 'title - premise' lines to feed back into the story prompt."""
        lines = []
        for entry in self.entries[-limit:]:
            title = entry.get("title", "")
            premise = entry.get("premise", "")
            lines.append(f"{title} — {premise}" if premise else title)
        return [line for line in lines if line.strip()]

    def record(self, story: dict, video_format: str, youtube_id: str = "") -> None:
        self.entries.append(
            {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "format": video_format,
                "title": story.get("title", ""),
                "premise": story.get("premise", ""),
                "content_type": story.get("content_type", ""),
                "youtube_id": youtube_id,
            }
        )
        self.entries = self.entries[-self.max_entries :]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": self.entries}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
