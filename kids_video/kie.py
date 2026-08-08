"""kie.ai client: Pixar-style stills (Jobs API) and Veo 3.1 clips (Veo API).

These are two genuinely different APIs and must not share a code path:

  Jobs API  POST /jobs/createTask   body {"model", "input": {...}}
            GET  /jobs/recordInfo   -> data.state (string), data.resultJson (JSON *string*)

  Veo API   POST /veo/generate      flat body, no "input" wrapper
            GET  /veo/record-info   -> data.successFlag (int), data.response.resultUrls (array)

Generated media is deleted by kie.ai after 14 days, which is fine because
everything is downloaded within the same run.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import requests


class KieError(RuntimeError):
    pass


class KieClient:
    def __init__(self, config):
        self.config = config
        self.logger = config.logger.getChild("kie")
        self.base_url = config.get("kie.base_url", "https://api.kie.ai/api/v1").rstrip("/")
        self.poll_interval = config.get("kie.poll_interval_seconds", 10)
        self.max_poll_seconds = config.get("kie.max_poll_seconds", 900)
        self.max_workers = config.get("kie.max_parallel_jobs", 5)
        self.submit_delay = config.get("kie.submit_delay_seconds", 0.6)
        # kie.ai hard-rejects bursts over 20 requests / 10s, so submissions are
        # spaced out behind a lock rather than fired all at once.
        self._submit_lock = threading.Lock()

    @property
    def _headers(self) -> dict:
        token = self.config.require_key("kie", "KIE_API_TOKEN")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict) -> dict:
        with self._submit_lock:
            time.sleep(self.submit_delay)
            resp = requests.post(
                f"{self.base_url}{path}", headers=self._headers, json=payload, timeout=60
            )
        if resp.status_code == 429:
            raise KieError("kie.ai rate limit hit (429); request was rejected, not queued")
        if resp.status_code >= 400:
            raise KieError(f"kie.ai {path} returned {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("code") != 200:
            raise KieError(f"kie.ai {path} error: {body.get('msg', body)}")
        return body.get("data", {})

    def _poll(self, describe: str, fetch: Callable[[], Optional[str]]) -> str:
        """Repeatedly call fetch() until it returns a URL or the deadline passes."""
        deadline = time.time() + self.max_poll_seconds
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            url = fetch()
            if url:
                return url
        raise KieError(f"{describe}: timed out after {self.max_poll_seconds}s")

    # ── Jobs API: images ──────────────────────────────────────────────────

    def generate_image(self, model: str, input_payload: dict, label: str = "image") -> str:
        data = self._post("/jobs/createTask", {"model": model, "input": input_payload})
        task_id = data.get("taskId")
        if not task_id:
            raise KieError(f"{label}: no taskId in create response")
        self.logger.info("%s submitted (%s, task %s)", label, model, task_id[:12])

        def fetch() -> Optional[str]:
            resp = requests.get(
                f"{self.base_url}/jobs/recordInfo",
                headers=self._headers,
                params={"taskId": task_id},
                timeout=30,
            )
            resp.raise_for_status()
            info = resp.json().get("data", {})
            state = (info.get("state") or "").lower()
            if state == "success":
                result = info.get("resultJson") or "{}"
                if isinstance(result, str):
                    result = json.loads(result)
                urls = result.get("resultUrls") or []
                if not urls:
                    raise KieError(f"{label}: success but no resultUrls")
                return urls[0]
            if state == "fail":
                raise KieError(f"{label} failed: {info.get('failMsg', 'unknown reason')}")
            return None

        url = self._poll(label, fetch)
        self.logger.info("%s ready", label)
        return url

    # ── Veo API: video clips ──────────────────────────────────────────────

    def generate_clip(
        self,
        prompt: str,
        aspect_ratio: str,
        model: str = "veo3_fast",
        resolution: str = "1080p",
        reference_urls: Optional[Sequence[str]] = None,
        label: str = "clip",
    ) -> str:
        payload = {
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,  # snake_case here, camelCase elsewhere
            "resolution": resolution,
            "duration": 8,
            "enableTranslation": False,  # prompts are already English
        }
        if reference_urls:
            # REFERENCE_2_VIDEO keeps the character identical across clips.
            # It accepts 1-3 images and only runs on veo3_fast / veo3_lite.
            payload["generationType"] = "REFERENCE_2_VIDEO"
            payload["imageUrls"] = list(reference_urls)[:3]
            if model == "veo3":
                self.logger.warning(
                    "veo3 (Quality) does not support reference images; using veo3_fast"
                )
                payload["model"] = "veo3_fast"
        else:
            payload["generationType"] = "TEXT_2_VIDEO"

        data = self._post("/veo/generate", payload)
        task_id = data.get("taskId")
        if not task_id:
            raise KieError(f"{label}: no taskId in Veo create response")
        self.logger.info("%s submitted (task %s)", label, task_id[:12])

        def fetch() -> Optional[str]:
            resp = requests.get(
                f"{self.base_url}/veo/record-info",
                headers=self._headers,
                params={"taskId": task_id},
                timeout=30,
            )
            resp.raise_for_status()
            info = resp.json().get("data", {})
            flag = info.get("successFlag")
            if flag == 1:
                urls = (info.get("response") or {}).get("resultUrls") or []
                if not urls:
                    raise KieError(f"{label}: successFlag=1 but no resultUrls")
                return urls[0]
            if flag in (2, 3):
                raise KieError(
                    f"{label} failed: {info.get('errorMessage') or 'generation failed'}"
                )
            return None

        url = self._poll(label, fetch)
        self.logger.info("%s ready", label)
        return url

    # ── Parallel helper ───────────────────────────────────────────────────

    def run_parallel(self, tasks: List[Callable[[], None]]) -> List[BaseException]:
        """Run tasks concurrently, returning whatever exceptions occurred."""
        errors: List[BaseException] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 - reported to caller
                    errors.append(exc)
        return errors


def download(url: str, destination: Path, timeout: int = 300) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(destination, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                handle.write(chunk)
    if destination.stat().st_size == 0:
        raise KieError(f"Downloaded an empty file from {url}")
    return destination
