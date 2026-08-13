"""Daily pipeline orchestrator.

Flow per video:
    trend signals -> explainer script (Claude) -> Telegram approval
    -> narration (ElevenLabs) -> stylised 8s clips (Veo 3.1)
    -> ffmpeg assembly with burned-in English subtitles -> YouTube

The approval gate sits after the script (a few cents) and before Veo (dollars),
so a subject you do not want costs almost nothing to reject.
"""

import json
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from . import assemble
from .history import History
from .kie import KieClient, KieError, download
from .notify import TelegramNotifier
from .story import StoryGenerator
from .trends import TrendSignals
from .voice import Narrator
from .youtube import YouTubeUploader

# USD per 8-second 1080p clip, from kie.ai pricing (1 credit = $0.005).
CLIP_COST = {"veo3_lite": 0.175, "veo3_fast": 0.325, "veo3": 1.275}
NARRATION_COST_PER_1K_CHARS = 0.10

# A Short is short enough that losing a shot is obvious; a long explainer can
# absorb a couple of gaps rather than throwing the whole render away.
MAX_FAILED_SHOT_RATIO = {"short": 0.0, "long": 0.1}

# Shots in a chain open on the previous shot's last frame, which is what stops
# the subject being redrawn from scratch every 8 seconds. That forces them to be
# generated one after another, so chains are capped: a long chain would serialise
# the whole render, and a scene change is a natural place to break continuity
# anyway. 75 shots at this cap is 15 chains, which still saturates the pool.
MAX_CHAIN_LENGTH = 5


def _chain_shots(shots: List[dict]) -> List[List[dict]]:
    """Group consecutive shots that share a background into continuity chains."""
    chains: List[List[dict]] = []
    current: List[dict] = []
    for shot in shots:
        continues = (
            current
            and shot.get("background") == current[-1].get("background")
            and len(current) < MAX_CHAIN_LENGTH
        )
        if not continues and current:
            chains.append(current)
            current = []
        current.append(shot)
    if current:
        chains.append(current)
    return chains


@dataclass
class VideoResult:
    video_format: str
    success: bool
    story: Optional[dict] = None
    video_path: Optional[Path] = None
    youtube_id: Optional[str] = None
    estimated_cost: float = 0.0
    failed_shots: List[str] = field(default_factory=list)
    error: Optional[str] = None


