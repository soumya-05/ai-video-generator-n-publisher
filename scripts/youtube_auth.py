#!/usr/bin/env python3
"""Mint a YouTube refresh token.

A refresh token is bound to the OAuth client that issued it, so it must be
re-minted whenever the client ID or secret changes. Run this locally (it opens
a browser), then store the printed token as the YOUTUBE_REFRESH_TOKEN secret.

    python scripts/youtube_auth.py

The OAuth client in Google Cloud Console must be of type "Desktop app", which
is what permits the loopback redirect used here.
"""

import http.server
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PORT = 8080
REDIRECT_URI = f"http://localhost:{PORT}"

# upload: publish videos and set thumbnails. readonly: verify the channel.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

_received = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _received.update({k: v[0] for k, v in query.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _received
        self.wfile.write(
            ("<h2>You can close this tab and return to the terminal.</h2>"
             if ok else
             f"<h2>Authorisation failed: {_received.get('error')}</h2>").encode()
        )

    def log_message(self, *args):
        pass  # keep the console clean


def main() -> int:
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "Set the OAuth client first, e.g.:\n"
            "  read -s YOUTUBE_CLIENT_SECRET && export YOUTUBE_CLIENT_SECRET\n"
            "  export YOUTUBE_CLIENT_ID='....apps.googleusercontent.com'\n"
            "Both come from the SAME client in Google Cloud Console >\n"
            "APIs & Services > Credentials.",
            file=sys.stderr,
        )
        return 1

    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # Both are required to get a refresh token back: offline asks for one,
        # and consent forces a fresh one even if you have approved before.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("localhost", PORT), Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"Opening your browser. If nothing happens, visit:\n\n{url}\n")
        webbrowser.open(url)
        print("Waiting for Google to redirect back...")
        while "code" not in _received and "error" not in _received:
            threading.Event().wait(0.3)
        httpd.shutdown()

    if "error" in _received:
        print(f"Authorisation failed: {_received['error']}", file=sys.stderr)
        return 1
    if _received.get("state") != state:
        print("State mismatch - aborting.", file=sys.stderr)
        return 1

    resp = requests.post(
        TOKEN_URL,
        data={
            "code": _received["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}",
              file=sys.stderr)
        return 1

    payload = resp.json()
    refresh = payload.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Revoke the app's access at "
              "https://myaccount.google.com/permissions and run this again.",
              file=sys.stderr)
        return 1

    # Confirm the token actually reaches a channel before the user stores it.
    channels = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {payload['access_token']}"},
        timeout=60,
    )
    items = channels.json().get("items", []) if channels.status_code == 200 else []
    if items:
        print(f"\nAuthorised channel: {items[0]['snippet']['title']} "
              f"(id {items[0]['id']})")
    else:
        print("\nWARNING: this Google account has no YouTube channel yet. "
              "Create one before uploading.")

    print("\nRefresh token (store it, it is not shown again):\n")
    print(refresh)
    print("\nStore it without echoing to your shell history:")
    print("  gh secret set YOUTUBE_REFRESH_TOKEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
