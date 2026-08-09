# /make trigger

Cuts the delay between texting the bot `/make ...` and the render starting from
15-45 minutes down to about a minute.

## Why not a Telegram webhook

`setWebhook` and `getUpdates` are mutually exclusive - once a webhook is set,
`getUpdates` returns 409 forever. The approval gate in `kids_video/notify.py`
is built entirely on `getUpdates`, so a webhook would break the ✅/❌ buttons and
every run would refuse to spend.

This worker peeks instead. `getUpdates` only deletes an update once you
acknowledge it with a higher offset, so reading with no offset leaves the queue
intact. The worker only decides *when* to start the workflow; the workflow still
reads and acknowledges the request itself.

`.github/workflows/telegram-requests.yml` keeps its `*/15` cron as a fallback for
when the worker is down.

## Cost

Free. Workers' free plan includes cron triggers and 100,000 requests/day; this
uses 1,440. KV's free plan covers the single dedup key.

## Setup

```sh
cd trigger/worker
npm install -g wrangler
wrangler login                          # opens a browser, needs a Cloudflare account
wrangler kv namespace create STATE      # paste the printed id into wrangler.toml
```

Create a **fine-grained** GitHub token at
<https://github.com/settings/personal-access-tokens/new>:

- Repository access: only `soumya-05/ai-video-generator-n-publisher`
- Permissions: **Contents: Read and write** (this is what `repository_dispatch`
  requires - it does not need Actions or admin)

Then load the three secrets and deploy:

```sh
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
wrangler secret put GITHUB_TOKEN
wrangler deploy
```

## Checking it

```sh
wrangler tail                 # live logs
```

Text the bot `/make how a heat pump works`, then:

```sh
gh run list --workflow=telegram-requests.yml --limit 3
```

A run triggered by `repository_dispatch` (not `schedule`) should appear within
about a minute.
