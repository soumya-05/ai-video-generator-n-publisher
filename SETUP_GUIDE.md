# Kids Video Generator - Daily Automation Setup Guide

## 🎯 Overview

This pipeline automatically creates and publishes a Hindi kids animated video **every day** to:
- **YouTube Shorts** 
- **Instagram Reels**
- Sends Telegram notification

Two modes available:
1. **HeyGen Mode** (default) - Fast avatar videos (~3-5 min per video)
2. **3D Animation Mode** - High-quality Pixar-style 3D (requires manual approval steps)

---

## 📋 Prerequisites

### Required Accounts & API Keys

| Service | Purpose | Get Key From |
|---------|---------|--------------|
| **Anthropic Claude** | Script generation | https://console.anthropic.com/ |
| **HeyGen** | Avatar video generation | https://app.heygen.com/settings/api |
| **YouTube Data API v3** | Trending topics + upload | https://console.cloud.google.com/ |
| **YouTube OAuth** | Upload videos | Google Cloud Console → OAuth 2.0 |
| **Instagram Graph API** | Post Reels | https://developers.facebook.com/ |
| **Telegram Bot** | Notifications | @BotFather on Telegram |
| **kie.ai** (optional) | 3D animation | https://kie.ai/ |

---

## 🚀 Quick Start (GitHub Actions - Recommended)

### 1. Fork/Clone This Repository
```bash
git clone https://github.com/YOUR_USERNAME/kids-video-generator.git
cd kids-video-generator
```

### 2. Add GitHub Secrets
Go to **Settings → Secrets and variables → Actions → New repository secret**

Add these **required** secrets:
| Secret Name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-xxxxx` |
| `HEYGEN_API_KEY` | `sk_V2_hgu_xxxxx` |
| `YOUTUBE_CLIENT_ID` | `xxx.apps.googleusercontent.com` |
| `YOUTUBE_CLIENT_SECRET` | `GOCSPX-xxxxx` |
| `YOUTUBE_REFRESH_TOKEN` | `1//0xxxx` |
| `INSTAGRAM_ACCESS_TOKEN` | `EAAG...` (long-lived, 60 days) |
| `INSTAGRAM_ACCOUNT_ID` | `178414xxxx` (Instagram Business Account ID) |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-xxxxx` |
| `TELEGRAM_CHAT_ID` | `7148944586` |
| `YOUTUBE_DATA_API_KEY` | `AIzaSyXxxxx` |

Optional (for 3D mode):
| Secret Name | Value |
|-------------|-------|
| `KIE_API_TOKEN` | `xxx` |

Character reference sheets need no key: they are committed to `data/cast/` and
served from raw.githubusercontent.com, which requires the repo to stay public.

### 3. Enable Workflow
1. Go to **Actions** tab
2. Enable "Daily Kids Video Pipeline" workflow
3. It runs automatically at **2:00 AM UTC (7:30 AM IST)** daily

### 4. Test Run
- Go to Actions → "Daily Kids Video Pipeline" → **Run workflow**
- Check logs for success/failure

---

## 💻 Local Development Setup

### 1. Install Dependencies
```bash
cd kids-video-generator
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install pyyaml requests apscheduler
```

### 2. Configure
```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your API keys
```

### 3. Run Once (Test)
```bash
python run_daily_pipeline.py --test
```

### 4. Run Production
```bash
python run_daily_pipeline.py        # Single run
python run_daily_pipeline.py --daemon  # Run scheduler continuously
```

---

## 🔐 Detailed API Setup Instructions

### Anthropic Claude
1. Go to https://console.anthropic.com/
2. Create API key
3. Add to secrets as `ANTHROPIC_API_KEY`

### HeyGen
1. Go to https://app.heygen.com/settings/api
2. Generate API key
3. Add to secrets as `HEYGEN_API_KEY`

### YouTube (Two Parts)

#### Part A: YouTube Data API v3 (for trending topics)
1. Google Cloud Console → APIs & Services → Enable "YouTube Data API v3"
2. Create API Key (restrict to YouTube Data API v3)
3. Add as `YOUTUBE_DATA_API_KEY`

#### Part B: YouTube OAuth (for uploading)
1. Google Cloud Console → APIs & Services → Credentials
2. Create OAuth 2.0 Client ID (Web application)
3. Authorized redirect URIs: `https://developers.google.com/oauthplayground`
4. Get Client ID & Secret → Add as `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`
5. Go to OAuth Playground: https://developers.google.com/oauthplayground
6. Settings (gear) → Use your own OAuth credentials → Enter Client ID/Secret
7. Select "YouTube Data API v3" → `https://www.googleapis.com/auth/youtube.upload`
8. Authorize → Exchange code for tokens
9. Copy **Refresh Token** → Add as `YOUTUBE_REFRESH_TOKEN`