class Pipeline:
    def __init__(self, config, dry_run: bool = False, skip_upload: bool = False):
        self.config = config
        self.logger = config.logger
        self.dry_run = dry_run
        self.skip_upload = skip_upload

        self.kie = KieClient(config)
        self.trends = TrendSignals(config)
        self.stories = StoryGenerator(config)
        self.narrator = Narrator(config)
        self.uploader = YouTubeUploader(config)
        self.notifier = TelegramNotifier(config)
        self.history = History(
            config.get("paths.history_file", "data/history.json"),
            config.get("pipeline.history_size", 90),
        )

    # ── Scheduling ────────────────────────────────────────────────────────

    def formats_for(self, today: date) -> List[str]:
        """Which video formats are due today."""
        weekday = today.weekday()
        formats = []
        if weekday in self.config.get("pipeline.short_days", []):
            formats.append("short")
        if weekday in self.config.get("pipeline.long_days", []):
            # An expensive video can run less often than weekly. The ISO week
            # number keeps the cadence stable across year boundaries.
            every = self.config.get("pipeline.long_every_weeks", 1)
            if every <= 1 or today.isocalendar()[1] % every == 0:
                formats.append("long")
        return formats

    def content_type_for(self, today: date) -> str:
        rotation = self.config.get("pipeline.content_rotation", ["science"])
        return rotation[today.weekday() % len(rotation)]

    # ── Entry point ───────────────────────────────────────────────────────

    def run(self, today: Optional[date] = None, only: Optional[str] = None) -> List[VideoResult]:
        today = today or date.today()
        formats = [only] if only else self.formats_for(today)

        if not formats:
            self.logger.info("Nothing scheduled for %s", today.isoformat())
            return []

        self.logger.info(
            "=== %s | formats: %s | dry_run=%s ===",
            today.isoformat(), ", ".join(formats), self.dry_run,
        )

        results = []
        for video_format in formats:
            results.append(self._guarded(video_format, today))

        self.history.save()
        return results

    def serve_requests(self, today: Optional[date] = None) -> List[VideoResult]:
        """Build whatever topics have been sent to the bot with /make.

        Requests that arrive while an earlier one is waiting for approval are
        picked up in the same run, so a burst of topics is handled in one go.
        """
        today = today or date.today()
        queue = self.notifier.poll_requests()
        if not queue:
            self.logger.info("No pending /make requests")
            return []

        results = []
        while queue:
            request = queue.pop(0)
            self.logger.info("=== request: %s ===", request["topic"])
            results.append(
                self._guarded(request["format"], today, request, index=len(results))
            )
            queue.extend(self.notifier.take_requests())

        self.history.save()
        return results

    def _guarded(
        self,
        video_format: str,
        today: date,
        request: Optional[dict] = None,
        index: int = 0,
    ) -> VideoResult:
        """Run one video, turning any failure into a reported result."""
        try:
            return self._run_one(video_format, today, request, index)
        except Exception as exc:  # noqa: BLE001 - one video must not kill the rest
            self.logger.error("%s video failed: %s", video_format, exc, exc_info=True)
            self.notifier.failure(video_format, str(exc))
            return VideoResult(video_format, success=False, error=str(exc))

    def _run_one(
        self,
        video_format: str,
        today: date,
        request: Optional[dict] = None,
        index: int = 0,
    ) -> VideoResult:
        suffix = f"_req{index}" if request else ""
        work_dir = self.config.work_dir / f"{today.isoformat()}_{video_format}{suffix}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        content_type = self.content_type_for(today)
        shot_count = self.config.get(f"{video_format}.shot_count", 8)
        aspect_ratio = self.config.get(f"{video_format}.aspect_ratio", "16:9")
        veo_model = self.config.get(f"{video_format}.veo_model", "veo3_fast")
        style = self._subtitle_style(aspect_ratio)

        # Checked up front: a broken ffmpeg discovered after the clips are
        # rendered would waste every dollar spent on them.
        if not self.dry_run:
            assemble.ensure_ffmpeg(need_subtitles=style is not None)

        # 1. Signals + original script
        signals = self.trends.collect(content_type, today)
        story = self.stories.generate(
            content_type=content_type,
            video_format=video_format,
            shot_count=shot_count,
            signals=signals,
            avoid=self.history.recent_summaries(),
            requested=request,
        )
        self._write_artifacts(work_dir, story, signals)

        cost = self._estimate_cost(story, veo_model)
        self.logger.info(
            "Estimated spend for this %s video: $%.2f", video_format, cost
        )

        if self.dry_run:
            self.logger.info("Dry run: stopping before any paid generation")
            self.notifier.dry_run(story, video_format, cost)
            return VideoResult(
                video_format, success=True, story=story, estimated_cost=cost
            )

        # 2. Human approval, before anything expensive happens
        if not self._approved(story, video_format, cost):
            self.logger.info("%s video not approved; skipping", video_format)
            return VideoResult(
                video_format,
                success=True,
                story=story,
                error="not approved",
            )

        # 3. Narration and clips
        narrations = self._render_narration(story, work_dir, today)
        clips, failed = self._render_clips(story, work_dir, aspect_ratio, veo_model)

        allowed = MAX_FAILED_SHOT_RATIO.get(video_format, 0.0) * len(story["shots"])
        if len(failed) > allowed:
            raise KieError(
                f"{len(failed)} of {len(story['shots'])} clips failed "
                f"({', '.join(failed[:5])}); aborting rather than publishing a broken video"
            )

        # 4. Assemble
        video_path = self._assemble(
            story, work_dir, clips, narrations, aspect_ratio, style
        )
        thumb = assemble.thumbnail(video_path, work_dir / "thumbnail.jpg")

        # 5. Publish
        youtube_id = None
        if self.skip_upload:
            self.logger.info("Upload skipped; video is at %s", video_path)
        else:
            youtube_id = self.uploader.upload(
                video_path=video_path,
                title=self._youtube_title(story, video_format),
                description=self._youtube_description(story, video_format),
                tags=story.get("youtube", {}).get("tags", []),
                thumbnail_path=thumb,
            )
            self.notifier.success(story, video_format, youtube_id, cost)

        self.history.record(story, video_format, youtube_id or "")
        return VideoResult(
            video_format=video_format,
            success=True,
            story=story,
            video_path=video_path,
            youtube_id=youtube_id,
            estimated_cost=cost,
            failed_shots=failed,
        )

    # ── Steps ─────────────────────────────────────────────────────────────

    def _write_artifacts(self, work_dir: Path, story: dict, signals: dict) -> None:
        """Dump the script so a failed run can be inspected or hand-edited."""
        def dump(name: str, payload: dict) -> None:
            (work_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        dump("story.json", {**story, "signals": signals})
        dump("backgrounds.json", {"backgrounds": story["backgrounds"]})
        dump("shots.json", {"shots": story["shots"]})
        self.logger.info("Artifacts written to %s", work_dir)

    def _approved(self, story: dict, video_format: str, cost: float) -> bool:
        if not self.config.get("approval.enabled", True):
            return True
        return self.notifier.request_approval(
            story,
            video_format,
            cost,
            self.config.get("approval.timeout_seconds", 3600),
        )

    def _render_narration(
        self, story: dict, work_dir: Path, today: date
    ) -> Dict[str, Path]:
        voice_id = self.narrator.voice_for(today)
        self.logger.info(
            "Rendering narration for %d shots (voice %s)",
            len(story["shots"]), voice_id,
        )
        narrations: Dict[str, Path] = {}
        lock = threading.Lock()

        def task(shot: dict):
            def run():
                path = self.narrator.narrate(
                    shot["narration"],
                    work_dir / "narration" / f"{shot['shot_id']}.mp3",
                    label=shot["shot_id"],
                    voice_id=voice_id,
                )
                with lock:
                    narrations[shot["shot_id"]] = path
            return run

        errors = self.kie.run_parallel([task(shot) for shot in story["shots"]])
        if errors:
            # Narration is cheap and every shot needs it, so any failure is fatal.
            raise errors[0]
        return narrations

    def _render_clips(
        self, story: dict, work_dir: Path, aspect_ratio: str, veo_model: str
    ):
        chains = _chain_shots(story["shots"])
        self.logger.info(
            "Generating %d Veo clips in %d chained scenes (%s, %s)",
            len(story["shots"]), len(chains), veo_model, aspect_ratio,
        )
        clips: Dict[str, Path] = {}
        failed: List[str] = []
        lock = threading.Lock()

        # Veo's safety filter is stochastic: the same wholesome prompt can be
        # blocked once and pass on the next submission. A blocked request
        # generates nothing and so costs nothing, whereas giving up strands
        # every clip already paid for in this episode.
        attempts = self.config.get("kie.clip_attempts", 3)
        resolution = self.config.get(f"{story['format']}.resolution", "1080p")

        def task(chain: List[dict]):
            def run():
                # Carried from shot to shot: each clip opens on the frame the
                # previous one ended on. None means this shot starts a scene.
                first_frame_url = None
                for shot in chain:
                    shot_id = shot["shot_id"]
                    for attempt in range(1, attempts + 1):
                        try:
                            url = self.kie.generate_clip(
                                prompt=shot["veo_prompt"],
                                aspect_ratio=aspect_ratio,
                                model=veo_model,
                                resolution=resolution,
                                first_frame_url=first_frame_url,
                                label=shot_id,
                            )
                            path = download(url, work_dir / "clips" / f"{shot_id}.mp4")
                            with lock:
                                clips[shot_id] = path
                            first_frame_url = self._chain_frame(path, work_dir, shot_id)
                            break
                        except Exception as exc:  # noqa: BLE001 - handled by caller
                            if attempt == attempts:
                                self.logger.error(
                                    "%s failed after %d attempts: %s",
                                    shot_id, attempts, exc,
                                )
                                with lock:
                                    failed.append(shot_id)
                                # The link is broken, so the next shot in this
                                # scene starts fresh rather than from a stale frame.
                                first_frame_url = None
                            else:
                                self.logger.warning(
                                    "%s attempt %d/%d failed (%s); retrying",
                                    shot_id, attempt, attempts, exc,
                                )
            return run

        self.kie.run_parallel([task(chain) for chain in chains])
        return clips, failed

    def _chain_frame(self, clip: Path, work_dir: Path, shot_id: str) -> Optional[str]:
        """Host this clip's final frame so the next shot can open on it."""
        try:
            frame = assemble.last_frame(clip, work_dir / "frames" / f"{shot_id}.jpg")
            return self.kie.upload_image(frame)
        except Exception as exc:  # noqa: BLE001 - continuity is not worth the run
            self.logger.warning(
                "%s: could not hand a frame to the next shot (%s)", shot_id, exc
            )
            return None

    def _assemble(
        self,
        story: dict,
        work_dir: Path,
        clips: Dict[str, Path],
        narrations: Dict[str, Path],
        aspect_ratio: str,
        style: Optional[dict],
    ) -> Path:
        self.logger.info("Assembling %d segments", len(clips))
        segments = []
        for shot in story["shots"]:
            shot_id = shot["shot_id"]
            if shot_id not in clips:
                continue  # already accounted for in the failed-shot budget
            segments.append(
                assemble.build_segment(
                    clip=clips[shot_id],
                    narration=narrations[shot_id],
                    destination=work_dir / "segments" / f"{shot_id}.mp4",
                    aspect_ratio=aspect_ratio,
                    subtitle=shot.get("narration") if style else None,
                    subtitle_style=style,
                )
            )

        final = assemble.concat(segments, work_dir / "final.mp4")
        self.logger.info(
            "Final video: %s (%.1fs, %.1f MB)",
            final, assemble.probe_duration(final), final.stat().st_size / 1e6,
        )
        return final

    def _subtitle_style(self, aspect_ratio: str) -> Optional[dict]:
        """Resolve the per-aspect-ratio subtitle style, or None if disabled."""
        settings = self.config.get("subtitles", {})
        if not settings.get("enabled", True):
            return None

        def pick(key: str, fallback):
            value = settings.get(key, fallback)
            # font_size and margin_v are keyed by aspect ratio, the rest are not.
            return value.get(aspect_ratio, fallback) if isinstance(value, dict) else value

        return {
            "font": settings.get("font", "DejaVu Sans"),
            "font_size": pick("font_size", 50),
            "primary_colour": settings.get("primary_colour", "&H00FFFFFF"),
            "outline_colour": settings.get("outline_colour", "&H00000000"),
            "back_colour": settings.get("back_colour", "&HFF000000"),
            "border_style": settings.get("border_style", 1),
            "outline": settings.get("outline", 3),
            "shadow": settings.get("shadow", 1),
            "margin_h": settings.get("margin_h", 120),
            "margin_v": pick("margin_v", 300),
            "words_per_cue": settings.get("words_per_cue", 5),
        }

    # ── Metadata ──────────────────────────────────────────────────────────

    def _youtube_title(self, story: dict, video_format: str) -> str:
        title = story.get("youtube", {}).get("title") or story.get("title", "")
        if video_format == "short" and "#Shorts" not in title:
            title = f"{title} #Shorts"
        return title[:100]

    def _youtube_description(self, story: dict, video_format: str) -> str:
        parts = [
            story.get("youtube", {}).get("description", ""),
            "",
            f"🔬 {story.get('takeaway', '')}" if story.get("takeaway") else "",
            "",
            "#Science #Technology #HowItWorks #Explained #ScienceShorts",
        ]
        if video_format == "short":
            parts.append("#Shorts")
        return "\n".join(part for part in parts if part is not None)

    def _estimate_cost(self, story: dict, veo_model: str) -> float:
        shots = story.get("shots", [])
        clips = len(shots) * CLIP_COST.get(veo_model, CLIP_COST["veo3_fast"])
        chars = sum(len(shot.get("narration", "")) for shot in shots)
        return clips + (chars / 1000) * NARRATION_COST_PER_1K_CHARS
