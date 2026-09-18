"""Push notifications: one outbound channel, opt-in.

On a VPS the common case is that nobody is looking. An approval that sits
unseen for 15 minutes expires; a "needs you" task waits for nothing. This
sends a short push for the few events that need a human, to an ntfy topic
(zero setup: pick a topic name, subscribe on your phone) or any webhook that
takes a JSON POST.

Rules that keep a notification log from becoming a leak: one push per event
type per minute; bodies capped at 200 characters; never the arguments of a
tool call - a shell command in a push is a secret in somebody else's log.
"""

import json
import logging
import queue
import threading
import time
import urllib.request

from . import config, redact

log = logging.getLogger("office.notify")

BODY_MAX = 200
PER_KIND_S = 60


class Notifier:
    def __init__(self, url, token="", post=None):
        self.url = url
        self.token = token
        self._post = post or self._http_post
        self._last = {}            # kind -> last sent ts
        self._lock = threading.Lock()
        self.sent = 0

    @property
    def enabled(self):
        return bool(self.url)

    def send(self, kind, title, body="", priority="default"):
        """Rate-limited, size-capped, never raises. Returns True when sent."""
        if not self.enabled:
            return False
        now = time.time()
        with self._lock:
            if now - self._last.get(kind, 0) < PER_KIND_S:
                return False
            self._last[kind] = now
        # Titles only in production: a push is a copy of the office's words
        # in a service you do not run. And never a secret, in either field.
        body = "" if config.NOTIFY_TITLES_ONLY else " ".join((body or "").split())[:BODY_MAX]
        title = " ".join((title or "").split())[:80]
        body, _ = redact.redact(body)
        title, _ = redact.redact(title)
        try:
            self._post(title, body, priority)
            self.sent += 1
            return True
        except Exception as exc:
            log.warning("notification failed (%s): %s", kind, exc)
            return False

    def _http_post(self, title, body, priority):
        headers = {"User-Agent": "digital-office"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if "ntfy" in self.url:
            data = (body or title).encode("utf-8")
            headers["Title"] = title.encode("ascii", "ignore").decode()
            headers["Priority"] = {"high": "high", "low": "low"}.get(priority, "default")
        else:
            data = json.dumps({"title": title, "message": body, "text": f"{title}: {body}",
                               "priority": priority}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(64)

    # -- what to push -------------------------------------------------------
    def consider(self, event, names=None):
        """Map one office event to at most one push. `names` resolves an
        agent id to a display name."""
        etype = event.get("type") or ""
        p = event.get("payload") or {}
        who = (names or {}).get(event.get("agent_id"), event.get("agent_id") or "someone")
        if etype == "approval.requested":
            kind = p.get("kind") or "action"
            if kind == "question":
                return self.send("approval", f"{who} has a question",
                                 p.get("action") or "", "high")
            # Never the action itself: a command string does not belong in a
            # notification log. The kind and the task title are enough.
            return self.send("approval", f"{who} needs your approval",
                             f"{kind}: {p.get('detail') or 'open the office to decide'}", "high")
        if etype == "agent.message_user" and event.get("agent_id") == config.MANAGER_ID \
                and not p.get("routed"):
            return self.send("reply", f"{who} replied", p.get("text") or "")
        if etype == "office.paused":
            return self.send("paused", "Office paused", p.get("reason") or "", "low")
        if etype == "task.updated":
            status = p.get("status")
            if p.get("sensitive"):
                # A sensitive task's title is the most that leaves the office.
                if status in ("needs_you", "failed"):
                    return self.send(status, f"{who}: a sensitive task needs you", "", "high")
                return False
            if status == "needs_you":
                return self.send("needs_you", f"{who} needs you",
                                 p.get("title") or "a task is waiting on your decision", "high")
            if status == "failed" and p.get("routine"):
                return self.send("routine", f"Routine failed ({who})",
                                 p.get("title") or "", "default")
        return False


def start(bus, notifier, names):
    """A daemon thread that reads the bus and pushes what matters. Runs
    outside the office loop so a slow webhook never delays real work."""
    if not notifier.enabled:
        return None
    q = bus.subscribe()

    def loop():
        while True:
            try:
                event = q.get()
            except Exception:
                return
            try:
                notifier.consider(event, names())
            except Exception:
                log.exception("notifier crashed on %s", event.get("type"))

    thread = threading.Thread(target=loop, name="notify", daemon=True)
    thread.start()
    log.info("notifications on: %s", notifier.url.split("?")[0][:60])
    return thread
