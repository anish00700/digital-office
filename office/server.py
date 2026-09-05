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

from . import config, roster

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
        if route == "/api/setup":
            return self._json({
                "needed": roster.setup_needed(self.office.store),
                "packs": roster.packs(),
                "principal": self.office.principal(),
            })
        if route == "/api/export":
            body = json.dumps(self.office.export_office(), indent=2, default=str)
            stamp = time.strftime("%Y%m%d-%H%M")
            return self._send(200, body, "application/json", {
                "Content-Disposition": f'attachment; filename="office-{stamp}.json"',
            })
        if route == "/api/files":
            return self._json({"files": self._workspace_listing()})
        if route.startswith("/api/file/"):
            return self._workspace_file(
                urllib.parse.unquote(route[len("/api/file/"):]))
        if route == "/api/roster":
            return self._json({
                "roster": [roster.as_dict(r, full=True) for r in config.ROSTER],
                "catalogue": roster.catalogue(),
                "tool_help": self.office.tool_help(),
            })
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
            self.office.submit_user_message(text, body.get("to"))
            return self._json({"ok": True})

        if route == "/api/approval":
            approval_id = body.get("id")
            approved = bool(body.get("approved"))
            row = self.office.decide_approval(
                approval_id, approved, (body.get("response") or "").strip())
            if row is None:
                return self._json({"error": "unknown or already decided"}, 404)
            return self._json({"ok": True, "status": row["status"]})

        if route == "/api/setup":
            try:
                self.office.install_pack((body.get("pack") or "").strip(),
                                         body.get("principal") or "")
            except roster.RosterError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True})

        if route == "/api/principal":
            text = (body.get("principal") or "").strip()
            if not 3 <= len(text) <= 400:
                return self._json({"error": "principal must be 3-400 characters"}, 400)
            self.office.store.set_setting("principal", text)
            return self._json({"ok": True, "principal": text})

        if route == "/api/import":
            try:
                installed, retired = self.office.import_office(body)
            except roster.RosterError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True, "installed": installed,
                               "retired": retired})

        if route.startswith("/api/roster/"):
            return self._roster_write(route.rsplit("/", 1)[-1], body)

        return self._send(404, "not found", "text/plain")

    def _roster_write(self, action, body):
        """Hire, fire, or change one employee. RosterError carries a message
        written for the person reading it, so it goes straight through."""
        try:
            if action == "hire":
                role = self.office.hire_employee(body)
            elif action == "fire":
                role = self.office.fire_employee((body.get("id") or "").strip())
            elif action == "update":
                agent_id = (body.get("id") or "").strip()
                fields = {k: v for k, v in body.items() if k != "id"}
                role = self.office.update_employee(agent_id, fields)
            else:
                return self._send(404, "not found", "text/plain")
        except roster.RosterError as exc:
            return self._json({"error": str(exc)}, 400)
        except KeyError:
            return self._json({"error": "no such employee"}, 404)
        return self._json({"ok": True, "employee": roster.as_dict(role, full=True)})

    do_HEAD = do_GET

    # -- payloads ------------------------------------------------------
    def _state(self):
        store = self.office.store
        return {
            "roster": [roster.as_dict(r, full=True) for r in config.ROSTER],
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
            "setup_needed": roster.setup_needed(store),
            "principal": self.office.principal(),
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

    # -- workspace ------------------------------------------------------
    # Anything an agent writes lands in the workspace. Without a way to see and
    # pull those files out, work an agent finished is work you cannot collect.
    def _workspace_listing(self):
        root = config.WORKSPACE.resolve()
        if not root.is_dir():
            return []
        out = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            stat = path.stat()
            out.append({
                "path": str(rel),
                "name": path.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        out.sort(key=lambda f: f["modified"], reverse=True)
        return out[:400]

    def _workspace_file(self, relative):
        """Serve one workspace file. Resolved and re-checked against the root,
        so `..` and absolute paths cannot climb out of it."""
        root = config.WORKSPACE.resolve()
        if not relative:
            return self._send(404, "not found", "text/plain")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._send(403, "forbidden", "text/plain")
        if target.is_symlink() or not target.is_file():
            return self._send(404, "not found", "text/plain")

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        inline = ctype.startswith("text/") or ctype in (
            "application/json", "application/javascript")
        if inline:
            ctype += "; charset=utf-8"
        disposition = "inline" if inline else "attachment"
        return self._send(200, target.read_bytes(), ctype, {
            "Content-Disposition": f'{disposition}; filename="{target.name}"',
            "Cache-Control": "no-cache",
        })

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
