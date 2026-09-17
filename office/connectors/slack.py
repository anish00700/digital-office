"""Slack connector for the Comms desk.

Read-only by design. Iris can see messages; she cannot post, reply, or react.
That boundary is deliberate - an agent that can write to your Slack is an agent
that can embarrass you while you sleep.

To enable:
  1. Create a Slack app at https://api.slack.com/apps
  2. Add the user-token scopes: channels:history, groups:history, im:history,
     mpim:history, users:read
  3. Install to your workspace and export the token:
       export SLACK_TOKEN=xoxp-...
  4. Optionally limit which channels are swept:
       export SLACK_CHANNELS=C012ABCDEF,C034GHIJKL
"""

import json
import os
import time
import urllib.parse
import urllib.request

from .. import config

API = "https://slack.com/api"


def _configured():
    return bool(config.secret("SLACK_TOKEN"))


def _call(method, token, **params):
    url = f"{API}/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def fetch(since_hours=12):
    token = config.secret("SLACK_TOKEN")
    if not token:
        return ("Slack is not configured. Set SLACK_TOKEN to enable this desk. "
                "Report this to your principal rather than guessing at content.")

    oldest = time.time() - since_hours * 3600
    channels = [c for c in config.secret("SLACK_CHANNELS", "").split(",") if c.strip()]

    try:
        if not channels:
            conv = _call("conversations.list", token, limit=50,
                         types="public_channel,private_channel,im")
            if not conv.get("ok"):
                return f"Slack API error: {conv.get('error')}"
            channels = [c["id"] for c in conv.get("channels", [])]

        out = []
        for cid in channels[:25]:
            hist = _call("conversations.history", token, channel=cid,
                         oldest=str(oldest), limit=30)
            if not hist.get("ok"):
                continue
            for msg in hist.get("messages", []):
                if msg.get("subtype"):
                    continue
                who = msg.get("user") or msg.get("bot_id") or "unknown"
                text = " ".join((msg.get("text") or "").split())[:400]
                if text:
                    out.append(f"[{cid}] {who}: {text}")
        if not out:
            return f"No Slack messages in the last {since_hours}h."
        return "\n".join(out[:120])
    except Exception as exc:
        return f"Slack fetch failed: {type(exc).__name__}: {exc}"
