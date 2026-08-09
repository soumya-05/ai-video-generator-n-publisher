#!/usr/bin/env python3
"""CLI for the Hindi science explainer video pipeline.

    python run_daily_pipeline.py run --dry-run   # plan a video, spend nothing
    python run_daily_pipeline.py run             # today's scheduled videos
    python run_daily_pipeline.py run --only short --skip-upload
    python run_daily_pipeline.py listen          # build topics sent with /make
    python run_daily_pipeline.py voices          # list Hindi ElevenLabs voices
"""

import argparse
import sys
from datetime import date

from kids_video.config import Config
from kids_video.pipeline import Pipeline
from kids_video.voice import HindiNarrator


def _report(config, results) -> int:
    for result in results:
        if result.success:
            where = result.youtube_id or result.video_path or result.error or "dry run"
            config.logger.info("%s: OK (%s)", result.video_format, where)
        else:
            config.logger.error("%s: FAILED (%s)", result.video_format, result.error)
    return 0 if all(r.success for r in results) else 1


def cmd_run(args, config) -> int:
    pipeline = Pipeline(config, dry_run=args.dry_run, skip_upload=args.skip_upload)
    today = date.fromisoformat(args.date) if args.date else date.today()
    return _report(config, pipeline.run(today=today, only=args.only))


def cmd_listen(args, config) -> int:
    pipeline = Pipeline(config, dry_run=args.dry_run, skip_upload=args.skip_upload)
    return _report(config, pipeline.serve_requests())


def cmd_voices(args, config) -> int:
    narrator = HindiNarrator(config)
    configured = config.get("voice.voice_id")

    mine = narrator.my_voices()
    print("\nOn your account (only these work for narration):")
    for voice in mine:
        marker = "  <-- config voice.voice_id" if voice["voice_id"] == configured else ""
        print(f"  {voice['voice_id']}  {voice['name']}{marker}")

    if configured not in {v["voice_id"] for v in mine}:
        print(
            f"\nWARNING: configured voice {configured} is NOT on your account, so "
            "narration will fail.\nAdd it from the shared library below, or set "
            "voice.voice_id in config.yaml to one of the ids above."
        )

    print("\nHindi storyteller voices in the shared library (add before using):")
    for voice in narrator.list_hindi_voices():
        print(f"  {voice['voice_id']}  {voice['name']}  ({voice.get('accent')})")
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

    listen = sub.add_parser(
        "listen", help="build any topics sent to the Telegram bot with /make"
    )
    listen.add_argument("--dry-run", action="store_true",
                        help="write the script only, spend nothing")
    listen.add_argument("--skip-upload", action="store_true",
                        help="build the video but do not publish to YouTube")
    listen.set_defaults(func=cmd_listen)

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
