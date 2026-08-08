"""YouTube upload via the Data API v3 resumable protocol.

Resumable rather than multipart because a 5-minute 1080p story is far too large
to push in a single request reliably.
"""

import json
import time
from pathlib import Path
from typing import List, Optional

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB


class UploadError(RuntimeError):
    pass


class YouTubeUploader:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("youtube")
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 120:
            return self._access_token

        self.logger.info("Refreshing YouTube access token")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.config.require_key("youtube_client_id", "YOUTUBE_CLIENT_ID"),
                "client_secret": self.config.require_key(
                    "youtube_client_secret", "YOUTUBE_CLIENT_SECRET"
                ),
                "refresh_token": self.config.require_key(
                    "youtube_refresh_token", "YOUTUBE_REFRESH_TOKEN"
                ),
                "grant_type": "refresh_token",
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            raise UploadError(
                f"Token refresh failed ({resp.status_code}): {resp.text[:300]}. "
                "The refresh token may have been revoked; re-run the OAuth consent flow."
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + payload.get("expires_in", 3600)
        return self._access_token

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: List[str],
        thumbnail_path: Optional[Path] = None,
    ) -> str:
        if not video_path.exists():
            raise UploadError(f"Video file not found: {video_path}")

        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": _clean_tags(tags),
                "categoryId": str(self.config.get("youtube.category_id", "27")),
                "defaultLanguage": self.config.get("youtube.default_language", "hi"),
                "defaultAudioLanguage": self.config.get("youtube.default_language", "hi"),
            },
            "status": {
                "privacyStatus": self.config.get("youtube.privacy_status", "public"),
                "selfDeclaredMadeForKids": bool(
                    self.config.get("youtube.made_for_kids", True)
                ),
            },
        }

        size = video_path.stat().st_size
        self.logger.info("Uploading %s (%.1f MB)", video_path.name, size / 1e6)

        session_url = self._start_session(metadata, size)
        video_id = self._upload_chunks(session_url, video_path, size)

        self.logger.info("Published: https://youtu.be/%s", video_id)
        if thumbnail_path and thumbnail_path.exists():
            self._set_thumbnail(video_id, thumbnail_path)
        return video_id

    def _start_session(self, metadata: dict, size: int) -> str:
        resp = requests.post(
            UPLOAD_URL,
            params={"part": "snippet,status", "uploadType": "resumable"},
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            data=json.dumps(metadata).encode("utf-8"),
            timeout=60,
        )
        if resp.status_code >= 400:
            raise UploadError(
                f"Could not start upload session ({resp.status_code}): {resp.text[:300]}"
            )
        session_url = resp.headers.get("Location")
        if not session_url:
            raise UploadError("YouTube did not return a resumable session URL")
        return session_url

    def _upload_chunks(self, session_url: str, video_path: Path, size: int) -> str:
        uploaded = 0
        with open(video_path, "rb") as handle:
            while uploaded < size:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                end = uploaded + len(chunk) - 1
                resp = requests.put(
                    session_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {uploaded}-{end}/{size}",
                    },
                    data=chunk,
                    timeout=600,
                )
                # 308 means "chunk accepted, keep going".
                if resp.status_code == 308:
                    uploaded = end + 1
                    self.logger.info("  %.0f%% uploaded", 100 * uploaded / size)
                    continue
                if resp.status_code in (200, 201):
                    return resp.json()["id"]
                raise UploadError(
                    f"Chunk upload failed ({resp.status_code}): {resp.text[:300]}"
                )
        raise UploadError("Upload finished without YouTube returning a video id")

    def _set_thumbnail(self, video_id: str, thumbnail_path: Path) -> None:
        resp = requests.post(
            THUMBNAIL_URL,
            params={"videoId": video_id},
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "image/jpeg",
            },
            data=thumbnail_path.read_bytes(),
            timeout=120,
        )
        if resp.status_code >= 400:
            # A missing custom thumbnail is cosmetic; never fail the run for it.
            self.logger.warning(
                "Thumbnail upload skipped (%s): %s", resp.status_code, resp.text[:200]
            )


def _clean_tags(tags: List[str]) -> List[str]:
    """YouTube rejects the whole request if the tag list exceeds 500 characters."""
    result: List[str] = []
    budget = 480
    for tag in tags:
        tag = tag.strip().lstrip("#")
        if not tag or tag in result:
            continue
        if len(tag) + 1 > budget:
            break
        result.append(tag)
        budget -= len(tag) + 1
    return result
