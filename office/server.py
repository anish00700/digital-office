"""HTTP + SSE front door.

Deliberately thin: it reads from the store, writes to the office's queues, and
streams events. All the state lives in SQLite, which is what lets the browser be
a pure viewer - close the tab and the office keeps working.
"""

import http.cookies
import json
import logging
import mimetypes
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

log = logging.getLogger("office.http")

LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<title>Digital Office</title>
<style>body{font:15px system-ui;background:#14110e;color:#e8ded0;display:grid;
place-items:center;height:100vh;margin:0}form{display:flex;gap:8px}
input{padding:10px 12px;border-radius:8px;border:1px solid #3a3128;background:#1d1913;
color:inherit;font:inherit}button{padding:10px 16px;border-radius:8px;border:0;
background:#c2703d;color:#fff;font:inherit;cursor:pointer}</style>
<form onsubmit="document.cookie='office_token='+encodeURIComponent(t.value)+
';path=/;max-age=31536000;samesite=strict';location='/';return false">
<input id=t type=password placeholder="Access token" autofocus>
<button>Enter</button></form>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    office = None

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    # -- helpers -------------------------------------------------------
    def _authorized(self):
        if not config.TOKEN:
            return True
        if self.headers.get("X-Office-Token") == config.TOKEN:
            return True
        raw = self.headers.get("Cookie")
        if raw:
            cookie = http.cookies.SimpleCookie(raw)
            if "office_token" in cookie and cookie["office_token"].value == config.TOKEN:
                return True
        qs = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(qs).get("token", [""])[0] == config.TOKEN

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, default=str), "application/json")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routing -------------------------------------------------------
    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/api/health":
            return self._json({"ok": True, "backend": self.office.backend.name})
        if not self._authorized():
            return self._send(401, LOGIN_PAGE, "text/html; charset=utf-8")

        if route in ("/", "/index.html"):
            return self._file("index.html")
        if route.startswith("/static/"):
            return self._file(route[len("/static/"):])
        if route == "/api/state":
            return self._json(self._state())
        if route == "/api/stream":
            return self._stream()
        if route.startswith("/api/agent/"):
            agent_id = route.rsplit("/", 1)[-1]
            return self._json({
                "id": agent_id,
                "transcript": self.office.store.transcript(agent_id),
            })
        if route == "/api/tasks":
            return self._json({"tasks": self.office.store.tasks()})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return self._send(401, "unauthorized", "text/plain")
        body = self._body()

        if route == "/api/message":
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"error": "empty"}, 400)
            self.office.submit_user_message(text)
            return self._json({"ok": True})

        if route == "/api/approval":
            approval_id = body.get("id")
            approved = bool(body.get("approved"))
            row = self.office.decide_approval(
                approval_id, approved, (body.get("response") or "").strip())
            if row is None:
                return self._json({"error": "unknown or already decided"}, 404)
            return self._json({"ok": True, "status": row["status"]})

        return self._send(404, "not found", "text/plain")

    do_HEAD = do_GET

    # -- payloads ------------------------------------------------------
    def _state(self):
        store = self.office.store
        return {
            "roster": [
                {"id": r.id, "name": r.name, "title": r.title, "emoji": r.emoji,
                 "color": r.color, "desk": list(r.desk),
                 "manager": r.id == config.MANAGER_ID}
                for r in config.ROSTER
            ],
            "agents": store.agents(),
            "tasks": store.tasks(limit=60),
            "messages": store.messages(limit=60),
            "approvals": store.pending_approvals(),
            "spend": {
                "total": store.spend(),
                "day": store.spend_since(time.time() - 86400),
                "daily_budget": config.DAILY_BUDGET_USD,
            },
            "backend": self.office.backend.name,
            "auth": self.office.backend.describe_auth(),
            "started_at": self.office.started_at,
            "now": time.time(),
            "seq": store.max_event_seq(),
        }

    def _stream(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        since = int(qs.get("since", ["0"])[0])
        sub = self.office.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx: do not buffer SSE
        self.end_headers()
        try:
            for event in self.office.store.events_since(since, limit=300):
                self._event(event)
                since = event["seq"]
            while True:
                try:
                    event = sub.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event["seq"] > since:
                    self._event(event)
                    since = event["seq"]
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.office.bus.unsubscribe(sub)

    def _event(self, event):
        payload = json.dumps(event, default=str)
        self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _file(self, relative):
        target = (config.WEB_DIR / relative).resolve()
        try:
            target.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            return self._send(403, "forbidden", "text/plain")
        if not target.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


def serve(office):
    Handler.office = office
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("listening on http://%s:%d", config.HOST, config.PORT)
    return httpd
