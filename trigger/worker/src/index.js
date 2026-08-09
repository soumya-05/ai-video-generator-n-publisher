/**
 * Fires the Telegram Topic Requests workflow within a minute of a /make.
 *
 * This deliberately does NOT use a Telegram webhook. Calling setWebhook makes
 * getUpdates return 409 for the rest of the bot's life, and the whole approval
 * gate in kids_video/notify.py is built on getUpdates - the buttons would stop
 * working and every run would refuse to spend.
 *
 * Instead this peeks. getUpdates only deletes an update once you acknowledge it
 * by calling again with a higher offset, so reading without an offset is
 * non-destructive: the /make stays in the queue for the pipeline to consume in
 * the normal way. All this worker decides is *when* the workflow should run.
 */

const TELEGRAM_API = "https://api.telegram.org";
const GITHUB_API = "https://api.github.com";
const DISPATCH_EVENT = "telegram-request";
const REQUEST_COMMAND = "/make";

// The highest update_id we have already fired for. Without this the worker
// would dispatch again every minute until the workflow drains the queue, and a
// queued run can sit behind the concurrency group for several minutes.
const CURSOR_KEY = "last_dispatched_update_id";

export default {
  async scheduled(event, env) {
    await trigger(env);
  },
};

async function trigger(env) {
  const pending = await peekUpdates(env);
  const requests = pending.filter((update) => isRequest(update, env));
  if (requests.length === 0) return;

  const newest = Math.max(...requests.map((update) => update.update_id));
  const fired = parseInt((await env.STATE.get(CURSOR_KEY)) ?? "-1", 10);
  if (newest <= fired) return;

  await dispatch(env);
  await env.STATE.put(CURSOR_KEY, String(newest));
}

async function peekUpdates(env) {
  // No offset (nothing is acknowledged) and no allowed_updates filter, so this
  // cannot interfere with the callback_query updates the approval gate needs.
  // Filtering happens below, on our side.
  const url = `${TELEGRAM_API}/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`getUpdates failed (${response.status})`);
  }
  const body = await response.json();
  return body.result ?? [];
}

function isRequest(update, env) {
  const message = update.message;
  const text = (message?.text ?? "").trim().toLowerCase();
  if (!text.startsWith(REQUEST_COMMAND)) return false;
  // Only the configured chat can start a render; everything downstream spends
  // real money.
  return String(message.chat?.id ?? "") === env.TELEGRAM_CHAT_ID;
}

async function dispatch(env) {
  const response = await fetch(`${GITHUB_API}/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "telegram-make-trigger",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: DISPATCH_EVENT }),
  });
  if (!response.ok) {
    throw new Error(
      `repository_dispatch failed (${response.status}): ${await response.text()}`
    );
  }
}
