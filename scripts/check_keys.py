#!/usr/bin/env python3
"""Validate every API key and the YouTube channel, without printing secrets.

Each check makes the cheapest possible read-only call. Run locally, where a
local .env is read, or in GitHub Actions where the secrets are injected:

    python scripts/check_keys.py
"""

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kids_video.config import _load_dotenv  # noqa: E402

_load_dotenv(Path(".env"))

TIMEOUT = 30
results = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:24} {detail}", flush=True)


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


# A GitHub secret can exist by name yet hold an empty value, which looks
# identical to a missing secret from inside the job. Say so, or the fix is
# non-obvious.
UNSET = "empty - the GitHub secret is missing OR exists with a blank value"


def check_anthropic() -> None:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return report("ANTHROPIC_API_KEY", False, "not set")
    r = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return report("ANTHROPIC_API_KEY", False, f"HTTP {r.status_code} {r.text[:120]}")
    ids = [m["id"] for m in r.json().get("data", [])]
    wanted = "claude-opus-4-6"
    report(
        "ANTHROPIC_API_KEY",
        wanted in ids,
        f"valid, {len(ids)} models"
        + ("" if wanted in ids else f" but '{wanted}' NOT available"),
    )


def check_kie() -> None:
    key = env("KIE_API_TOKEN")
    if not key:
        return report("KIE_API_TOKEN", False, UNSET)
    headers = {"Authorization": f"Bearer {key}"}
    for url in (
        "https://api.kie.ai/api/v1/chat/credit",
        "https://api.kie.ai/api/v1/common/credit",
    ):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            body = r.json()
            credits = body.get("data")
            usd = f" (~${credits * 0.005:.2f})" if isinstance(credits, (int, float)) else ""
            return report("KIE_API_TOKEN", True, f"valid, credits={credits}{usd}")
        if r.status_code in (401, 403):
            return report("KIE_API_TOKEN", False, f"rejected: HTTP {r.status_code}")
    report("KIE_API_TOKEN", False, "no credit endpoint answered; check manually")


def check_imgbb() -> None:
    key = env("IMGBB_API_KEY")
    if not key:
        return report("IMGBB_API_KEY", False, UNSET)
    # Smallest legal upload: a 1x1 transparent GIF.
    tiny = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    r = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": key, "image": tiny, "expiration": 60},
        timeout=TIMEOUT,
    )
    ok = r.status_code == 200 and r.json().get("success")
    report("IMGBB_API_KEY", bool(ok), "valid, upload works" if ok else f"HTTP {r.status_code} {r.text[:120]}")


def check_elevenlabs() -> None:
    key = env("ELEVENLABS_API_KEY")
    if not key:
        return report("ELEVENLABS_API_KEY", False, f"{UNSET} - narration cannot run")
    if not key.startswith("sk_"):
        # The dashboard shows a key *ID* next to each key; it is easy to copy
        # that by mistake. The real key is only revealed at create/rotate time.
        return report(
            "ELEVENLABS_API_KEY", False,
            "looks like a key ID, not a key - real keys start with 'sk_' and are "
            "shown only when created or rotated",
        )
    r = requests.get(
        "https://api.elevenlabs.io/v1/user/subscription",
        headers={"xi-api-key": key},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return report("ELEVENLABS_API_KEY", False, f"HTTP {r.status_code} {r.text[:120]}")
    body = r.json()
    used = body.get("character_count", 0)
    limit = body.get("character_limit", 0)
    report(
        "ELEVENLABS_API_KEY",
        used < limit,
        f"valid, tier={body.get('tier')}, {used}/{limit} chars used, {limit - used} left",
    )


def check_voice_id() -> None:
    key = env("ELEVENLABS_API_KEY")
    voice_id = "3AMU7jXQuQa3oRvRqUmb"  # config default
    if not key:
        return report("voice_id", False, "skipped, no ElevenLabs key")
    r = requests.get(
        f"https://api.elevenlabs.io/v1/voices/{voice_id}",
        headers={"xi-api-key": key},
        timeout=TIMEOUT,
    )
    ok = r.status_code == 200
    name = r.json().get("name") if ok else ""
    report("voice_id (config)", ok, f"'{name}' reachable" if ok else f"HTTP {r.status_code} - voice not on your account")


def check_youtube_data_key() -> None:
    key = env("YOUTUBE_DATA_API_KEY")
    if not key:
        return report("YOUTUBE_DATA_API_KEY", False, f"{UNSET} (trend signals degrade)")
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "id", "chart": "mostPopular", "maxResults": 1,
                "regionCode": "IN", "key": key},
        timeout=TIMEOUT,
    )
    ok = r.status_code == 200
    report("YOUTUBE_DATA_API_KEY", ok, "valid" if ok else f"HTTP {r.status_code} {r.text[:160]}")


