"""Daily pipeline orchestrator.

Flow per video:
    trend signals -> original story (Claude) -> Hindi narration (ElevenLabs)
    -> Pixar-style 8s clips (Veo 3.1, locked to the host reference sheet)
    -> ffmpeg assembly -> YouTube

Artifacts are written in the same shape the story-to-animation skills use
(story.json / characters.json / backgrounds.json / shots.json), so any run can
be inspected or hand-edited with those skills before assembly.
"""

import json
import shutil
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from . import assemble
from .cast import CastManager
from .history import History
from .kie import KieClient, KieError, download
from .notify import TelegramNotifier
from .story import StoryGenerator
from .trends import TrendSignals
from .voice import HindiNarrator
from .youtube import YouTubeUploader

# USD per 8-second 1080p clip, from kie.ai pricing (1 credit = $0.005).
CLIP_COST = {"veo3_lite": 0.175, "veo3_fast": 0.325, "veo3": 1.275}
NARRATION_COST_PER_1K_CHARS = 0.10

# A Short is short enough that losing a shot is obvious; a 5-minute story can
# absorb a couple of gaps rather than throwing the whole render away.
MAX_FAILED_SHOT_RATIO = {"short": 0.0, "long": 0.1}


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
        self.cast_manager = CastManager(config, self.kie)
        self.trends = TrendSignals(config)
        self.stories = StoryGenerator(config)
        self.narrator = HindiNarrator(config)
        self.uploader = YouTubeUploader(config)
        self.notifier = TelegramNotifier(config)
        self.history = History(
            config.get("paths.history_file", "data/history.json"),
            config.get("pipeline.history_size", 90),
        )

    # ── Scheduling ────────────────────────────────────────────────────────

    def formats_for(self, today: date) -> List[str]:
        """Which video formats are due today. Shorts are daily, long is weekly."""
        weekday = today.weekday()
        formats = []
        if weekday in self.config.get("pipeline.short_days", []):
            formats.append("short")
        if weekday in self.config.get("pipeline.long_days", []):
            formats.append("long")
        return formats

    def content_type_for(self, today: date) -> str:
        rotation = self.config.get("pipeline.content_rotation", ["moral_story"])
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
            try:
                results.append(self._run_one(video_format, today))
            except Exception as exc:  # noqa: BLE001 - one format must not kill the other
                self.logger.error("%s video failed: %s", video_format, exc, exc_info=True)
                self.notifier.failure(video_format, str(exc))
                results.append(VideoResult(video_format, success=False, error=str(exc)))

        self.history.save()
        return results

    def _run_one(self, video_format: str, today: date) -> VideoResult:
        work_dir = self.config.work_dir / f"{today.isoformat()}_{video_format}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        # A dry run only writes a story, so config descriptions are enough;
        # a real run needs the pinned reference sheets.
        cast = (
            self.cast_manager.from_config()
            if self.dry_run
            else self.cast_manager.require()
        )
        # Rotate which friends appear so every character gets screen time.
        # A Short has no room for a crowd, so it carries fewer.
        friends = cast.rotate_friends(
            self.config.get(
                f"{video_format}.friends_per_episode",
                self.config.get("story.friends_per_episode", 2),
            ),
            offset=today.toordinal(),
        )
        content_type = self.content_type_for(today)
        shot_count = self.config.get(f"{video_format}.shot_count", 8)
        aspect_ratio = self.config.get(f"{video_format}.aspect_ratio", "16:9")
        veo_model = self.config.get(f"{video_format}.veo_model", "veo3_fast")

        # 1. Signals + original story
        signals = self.trends.collect(content_type, today)
        story = self.stories.generate(
            content_type=content_type,
            video_format=video_format,
            shot_count=shot_count,
            signals=signals,
            avoid=self.history.recent_summaries(),
            host=cast.host,
            friends=friends,
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

        # 2. Narration and clips
        assemble.ensure_ffmpeg()
        narrations = self._render_narration(story, work_dir, today)
        clips, failed = self._render_clips(
            story, work_dir, cast, aspect_ratio, veo_model
        )

        allowed = MAX_FAILED_SHOT_RATIO.get(video_format, 0.0) * len(story["shots"])
        if len(failed) > allowed:
            raise KieError(
                f"{len(failed)} of {len(story['shots'])} clips failed "
                f"({', '.join(failed[:5])}); aborting rather than publishing a broken video"
            )

        # 3. Assemble
        video_path = self._assemble(story, work_dir, clips, narrations, aspect_ratio)
        thumb = assemble.thumbnail(video_path, work_dir / "thumbnail.jpg")

        # 4. Publish
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
        """Mirror the story-to-animation skills' file contract."""
        def dump(name: str, payload: dict) -> None:
            (work_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        dump("story.json", {**story, "signals": signals})
        dump("characters.json", {"characters": story["characters"]})
        dump("backgrounds.json", {"backgrounds": story["backgrounds"]})
        dump("shots.json", {"shots": story["shots"]})
        self.logger.info("Artifacts written to %s", work_dir)

    def _render_narration(
        self, story: dict, work_dir: Path, today: date
    ) -> Dict[str, Path]:
        voice_id = self.narrator.voice_for(today)
        self.logger.info(
            "Rendering Hindi narration for %d shots (voice %s)",
            len(story["shots"]), voice_id,
        )
        narrations: Dict[str, Path] = {}
        lock = threading.Lock()

        def task(shot: dict):
            def run():
                path = self.narrator.narrate(
                    shot["narration_hi"],
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
        self, story: dict, work_dir: Path, cast, aspect_ratio: str, veo_model: str
    ):
        self.logger.info(
            "Generating %d Veo clips (%s, %s)", len(story["shots"]), veo_model, aspect_ratio
        )
        clips: Dict[str, Path] = {}
        failed: List[str] = []
        lock = threading.Lock()

        # Veo's safety filter is stochastic: the same wholesome prompt can be
        # blocked once and pass on the next submission. A blocked request
        # generates nothing and so costs nothing, whereas giving up strands
        # every clip already paid for in this episode.
        attempts = self.config.get("kie.clip_attempts", 3)

        def task(shot: dict):
            def run():
                shot_id = shot["shot_id"]
                # Passing the locked reference sheets is what keeps recurring
                # characters identical across every clip and every episode.
                references = cast.reference_urls_for(shot.get("cast_keys", []))
                for attempt in range(1, attempts + 1):
                    try:
                        url = self.kie.generate_clip(
                            prompt=shot["veo_prompt"],
                            aspect_ratio=aspect_ratio,
                            model=veo_model,
                            resolution=self.config.get(
                                f"{story['format']}.resolution", "1080p"
                            ),
                            reference_urls=references,
                            label=shot_id,
                        )
                        path = download(url, work_dir / "clips" / f"{shot_id}.mp4")
                        with lock:
                            clips[shot_id] = path
                        return
                    except Exception as exc:  # noqa: BLE001 - handled by caller
                        if attempt == attempts:
                            self.logger.error(
                                "%s failed after %d attempts: %s", shot_id, attempts, exc
                            )
                            with lock:
                                failed.append(shot_id)
                        else:
                            self.logger.warning(
                                "%s attempt %d/%d failed (%s); retrying",
                                shot_id, attempt, attempts, exc,
                            )
            return run

        self.kie.run_parallel([task(shot) for shot in story["shots"]])
        return clips, failed

    def _assemble(
        self,
        story: dict,
        work_dir: Path,
        clips: Dict[str, Path],
        narrations: Dict[str, Path],
        aspect_ratio: str,
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
                )
            )

        final = assemble.concat(segments, work_dir / "final.mp4")
        self.logger.info(
            "Final video: %s (%.1fs, %.1f MB)",
            final, assemble.probe_duration(final), final.stat().st_size / 1e6,
        )
        return final

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
            f"💡 {story.get('moral', '')}" if story.get("moral") else "",
            "",
            "#HindiKids #बच्चोंकीकहानी #KidsStory #HindiCartoon",
        ]
        if video_format == "short":
            parts.append("#Shorts")
        return "\n".join(part for part in parts if part is not None)

    def _estimate_cost(self, story: dict, veo_model: str) -> float:
        shots = story.get("shots", [])
        clips = len(shots) * CLIP_COST.get(veo_model, CLIP_COST["veo3_fast"])
        chars = sum(len(shot.get("narration_hi", "")) for shot in shots)
        return clips + (chars / 1000) * NARRATION_COST_PER_1K_CHARS
