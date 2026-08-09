# Hindi Science Explainer Pipeline — Setup Guide

## Overview

Generates and publishes Hindi science/engineering explainer videos to YouTube.
Narration is Hindi; English subtitles are burned into the picture.

```
Claude writes the script  ->  Telegram approval  ->  Veo 3.1 clips (kie.ai)
  ->  ElevenLabs narration  ->  ffmpeg stitch + subtitle burn-in  ->  YouTube
```

Two schedules, both on GitHub Actions:

| Workflow | Trigger | What it does |
|---|---|---|
| `daily-pipeline.yml` | `0 2 * * *` (07:30 IST) | A 60s Short daily, plus a ~5 min long video on Sunday |
| `telegram-requests.yml` | `*/15 * * * *` | Builds any topic you sent the bot with `/make` |

**Nothing is rendered without your approval.** The script (~$0.05) is written
first and sent to Telegram with the topic and description. Only after you tap
Approve does the pipeline spend on Veo clips. No answer within an hour = no.

---

## Prerequisites

| Service | Purpose | Get key from |
|---|---|---|
| **Anthropic Claude** | Script + visual direction | https://console.anthropic.com/ |
| **kie.ai** | Veo 3.1 video clips | https://kie.ai/ |
| **ElevenLabs** | Hindi narration (paid plan required for library voices) | https://elevenlabs.io/ |
| **YouTube Data API v3** | Trending signals | Google Cloud Console |
| **YouTube OAuth** | Uploading | Google Cloud Console → Credentials |
| **Telegram Bot** | Approvals, `/make` requests, notifications | @BotFather |

Also needed on the runner: **Python 3.11** and **ffmpeg built with libass**
(subtitle burn-in). CI installs both; see Local Development for macOS.

---

## Quick Start (GitHub Actions)

### 1. Clone

```bash
git clone https://github.com/soumya-05/ai-video-generator-n-publisher.git
cd ai-video-generator-n-publisher
```

### 2. Add GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Example |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` |
| `KIE_API_TOKEN` | `...` |
| `ELEVENLABS_API_KEY` | `sk_` + 48 hex chars |
| `YOUTUBE_CLIENT_ID` | `xxx.apps.googleusercontent.com` |
| `YOUTUBE_CLIENT_SECRET` | `GOCSPX-...` |
| `YOUTUBE_REFRESH_TOKEN` | `1//0...` |
| `YOUTUBE_DATA_API_KEY` | `AIza...` |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-...` |
| `TELEGRAM_CHAT_ID` | `7148944586` |

Verify them all at once with the **Check Keys** workflow
(`.github/workflows/check-keys.yml`) — it validates every credential and prints
no values.

### 3. Enable the workflows

Actions tab → enable **Daily Science Video Pipeline** and **Telegram Topic
Requests**. They share a `video-pipeline` concurrency group so they never poll
Telegram at the same time.

### 4. Test run

Actions → **Daily Science Video Pipeline** → **Run workflow** with
`dry_run: true`. That writes a full script and stops before anything is billed.

---

## Local Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/init_env.py        # prompts for each key, writes .env (0600)
python run_daily_pipeline.py run --dry-run
```

macOS needs a full ffmpeg. Homebrew's `ffmpeg` formula is built *without*
libass, so subtitle burn-in fails with `No such filter: 'subtitles'`:

```bash
brew install ffmpeg-full
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:$PATH"
```

Ubuntu's `apt-get install -y ffmpeg` (what CI uses) already includes libass.
`assemble.ensure_ffmpeg()` checks for the filter before any paid step, so a
bad binary costs nothing.

### Commands

```bash
python run_daily_pipeline.py run --dry-run          # plan a video, spend nothing
python run_daily_pipeline.py run                    # today's scheduled videos
python run_daily_pipeline.py run --only short --skip-upload
python run_daily_pipeline.py run --date 2026-08-16  # pretend it is this day
python run_daily_pipeline.py listen                 # build /make requests
python run_daily_pipeline.py voices                 # list Hindi ElevenLabs voices
```

---

## Requesting a topic from Telegram

Message the bot:

```
/make how a washing machine works
/make long CRISPR gene editing | focus on the delivery problem
/make short why planes stay up | keep it counter-intuitive
```

- `short` (default, 8 shots) or `long` (38 shots) as the first word.
- Everything after `|` is extra direction for Claude.

Within about 15 minutes the workflow writes the script, sends it back for
approval, then renders and uploads it.

---

## API setup notes

### Anthropic
Create a key at https://console.anthropic.com/. The model is set in
`config.yaml` under `story.model`.