def check_youtube_oauth() -> None:
    cid, secret, refresh = (
        env("YOUTUBE_CLIENT_ID"), env("YOUTUBE_CLIENT_SECRET"), env("YOUTUBE_REFRESH_TOKEN"),
    )
    missing = [n for n, v in
               (("YOUTUBE_CLIENT_ID", cid), ("YOUTUBE_CLIENT_SECRET", secret),
                ("YOUTUBE_REFRESH_TOKEN", refresh)) if not v]
    if missing:
        return report("YOUTUBE OAuth", False, f"missing: {', '.join(missing)}")

    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={"client_id": cid, "client_secret": secret,
              "refresh_token": refresh, "grant_type": "refresh_token"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        hint = ""
        if "invalid_grant" in r.text:
            hint = ("  -> the token no longer matches a live account/client. "
                    "Re-mint it: python scripts/youtube_auth.py")
        return report("YOUTUBE OAuth", False,
                      f"refresh failed HTTP {r.status_code}: {r.text[:200]}{hint}")
    token = r.json()["access_token"]
    scopes = r.json().get("scope", "")
    report("YOUTUBE OAuth", True, "refresh token works")

    upload_scope = "https://www.googleapis.com/auth/youtube.upload"
    report("YOUTUBE upload scope", upload_scope in scopes,
           "granted" if upload_scope in scopes else f"MISSING. Granted: {scopes}")

    c = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet,contentDetails,statistics,status", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if c.status_code != 200:
        return report("YOUTUBE channel", False, f"HTTP {c.status_code} {c.text[:200]}")
    items = c.json().get("items", [])
    if not items:
        return report("YOUTUBE channel", False,
                      "token has NO channel - create a channel on this Google account")
    ch = items[0]
    snip, stats, status = ch["snippet"], ch.get("statistics", {}), ch.get("status", {})
    report("YOUTUBE channel", True,
           f"'{snip['title']}' id={ch['id']} videos={stats.get('videoCount')} "
           f"subs={stats.get('subscriberCount')}")

    # Long-form uploads and custom thumbnails both need a verified channel.
    verified = status.get("longUploadsStatus")
    report("YOUTUBE long uploads", verified in ("allowed", "eligible"),
           f"longUploadsStatus={verified}"
           + ("" if verified in ("allowed", "eligible")
              else " - videos over 15 min will be rejected"))


def check_telegram() -> None:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token:
        return report("TELEGRAM", False, "not set (notifications only, non-fatal)")
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT)
    if r.status_code != 200:
        return report("TELEGRAM", False, f"bad token: HTTP {r.status_code}")
    name = r.json()["result"]["username"]
    if not chat:
        return report("TELEGRAM", False, f"bot @{name} ok but TELEGRAM_CHAT_ID not set")
    c = requests.get(f"https://api.telegram.org/bot{token}/getChat",
                     params={"chat_id": chat}, timeout=TIMEOUT)
    ok = c.status_code == 200
    report("TELEGRAM", ok,
           f"bot @{name} can reach the chat" if ok
           else f"@{name} cannot reach chat: {c.text[:120]}")


def main() -> int:
    print("Validating credentials (no secret values are printed)\n")
    for check in (
        check_anthropic, check_kie, check_imgbb, check_elevenlabs, check_voice_id,
        check_youtube_data_key, check_youtube_oauth, check_telegram,
    ):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - one bad check must not hide the rest
            report(check.__name__, False, f"check errored: {exc}")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("Needs attention: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
