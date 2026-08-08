#!/usr/bin/env python3
"""CLI for the Hindi kids' video pipeline.

    python run_daily_pipeline.py setup-cast      # once: render the permanent cast
    python run_daily_pipeline.py run --dry-run   # plan a video, spend nothing
    python run_daily_pipeline.py run             # today's scheduled videos
    python run_daily_pipeline.py run --only short --skip-upload
    python run_daily_pipeline.py voices          # list Hindi ElevenLabs voices
"""

import argparse
import sys
from datetime import date

from kids_video.cast import CastManager
from kids_video.config import Config
from kids_video.kie import KieClient
from kids_video.pipeline import Pipeline
from kids_video.voice import HindiNarrator


def cmd_run(args, config) -> int:
    pipeline = Pipeline(config, dry_run=args.dry_run, skip_upload=args.skip_upload)
    today = date.fromisoformat(args.date) if args.date else date.today()
    results = pipeline.run(today=today, only=args.only)

    for result in results:
        if result.success:
            where = result.youtube_id or result.video_path or "dry run"
            config.logger.info("%s: OK (%s)", result.video_format, where)
        else:
            config.logger.error("%s: FAILED (%s)", result.video_format, result.error)
    return 0 if all(r.success for r in results) else 1


def cmd_setup_cast(args, config) -> int:
    manager = CastManager(config, KieClient(config))
    cast = manager.create(force=args.force, only=args.character)
    for key, member in cast.members.items():
        config.logger.info(
            "%s (%s): %d reference views", key, member.name, len(member.reference_urls)
        )
    config.logger.info(
        "Commit %s so every future episode reuses these exact characters.",
        manager.path,
    )
    return 0


def cmd_voices(args, config) -> int:
    for voice in HindiNarrator(config).list_hindi_voices():
        config.logger.info("%s  %s", voice.get("voice_id"), voice.get("name"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="generate today's scheduled videos")
    run.add_argument("--dry-run", action="store_true",
                     help="stop before any paid generation")
    run.add_argument("--skip-upload", action="store_true",
                     help="build the video but do not publish to YouTube")
    run.add_argument("--only", choices=["short", "long"],
                     help="force one format regardless of the schedule")
    run.add_argument("--date", help="pretend today is this ISO date")
    run.set_defaults(func=cmd_run)

    setup = sub.add_parser("setup-cast", help="render the permanent cast (run once)")
    setup.add_argument("--force", action="store_true",
                       help="re-render characters that already have reference sheets")
    setup.add_argument("--character", help="only this cast key")
    setup.set_defaults(func=cmd_setup_cast)

    voices = sub.add_parser("voices", help="list Hindi ElevenLabs voices")
    voices.set_defaults(func=cmd_voices)

    args = parser.parse_args()
    config = Config(args.config)
    try:
        return args.func(args, config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        config.logger.error("%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