### Instagram Graph API
1. Facebook Developers → Create App → Business
2. Add "Instagram Graph API" product
3. Get Long-lived Access Token (60 days):
   - Graph API Explorer → Get User Access Token
   - Permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`
   - Extend token: `https://graph.facebook.com/v18.0/oauth/access_token?grant_type=fb_exchange_token&client_id={APP_ID}&client_secret={APP_SECRET}&fb_exchange_token={SHORT_TOKEN}`
4. Get Instagram Business Account ID:
   - `https://graph.facebook.com/v18.0/me/accounts?fields=instagram_business_account&access_token={LONG_TOKEN}`
5. Add `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_ACCOUNT_ID`

### Telegram Bot
1. Message @BotFather on Telegram
2. `/newbot` → Follow instructions
3. Copy token → Add as `TELEGRAM_BOT_TOKEN`
4. Start chat with your bot
5. Get chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`
6. Add as `TELEGRAM_CHAT_ID`

---

## 🎬 Switching to 3D Animation Mode

For higher quality (but manual approval required):

### 1. Enable in config.yaml
```yaml
pipeline:
  mode: "3d"  # or "both" to run both
```

### 2. Add the kie.ai key to secrets

### 3. Run 3D Pipeline Manually (5-step process)
```bash
# Step 1: Generate story from logline
# (Use the skill: generating-story-from-logline)

# Step 2: Extract characters & backgrounds
# (Use skill: extracting-characters-and-backgrounds)

# Step 3: Generate images
python ~/.claude/skills/generating-character-and-background-images/scripts/generate_images.py

# Step 4: Create shot list
# (Use skill: creating-shot-list)

# Step 5: Generate composites + video + merge
python ~/.claude/skills/generating-composite-and-video/scripts/generate_videos.py
python ~/.claude/skills/generating-composite-and-video/scripts/merge_clips.py
```

> **Note:** 3D mode requires manual approval at each step. Not fully automated.

---

## 📊 Monitoring & Logs

### GitHub Actions
- View runs at: `https://github.com/USER/REPO/actions`
- Logs retained for 90 days (on paid plans) / 30 days (free)

### Local Logs
- File: `logs/pipeline.log`
- Rotating logs (10MB max, 5 backups)

### Telegram Notifications
- Success: Video links + topic + moral
- Failure: Error details for debugging

---

## 🔧 Customization

### Change Schedule
Edit `config.yaml`:
```yaml
pipeline:
  schedule_cron: "0 2 * * *"  # 2 AM UTC = 7:30 AM IST
  # For 8 AM IST: "30 2 * * *"
  # For 6 PM IST: "30 12 * * *"
```

### Add Custom Topics
Edit `config.yaml` → `pipeline.topics`:
```yaml
topics:
  story:
    - "your custom story"
    - "another story"
```

### Change Video Style
Edit `heygen` section in config.yaml:
```yaml
heygen:
  avatar_id: "specific_avatar_id"  # or "auto"
  voice_id: "hi-IN-Standard-A"  # See HeyGen voices
```

---

## 🐛 Troubleshooting

### "Invalid YouTube refresh token"
- Re-generate refresh token via OAuth Playground
- Ensure `youtube.upload` scope is authorized

### "Instagram token expired"
- Tokens last 60 days
- Re-generate long-lived token and update secret

### "HeyGen video generation failed"
- Check API key validity
- Verify account has credits
- Check prompt length (max ~2000 chars)

### "Telegram notification not received"
- Verify bot token and chat ID
- Ensure you've started chat with bot (`/start`)

### GitHub Actions timeout
- Default: 30 min
- Increase in workflow: `timeout-minutes: 60`

---

## 📁 File Structure
```
kids-video-generator/
├── config.yaml                 # Main configuration (copy from example)
├── config.yaml.example         # Template
├── run_daily_pipeline.py       # Main orchestrator script
├── .github/
│   └── workflows/
│       └── daily-pipeline.yml  # GitHub Actions workflow
├── logs/                       # Auto-created
└── story-to-animation-skills/  # 3D animation pipeline (5 skills)
    ├── generating-story-from-logline/
    ├── extracting-characters-and-backgrounds/
    ├── generating-character-and-background-images/
    ├── creating-shot-list/
    └── generating-composite-and-video/
```

---

## 🆘 Support

- Check logs first: `logs/pipeline.log` or GitHub Actions logs
- Telegram notifications include error details
- For 3D pipeline issues, refer to individual skill `.md` files in `story-to-animation-skills/`

---

## 📝 License

MIT License - Feel free to modify and use for your channel!

---

**Happy automating! 🎬✨**