### YouTube — Data API key (trending signals)
Google Cloud Console → APIs & Services → enable **YouTube Data API v3** →
create an API key → `YOUTUBE_DATA_API_KEY`.

### YouTube — OAuth (uploading)
1. Credentials → Create OAuth 2.0 Client ID → **Desktop app**
   (required for the loopback redirect used by `scripts/youtube_auth.py`).
2. `python scripts/youtube_auth.py` and complete the browser flow.
3. Copy the refresh token into `YOUTUBE_REFRESH_TOKEN`.

> **Publish the OAuth app.** While the consent screen is in *Testing*, Google
> expires refresh tokens after **7 days**, so daily uploads die every week with
> `invalid_grant`. Do not upload a logo or set domains — either forces a
> verification review.

### ElevenLabs
The key is `sk_` + 48 hex characters. The 64-character hex string shown on the
dashboard is a key *ID*, not a credential. Library voices require a paid plan;
the free tier only exposes the ~21 premade voices.

### Telegram
1. `/newbot` with @BotFather, copy the token.
2. Send your bot a message, then read the chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.

---

## Configuration

`config.yaml` holds non-secret settings only (copy from `config.yaml.example`).
API keys always come from environment variables and override the file.

```yaml
pipeline:
  short_days: [0, 1, 2, 3, 4, 5, 6]   # Mon=0 .. Sun=6
  long_days: [6]
  content_rotation:                    # subject area per weekday
    - how_everyday_machines_work
    - physics_and_energy
    - space_and_astronomy
    - technology_and_computing
    - biology_and_the_human_body
    - medical_science
    - engineering_and_infrastructure

short: {shot_count: 8,  aspect_ratio: "9:16", veo_model: veo3_fast}
long:  {shot_count: 38, aspect_ratio: "16:9", veo_model: veo3_fast}

approval:  {enabled: true, timeout_seconds: 3600}
subtitles: {enabled: true}
youtube:   {category_id: "28", made_for_kids: false, privacy_status: public}
```

Topics are never hardcoded — Claude picks a specific mechanism inside the day's
subject area, avoiding anything in `data/history.json`.

The daily cron lives in `.github/workflows/daily-pipeline.yml`, not in
`config.yaml`.

---

## Cost

1 kie credit = $0.005. At `veo3_fast` ($0.325 per 8s clip):

| Format | Shots | Per video |
|---|---|---|
| Short | 8 | ~$2.69 |
| Long | 38 | ~$12.77 |

Daily Shorts + a weekly long video ≈ **$135/month**. Veo is >95% of the bill;
Claude and ElevenLabs are rounding errors. `veo3` (non-fast) is 4× the price.

---

## Monitoring

- **Telegram** — approval requests, success with the video link, failures with
  the error.
- **GitHub Actions** — every run uploads `logs/` and the generated JSON as an
  artifact (7 day retention).
- **Local** — `logs/pipeline.log`.

---

## Troubleshooting

**`No such filter: 'subtitles'`** — ffmpeg was built without libass. See the
macOS note above, or set `subtitles.enabled: false`.

**`invalid_grant` on upload** — the OAuth consent screen is still in Testing.
Publish the app and regenerate the refresh token.

**Custom thumbnail 403** — the channel is not phone-verified. Fix at
youtube.com/verify. This only logs a warning; it never fails a run.

**Approval never arrives** — check `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
If Telegram is unconfigured the pipeline refuses to spend and skips the video.

**A shot fails to render** — Veo's safety filter blocks prompts at random.
Blocked requests are not billed and are retried (`kie.clip_attempts`). Shorts
abort if any shot fails; long videos tolerate 10%.

---

## File structure

```
├── run_daily_pipeline.py       # CLI: run | listen | voices
├── config.yaml(.example)       # non-secret settings
├── kids_video/
│   ├── pipeline.py             # orchestration
│   ├── story.py                # Claude script + Veo prompts
│   ├── trends.py               # YouTube trending signals
│   ├── kie.py                  # Veo 3.1 clip rendering
│   ├── voice.py                # ElevenLabs narration
│   ├── assemble.py             # ffmpeg stitch, subtitle burn-in, thumbnail
│   ├── youtube.py              # upload + metadata
│   ├── notify.py               # Telegram approvals and /make requests
│   ├── history.py              # published-topic memory
│   └── config.py               # config + secrets loading
├── scripts/
│   ├── init_env.py             # write a .env template
│   ├── youtube_auth.py         # obtain a refresh token
│   └── check_keys.py           # validate every credential
├── .github/workflows/
│   ├── daily-pipeline.yml
│   ├── telegram-requests.yml
│   ├── check-keys.yml
│   └── secret-scan.yml
└── data/history.json           # what has already been published
```

---

## License

MIT.
