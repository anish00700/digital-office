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

from . import config, roster, routines as routines_mod, tools

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
            # Liveness is unauthenticated so a proxy or watchdog can poll it;
            # the detailed view (queue depths, worker names) needs the token.
            if self._authorized():
                return self._json(self.office.health())
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
                "lessons": self.office.store.lessons(agent_id),
            })
        if route == "/api/routines":
            return self._json({"routines": self._routines(), "help": routines_mod.HELP})
        if route == "/api/safety":
            return self._json({**self.office.safety(),
                               "always_ask": list(config.SHELL_ALWAYS_ASK),
                               "sensitive_paths": list(config.SENSITIVE_PATHS),
                               "egress_allow": list(config.EGRESS_ALLOW),
                               "profile": config.PROFILE, "sandbox": config.SANDBOX})
        if route == "/api/audit":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            limit = min(1000, max(1, int(qs.get("limit", ["200"])[0])))
            return self._json({"audit": self.office.store.audit_rows(limit),
                               "counts": self.office.store.audit_counts(
                                   time.time() - 7 * 86400)})
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
        if route == "/api/usage":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._json(self._usage(qs.get("window", ["session"])[0]))
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
            info = self.office.submit_user_message(text, body.get("to")) or {}
            return self._json({"ok": True, **info})

        if route == "/api/pause":
            reason = (body.get("reason") or "paused by you").strip()[:120]
            self.office.pause(reason)
            return self._json({"ok": True, "paused": reason})

        if route == "/api/resume":
            self.office.resume()
            return self._json({"ok": True})

        if route == "/api/lockdown":
            reason = (body.get("reason") or "locked down by you").strip()[:120]
            self.office.lockdown(reason)
            return self._json({"ok": True, "locked": reason})

        if route == "/api/unlock":
            self.office.unlock()
            return self._json({"ok": True})

        if route == "/api/task/cancel":
            row = self.office.cancel_task((body.get("id") or "").strip())
            if row is None:
                return self._json({"error": "no such task, or it already finished"}, 404)
            return self._json({"ok": True, "id": row["id"]})

        if route == "/api/approval":
            approval_id = body.get("id")
            approved = bool(body.get("approved"))
            always = None
            if approved and body.get("always_allow"):
                pending = {a["id"]: a for a in self.office.store.pending_approvals()}
                row = pending.get(approval_id)
                if row and row["kind"] == "shell":
                    always = self.office.always_allow(row["action"])
            row = self.office.decide_approval(
                approval_id, approved, (body.get("response") or "").strip())
            if row is None:
                return self._json({"error": "unknown or already decided"}, 404)
            return self._json({"ok": True, "status": row["status"], "always_allow": always})

        if route == "/api/task/feedback":
            out = self.office.set_feedback((body.get("id") or "").strip(),
                                           bool(body.get("up")), body.get("note") or "")
            if out is None:
                return self._json({"error": "no such task, or it is still running"}, 404)
            return self._json({"ok": True, **out})

        if route == "/api/task/retry":
            row = self.office.retry_task((body.get("id") or "").strip())
            if row is None:
                return self._json({"error": "only a finished task can be retried"}, 404)
            return self._json({"ok": True, "id": row["id"]})

        if route == "/api/lessons":
            agent_id = (body.get("agent") or "").strip()
            if agent_id not in config.STAFF_IDS and agent_id != config.MANAGER_ID:
                return self._json({"error": "no such employee"}, 404)
            if body.get("action") == "forget":
                self.office.store.forget_lesson(agent_id, int(body.get("lesson_id") or 0))
            elif body.get("action") == "add":
                if not self.office.store.add_lesson(agent_id, body.get("text") or ""):
                    return self._json({"error": "too short, or already known"}, 400)
            else:
                return self._json({"error": "action must be add or forget"}, 400)
            self.office.bus.publish("lesson.changed", agent_id=agent_id)
            return self._json({"ok": True, "lessons": self.office.store.lessons(agent_id)})

        if route == "/api/settings":
            if "careful_mode" in body:
                on = self.office.set_careful_mode(bool(body.get("careful_mode")))
                return self._json({"ok": True, "careful_mode": on,
                                   "reviewer": config.REVIEWER_ID
                                   if config.REVIEWER_ID in config.STAFF_IDS else None})
            return self._json({"error": "nothing to set"}, 400)

        if route == "/api/routines":
            return self._routine_write(body)

        if route == "/api/safety":
            out = self.office.set_safety(body.get("allow"), body.get("deny"))
            return self._json({"ok": True, **out})

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

    def _routines(self):
        out = []
        by_id = {r.id: r for r in config.ROSTER}
        for r in self.office.store.routines():
            row = dict(r)
            try:
                row["schedule_text"] = routines_mod.describe(r["schedule"])
            except ValueError:
                row["schedule_text"] = r["schedule"]
            who = by_id.get(r["assignee"])
            row["assignee_name"] = who.name if who else r["assignee"]
            row["assignee_emoji"] = who.emoji if who else "👤"
            out.append(row)
        return out

    def _routine_write(self, body):
        action = (body.get("action") or "").strip()
        try:
            if action == "add":
                rid = self.office.add_routine(
                    (body.get("title") or "").strip(), (body.get("assignee") or "").strip(),
                    (body.get("brief") or "").strip(), body.get("schedule") or "")
                return self._json({"ok": True, "id": rid, "routines": self._routines()})
            rid = (body.get("id") or "").strip()
            if action == "update":
                fields = {k: v for k, v in body.items() if k in
                          ("enabled", "schedule", "title", "brief", "assignee")}
                if self.office.update_routine(rid, **fields) is None:
                    return self._json({"error": "no such routine"}, 404)
            elif action == "delete":
                if self.office.delete_routine(rid) is None:
                    return self._json({"error": "no such routine"}, 404)
            elif action == "run":
                if self.office.run_routine_now(rid) is None:
                    return self._json({"error": "no such routine"}, 404)
            else:
                return self._json({"error": "action must be add, update, delete or run"}, 400)
        except (ValueError, KeyError) as exc:
            return self._json({"error": str(exc).strip("'\"")}, 400)
        return self._json({"ok": True, "routines": self._routines()})

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
            "approvals": [{**dict(a), "prefix": tools.prefix_for(a["action"])
                           if a["kind"] == "shell" else ""}
                          for a in store.pending_approvals()],
            "spend": {
                "total": store.spend(),
                "day": store.spend_since(time.time() - 86400),
                "session": store.spend_since(time.time() - config.SESSION_WINDOW_S),
                "daily_budget": config.DAILY_BUDGET_USD,
                "token_budget": config.SESSION_TOKEN_BUDGET,
                "session_window_seconds": config.SESSION_WINDOW_S,
            },
            "backend": self.office.backend.name,
            "auth": self.office.backend.describe_auth(),
            "paused": self.office._paused_reason or None,
            "paused_until": self.office._paused_until or None,
            "locked": self.office.locked_reason or None,
            "profile": config.PROFILE,
            "sandbox": config.SANDBOX,
            "front_desk": bool(config.ROUTER and hasattr(self.office.backend, "structured")),
            "careful_mode": self.office.careful_mode(),
            "reviewer": config.REVIEWER_ID if config.REVIEWER_ID in config.STAFF_IDS else None,
            "notifications": bool(config.NOTIFY_URL),
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

    # -- usage ----------------------------------------------------------
    WINDOWS = {
        "session": None,          # resolved from config at call time
        "day": 86400,
        "week": 7 * 86400,
        "all": None,
    }

    @staticmethod
    def _cost_split(rows):
        """Where the money actually went, by token kind.

        The SDK reports one total and no breakdown, so this apportions it using
        list prices. Worth doing because volume and cost point in opposite
        directions here: cache reads are usually the largest column of tokens
        and the smallest column of spend, and a panel that only counts tokens
        makes the cheapest thing in the office look like the problem.
        """
        kinds = {k: {"tokens": 0, "cost": 0.0}
                 for k in ("input", "output", "cache_read", "cache_write")}
        for r in rows:
            pin, pout = config.PRICING.get(r["model"], (2.0, 10.0))
            rates = {
                "input": pin,
                "output": pout,
                "cache_read": pin * config.CACHE_READ_DISCOUNT,
                "cache_write": pin * config.CACHE_WRITE_MULTIPLIER,
            }
            for kind, col in (("input", "input_tokens"), ("output", "output_tokens"),
                              ("cache_read", "cache_read"), ("cache_write", "cache_write")):
                n = r[col] or 0
                kinds[kind]["tokens"] += n
                kinds[kind]["cost"] += n * rates[kind] / 1_000_000
        total = sum(k["cost"] for k in kinds.values()) or 1.0
        out = []
        for name, v in kinds.items():
            out.append({"kind": name, "tokens": v["tokens"],
                        "cost": round(v["cost"], 6),
                        "share": round(v["cost"] / total * 100, 1)})
        out.sort(key=lambda k: k["cost"], reverse=True)
        return out

    @staticmethod
    def _advice(agent, role):
        """Concrete, evidence-backed suggestions. Only fires on the levers that
        actually move cost, and only when this employee's own numbers justify
        it - generic advice about being efficient helps nobody."""
        tips = []
        turns = agent["turns"] or 1
        out_per_turn = agent["output"] / turns
        write_per_turn = agent["cache_write"] / turns

        if role and role.model_id == "opus" and out_per_turn < 700:
            tips.append(f"On opus but averaging {out_per_turn:.0f} output tokens a "
                        f"turn. Opus output is 2.5x sonnet; sonnet would likely do.")
        if out_per_turn > 1200:
            tips.append(f"Averaging {out_per_turn:.0f} output tokens a turn, and "
                        f"output is the most expensive token there is. Ask for a "
                        f"length limit in the persona.")
        if role and write_per_turn > 3000:
            tips.append(f"{write_per_turn:.0f} tokens of cache written per turn - "
                        f"that is the persona plus tool definitions being re-cached. "
                        f"A shorter persona or fewer tools shrinks it.")
        if role and role.max_turns > 10 and turns >= 2:
            tips.append(f"max_turns is {role.max_turns}. Every extra round trip "
                        f"re-reads the whole prompt; lower it if the work does not "
                        f"need the headroom.")
        if role and len(role.office_tools) + len(role.native_tools) > 9:
            tips.append(f"Holding {len(role.office_tools) + len(role.native_tools)} "
                        f"tools. Every definition is re-sent and re-cached on every "
                        f"request, whether or not it gets used.")
        return tips

    def _usage(self, window):
        store = self.office.store
        now = time.time()
        span = config.SESSION_WINDOW_S if window == "session" \
            else self.WINDOWS.get(window, 86400)
        since = 0.0 if window == "all" or span is None and window == "all" \
            else (now - (span or config.SESSION_WINDOW_S))
        if window == "all":
            since = 0.0

        by_id = {r.id: r for r in config.ROSTER}
        rows = store.usage_recent(since, limit=5000)
        done = store.tasks_done_by(since)
        agents = []
        for row in store.usage_by_agent(since):
            role = by_id.get(row["agent_id"])
            turns = row["turns"] or 1
            mine = [r for r in rows if r["agent_id"] == row["agent_id"]]
            desk = row["agent_id"] == "router"
            agents.append({
                **row,
                "name": "Front desk" if desk else (role.name if role else row["agent_id"]),
                "emoji": "🛎️" if desk else (role.emoji if role else "👤"),
                "color": role.color if role else "#8a8580",
                "departed": role is None and not desk,
                "model_id": role.model_id if role else "",
                "max_turns": role.max_turns if role else 0,
                "tool_count": (len(role.office_tools) + len(role.native_tools)) if role else 0,
                "cost_per_turn": round((row["cost"] or 0) / turns, 6),
                "tasks": done.get(row["agent_id"], 0),
                "cost_split": self._cost_split(mine),
                "advice": self._advice(row, role),
            })
        return {
            "window": window,
            "since": since,
            "now": now,
            "window_seconds": 0 if window == "all" else (now - since),
            "first_turn": store.usage_first_ts(since),
            "totals": store.usage_totals(since),
            "cost_split": self._cost_split(rows),
            "by_agent": agents,
            "by_model": store.usage_by_model(since),
            "recent": store.usage_recent(since, limit=40),
            "token_budget": config.SESSION_TOKEN_BUDGET,
            "session_window_seconds": config.SESSION_WINDOW_S,
            "router": store.router_stats(since),
        }

    # -- workspace ------------------------------------------------------
    # Anything an agent writes lands in the workspace. Without a way to see and
    # pull those files out, work an agent finished is work you cannot collect.
    def _workspace_listing(self):
        root = config.WORKSPACE.resolve()
        if not root.is_dir():
            return []
        out = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            # Every component, not just the filename. Checking only the name
            # listed - and served - the contents of dot-directories, so a
            # .env or a .secrets/ dropped in the workspace was downloadable.
            if any(part.startswith(".") for part in rel.parts):
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
            rel = target.relative_to(root)
        except ValueError:
            return self._send(403, "forbidden", "text/plain")
        if any(part.startswith(".") for part in rel.parts):
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


class _Server(ThreadingHTTPServer):
    """A browser closing an SSE stream is normal, not an error.

    The default handler dumps a full traceback for every dropped connection,
    so a page reload writes a stack trace and a dozen of them bury whatever
    actually went wrong.
    """

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            log.debug("client %s went away: %s", client_address[0], exc)
            return
        super().handle_error(request, client_address)


def serve(office):
    Handler.office = office
    httpd = _Server((config.HOST, config.PORT), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    thread.start()
    log.info("listening on http://%s:%d", config.HOST, config.PORT)
    return httpd
