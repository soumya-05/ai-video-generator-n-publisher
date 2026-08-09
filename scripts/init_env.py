#!/usr/bin/env python3
"""Write a local .env by prompting for each key.

Avoids editors (macOS hides dotfiles in save dialogs and TextEdit can save RTF)
and keeps the values out of shell history. Existing values are preserved when
you press Enter, and the file is written owner-read/write only.

    python scripts/init_env.py
"""

import getpass
import os
import stat
import sys
from pathlib import Path

ENV_PATH = Path(".env")

KEYS = [
    ("ANTHROPIC_API_KEY", "Claude, writes the stories", True),
    ("KIE_API_TOKEN", "kie.ai, renders the video clips", True),
    ("IMGBB_API_KEY", "imgbb, permanently hosts character sheets", True),
    ("ELEVENLABS_API_KEY", "ElevenLabs, Hindi narration (starts with sk_)", True),
    ("YOUTUBE_CLIENT_ID", "OAuth client id (not secret, ends .apps.googleusercontent.com)", False),
    ("YOUTUBE_CLIENT_SECRET", "OAuth client secret", True),
    ("YOUTUBE_REFRESH_TOKEN", "leave blank until scripts/youtube_auth.py is run", True),
    ("YOUTUBE_DATA_API_KEY", "YouTube Data API key, for trend signals", True),
    ("TELEGRAM_BOT_TOKEN", "Telegram notifications", True),
    ("TELEGRAM_CHAT_ID", "Telegram chat id", False),
]


def read_existing() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    return values


def main() -> int:
    existing = read_existing()
    print("Enter each value, or press Enter to keep/skip.")
    print("Secret values are not echoed to the screen.\n")

    values = {}
    for name, hint, secret in KEYS:
        current = existing.get(name, "")
        state = f"currently set, {len(current)} chars" if current else "empty"
        prompt = f"{name}\n  {hint}\n  [{state}] > "
        try:
            entered = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted; .env unchanged.")
            return 1
        values[name] = entered or current
        print()

    body = "".join(f"{name}={values[name]}\n" for name, _, _ in KEYS)
    ENV_PATH.write_text(body, encoding="utf-8")
    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600, owner only

    filled = [n for n, _, _ in KEYS if values[n]]
    print(f"Wrote {ENV_PATH} (permissions 600) with {len(filled)}/{len(KEYS)} values set.")
    print("Verify with: .venv/bin/python scripts/check_keys.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
