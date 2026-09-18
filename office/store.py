"""SQLite persistence. One connection, one lock, WAL mode.

Everything the office knows lives here, which is what lets the GUI be a pure
viewer: close the browser, reopen it, and the office replays from this file.
"""

import json
import sqlite3
import threading
import time
import uuid

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'idle',
    detail TEXT DEFAULT '',
    current_task TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    assignee TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    agent_id TEXT,
    task_id TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,
    task_id TEXT
);
CREATE TABLE IF NOT EXISTS transcript (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL,
    response TEXT,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    author TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    due_at REAL NOT NULL,
    text TEXT NOT NULL,
    created_by TEXT,
    fired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    assignee TEXT NOT NULL,
    brief TEXT NOT NULL,
    schedule TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    next_run REAL,
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    emoji TEXT NOT NULL,
    color TEXT NOT NULL,
    desk_x INTEGER NOT NULL,
    desk_y INTEGER NOT NULL,
    persona TEXT NOT NULL,
    office_tools TEXT NOT NULL DEFAULT '[]',
    native_tools TEXT NOT NULL DEFAULT '[]',
    skills TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT 'low',
    max_turns INTEGER NOT NULL DEFAULT 8,
    reports_to TEXT NOT NULL DEFAULT 'manager',
    active INTEGER NOT NULL DEFAULT 1,
    hired_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lessons_agent ON lessons(agent_id, ts);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_transcript_agent ON transcript(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _drop_skill(db, name):
    """Remove one skill grant (by bare name or plugin:name) from every roster
    row that holds it. Used by a migration; safe to run twice."""
    rows = db.execute("SELECT id, skills FROM roster").fetchall()
    for row in rows:
        try:
            skills = list(json.loads(row["skills"] or "[]"))
        except (TypeError, ValueError):
            continue
        kept = [sk for sk in skills
                if sk != name and not str(sk).endswith(":" + name)
                and not str(sk).startswith(name + ":")]
        if kept != skills:
            db.execute("UPDATE roster SET skills=? WHERE id=?",
                       (json.dumps(kept), row["id"]))


def _refresh_default_persona(db, role_id):
    """Replace a roster persona with the current shipped default, but only if
    it still matches an *earlier* shipped default - i.e. the owner never
    edited it. Compared with whitespace collapsed, ignoring the sentence(s)
    the new default added."""
    from . import config
    role = config.ROLE_DEFS.get(role_id)
    row = db.execute("SELECT persona FROM roster WHERE id=?", (role_id,)).fetchone()
    if role is None or row is None:
        return
    def norm(t):
        return " ".join((t or "").split())
    current, shipped = norm(row["persona"]), norm(role.persona)
    if current == shipped:
        return
    # An older default is the new one minus what was appended; accept it when
    # every sentence of the old text still appears, in order, in the new one.
    if current and current in shipped:
        db.execute("UPDATE roster SET persona=? WHERE id=?", (role.persona, role_id))


def _add_tool_to_workers(db, tool):
    for row in db.execute("SELECT id, office_tools FROM roster WHERE active=1").fetchall():
        try:
            tools = list(json.loads(row["office_tools"] or "[]"))
        except (TypeError, ValueError):
            continue
        if "finish" in tools and tool not in tools:
            _add_tool(db, row["id"], tool)


def _add_tool(db, role_id, tool):
    """Grant one office tool to one roster row if it lacks it. Migration
    helper; safe to run twice."""
    row = db.execute("SELECT office_tools FROM roster WHERE id=?", (role_id,)).fetchone()
    if row is None:
        return
    try:
        tools = list(json.loads(row["office_tools"] or "[]"))
    except (TypeError, ValueError):
        return
    if tool not in tools:
        tools.append(tool)
        db.execute("UPDATE roster SET office_tools=? WHERE id=?",
                   (json.dumps(tools), role_id))


class Store:
    def __init__(self, path=None):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    # Ordered, named, recorded in `settings` as migrated:<name>. Each step is
    # either an ALTER TABLE ... ADD COLUMN (skipped when the column exists) or
    # a callable taking the connection. Every step must be safe to re-run,
    # because the settings row is written after the step.
    MIGRATIONS = (
        ("roster.skills",
         "ALTER TABLE roster ADD COLUMN skills TEXT NOT NULL DEFAULT '[]'"),
        # Why a run ended - end_turn, max_turns, budget_exhausted, ... A task
        # cut off by a cap used to be stored as done with half an answer.
        ("tasks.stop", "ALTER TABLE tasks ADD COLUMN stop TEXT"),
        # The manager's session-handoff skill widened the SDK's setting
        # sources, which loaded the workspace CLAUDE.md - rewritten after
        # every task - into every manager request. Offices seeded before the
        # default changed still carry the grant in their roster row.
        ("roster.no_session_handoff", lambda db: _drop_skill(db, "session-handoff")),
        # Which task a model call belonged to, and how many API turns it took.
        # Without these the ledger says who spent, never on what.
        ("usage.task_id", "ALTER TABLE usage ADD COLUMN task_id TEXT"),
        ("usage.turns", "ALTER TABLE usage ADD COLUMN turns INTEGER DEFAULT 0"),
        # Miles had no clock: asked the time he answered "no exact clock time
        # available". The DB row wins over the config default, so upgrade it.
        ("roster.manager_now", lambda db: _add_tool(db, "manager", "now")),
        # Your verdict on a finished task, kept on its row so the card shows it.
        ("tasks.feedback", "ALTER TABLE tasks ADD COLUMN feedback TEXT"),
        ("roster.manager_add_routine", lambda db: _add_tool(db, "manager", "add_routine")),
        # Iris's persona now names the <untrusted-data> tag her fetches arrive
        # in. Only rows still carrying the shipped text are touched; an edited
        # persona is the owner's and stays theirs.
        ("roster.comms_untrusted_tag", lambda db: _refresh_default_persona(db, "comms")),
        # Every worker may now ask a colleague. Workers are the rows that can
        # `finish`; the manager delegates instead and is left alone.
        ("roster.workers_ask_colleague",
         lambda db: _add_tool_to_workers(db, "ask_colleague")),
    )

    def _migrate(self):
        """CREATE TABLE IF NOT EXISTS never adds a column to a table that
        already exists, so anything added after the first release needs this."""
        done = {r[0] for r in self.db.execute(
            "SELECT key FROM settings WHERE key LIKE 'migrated:%'")}
        for name, step in self.MIGRATIONS:
            if f"migrated:{name}" in done:
                continue
            if callable(step):
                step(self.db)
            else:
                table = step.split()[2]
                column = (step.split("ADD COLUMN", 1)[1].split()[0]
                          if "ADD COLUMN" in step else None)
                have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if column is None or column not in have:
                    self.db.execute(step)
            self.db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (f"migrated:{name}", str(time.time())))

    def _run(self, sql, args=(), *, fetch=None):
        with self._lock:
            cur = self.db.execute(sql, args)
            if fetch == "one":
                row = cur.fetchone()
                out = dict(row) if row else None
            elif fetch == "all":
                out = [dict(r) for r in cur.fetchall()]
            else:
                out = cur.lastrowid
            self.db.commit()
            return out

    # -- lessons -----------------------------------------------------------
    # What an employee has worked out about this office that is worth carrying
    # into the next task. Deliberately few and short: they are prepended to the
    # system prompt, so they are re-sent (and re-cached) on every request that
    # employee runs. A long notebook is a permanent tax, not an improvement.
    LESSON_LIMIT = 6
    LESSON_CHARS = 240

    def add_lesson(self, agent_id, text):
        text = " ".join(str(text or "").split())[:self.LESSON_CHARS]
        if len(text) < 12:
            return False
        existing = [r["text"].lower() for r in self.lessons(agent_id)]
        if text.lower() in existing:
            return False
        self._run("INSERT INTO lessons (ts, agent_id, text) VALUES (?,?,?)",
                  (time.time(), agent_id, text))
        # Keep only the newest few; the oldest fall off rather than accumulate.
        self._run(
            "DELETE FROM lessons WHERE agent_id=? AND id NOT IN"
            " (SELECT id FROM lessons WHERE agent_id=? ORDER BY ts DESC LIMIT ?)",
            (agent_id, agent_id, self.LESSON_LIMIT))
        return True

    def lessons(self, agent_id):
        return self._run(
            "SELECT id, ts, text FROM lessons WHERE agent_id=? ORDER BY ts",
            (agent_id,), fetch="all") or []

    def forget_lesson(self, agent_id, lesson_id):
        self._run("DELETE FROM lessons WHERE agent_id=? AND id=?",
                  (agent_id, lesson_id))

    # -- settings ----------------------------------------------------------
    # Office-level configuration the owner sets once, in the GUI, and which
    # therefore cannot live in environment variables.
    def setting(self, key, default=None):
        row = self._run("SELECT value FROM settings WHERE key=?", (key,), fetch="one")
        return row["value"] if row else default

    def set_setting(self, key, value):
        self._run("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                  (key, str(value)))

    def json_setting(self, key, default=None):
        raw = self.setting(key)
        if raw is None:
            return default if default is not None else []
        try:
            return json.loads(raw)
        except ValueError:
            return default if default is not None else []

    def set_json_setting(self, key, value):
        self.set_setting(key, json.dumps(value))

    # -- routines ----------------------------------------------------------
    # Recurring work you configured: "sweep the inbox at 08:00". The ticker
    # fires them; nothing here ever calls a model on its own initiative.
    def add_routine(self, title, assignee, brief, schedule, next_run, created_by=None):
        rid = new_id("rtn")
        self._run(
            "INSERT INTO routines (id,title,assignee,brief,schedule,enabled,next_run,"
            "created_by,created_at) VALUES (?,?,?,?,?,1,?,?,?)",
            (rid, title, assignee, brief, schedule, next_run, created_by, time.time()))
        return rid

    def routines(self):
        return self._run("SELECT * FROM routines ORDER BY created_at", fetch="all") or []

    def routine(self, rid):
        return self._run("SELECT * FROM routines WHERE id=?", (rid,), fetch="one")

    def update_routine(self, rid, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE routines SET {cols} WHERE id=?", (*fields.values(), rid))

    def delete_routine(self, rid):
        self._run("DELETE FROM routines WHERE id=?", (rid,))

    def due_routines(self, now=None):
        return self._run(
            "SELECT * FROM routines WHERE enabled=1 AND next_run IS NOT NULL"
            " AND next_run<=? ORDER BY next_run", (now or time.time(),), fetch="all") or []

    def open_task_for_routine(self, rid):
        """A routine that is still running from last time does not pile up."""
        return self._run(
            "SELECT id FROM tasks WHERE created_by=? AND status IN ('queued','running')"
            " LIMIT 1", (f"routine:{rid}",), fetch="one")

    # -- roster ------------------------------------------------------------
    # Who works here is state, not source. The table is seeded from
    # config.DEFAULT_ROSTER on first boot and owned by the office after that,
    # so hiring someone survives a restart the same way a task does.
    def roster_rows(self, include_departed=False):
        sql = "SELECT * FROM roster"
        if not include_departed:
            sql += " WHERE active=1"
        return self._run(sql + " ORDER BY hired_at", fetch="all")

    def roster_count(self):
        row = self._run("SELECT COUNT(*) n FROM roster", fetch="one") or {}
        return row.get("n") or 0

    def write_role(self, row):
        """Insert or replace one roster row. `row` is a plain dict of columns."""
        cols = ("id", "name", "title", "emoji", "color", "desk_x", "desk_y",
                "persona", "office_tools", "native_tools", "skills", "model",
                "effort", "max_turns", "reports_to", "active", "hired_at")
        self._run(
            f"INSERT OR REPLACE INTO roster ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            tuple(row[c] for c in cols),
        )

    def set_role_active(self, agent_id, active):
        self._run("UPDATE roster SET active=? WHERE id=?",
                  (1 if active else 0, agent_id))

    # -- agents ------------------------------------------------------------
    def ensure_agents(self, ids):
        now = time.time()
        for aid in ids:
            self._run(
                "INSERT OR IGNORE INTO agents (id, status, updated_at) VALUES (?, 'idle', ?)",
                (aid, now),
            )
        # A crashed daemon leaves stale busy states behind; reset on boot.
        self._run(
            "UPDATE agents SET status='idle', detail='', current_task=NULL, updated_at=?",
            (now,),
        )

    def ensure_agent(self, agent_id):
        """Add one agent row without disturbing anyone else's live status."""
        self._run(
            "INSERT OR IGNORE INTO agents (id, status, updated_at) VALUES (?, 'idle', ?)",
            (agent_id, time.time()),
        )

    def set_agent(self, agent_id, status, detail="", task_id=None):
        self._run(
            "UPDATE agents SET status=?, detail=?, current_task=?, updated_at=? WHERE id=?",
            (status, detail, task_id, time.time(), agent_id),
        )

    def agents(self):
        return self._run("SELECT * FROM agents", fetch="all")

    # -- housekeeping ------------------------------------------------------
    def tasks_by_status(self, statuses):
        marks = ",".join("?" * len(statuses))
        return self._run(
            f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at",
            tuple(statuses), fetch="all",
        ) or []

    def last_event_ts(self):
        row = self._run("SELECT MAX(ts) t FROM events", fetch="one") or {}
        return row.get("t")

    def prune(self, days):
        """Drop events and transcripts older than `days`, keep the newest 2000
        chat messages. Usage rows are never pruned: they are the ledger and
        they are small. Returns rows removed per table."""
        cutoff = time.time() - days * 86400
        removed = {}
        with self._lock:
            for table in ("events", "transcript"):
                cur = self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed[table] = cur.rowcount
            cur = self.db.execute(
                "DELETE FROM messages WHERE id NOT IN"
                " (SELECT id FROM messages ORDER BY id DESC LIMIT 2000)")
            removed["messages"] = cur.rowcount
            self.db.commit()
        return removed

    # -- tasks -------------------------------------------------------------
    def create_task(self, title, brief, assignee, created_by):
        tid = new_id("task")
        self._run(
            "INSERT INTO tasks (id,title,brief,assignee,created_by,status,created_at)"
            " VALUES (?,?,?,?,?,'queued',?)",
            (tid, title, brief, assignee, created_by, time.time()),
        )
        return tid

    def update_task(self, task_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def task(self, task_id):
        return self._run("SELECT * FROM tasks WHERE id=?", (task_id,), fetch="one")

    # Every task row carries what it cost. The join is cheap (usage is small
    # and indexed by task) and it is the only way a card can say
    # "12.4k tok · $0.03 · 4 turns · sonnet" instead of just "done".
    _TASK_COLS = (
        "t.*,"
        " COALESCE(SUM(u.input_tokens+u.output_tokens+u.cache_read+u.cache_write),0) tokens,"
        " COALESCE(SUM(u.cost_usd),0) cost,"
        " COALESCE(SUM(u.turns),0) turns,"
        " COUNT(u.id) calls,"
        " MAX(u.model) model"
    )

    def tasks(self, limit=200):
        return self._run(
            f"SELECT {self._TASK_COLS} FROM tasks t LEFT JOIN usage u ON u.task_id=t.id"
            " GROUP BY t.id ORDER BY t.created_at DESC LIMIT ?", (limit,), fetch="all"
        )

    def task_usage(self, task_id):
        return self._run(
            "SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_write),0) tokens,"
            " COALESCE(SUM(cost_usd),0) cost, COALESCE(SUM(turns),0) turns, COUNT(*) calls,"
            " MAX(model) model FROM usage WHERE task_id=?", (task_id,), fetch="one")

    # -- events ------------------------------------------------------------
    def add_event(self, etype, agent_id=None, task_id=None, payload=None):
        ts = time.time()
        seq = self._run(
            "INSERT INTO events (ts,type,agent_id,task_id,payload) VALUES (?,?,?,?,?)",
            (ts, etype, agent_id, task_id, json.dumps(payload or {})),
        )
        return {
            "seq": seq, "ts": ts, "type": etype, "agent_id": agent_id,
            "task_id": task_id, "payload": payload or {},
        }

    def events_since(self, seq, limit=500):
        rows = self._run(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (seq, limit), fetch="all",
        )
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def max_event_seq(self):
        row = self._run("SELECT MAX(seq) AS m FROM events", fetch="one")
        return (row or {}).get("m") or 0

    # -- messages ----------------------------------------------------------
    def add_message(self, sender, recipient, body, task_id=None):
        return self._run(
            "INSERT INTO messages (ts,sender,recipient,body,task_id) VALUES (?,?,?,?,?)",
            (time.time(), sender, recipient, body, task_id),
        )

    def messages(self, limit=100):
        rows = self._run(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,), fetch="all"
        )
        return list(reversed(rows))

    # -- transcript --------------------------------------------------------
    def add_transcript(self, agent_id, kind, body, task_id=None):
        return self._run(
            "INSERT INTO transcript (ts,agent_id,task_id,kind,body) VALUES (?,?,?,?,?)",
            (time.time(), agent_id, task_id, kind, body),
        )

    def transcript(self, agent_id, limit=60):
        rows = self._run(
            "SELECT * FROM transcript WHERE agent_id=? ORDER BY id DESC LIMIT ?",
            (agent_id, limit), fetch="all",
        )
        return list(reversed(rows))

    # -- approvals & questions --------------------------------------------
    def create_approval(self, agent_id, kind, action, detail=None, task_id=None):
        aid = new_id("ap")
        self._run(
            "INSERT INTO approvals (id,ts,agent_id,task_id,kind,action,detail,status)"
            " VALUES (?,?,?,?,?,?,?,'pending')",
            (aid, time.time(), agent_id, task_id, kind, action, detail),
        )
        return aid

    def decide_approval(self, approval_id, status, response=None):
        """Settle a pending approval. Returns None if it was already settled,
        so a double-click cannot re-fire the decision or its event."""
        with self._lock:  # RLock: nested _run calls are fine
            row = self._run("SELECT * FROM approvals WHERE id=?", (approval_id,),
                            fetch="one")
            if row is None or row["status"] != "pending":
                return None
            self._run(
                "UPDATE approvals SET status=?, response=?, decided_at=? WHERE id=?",
                (status, response, time.time(), approval_id),
            )
            return self._run("SELECT * FROM approvals WHERE id=?", (approval_id,),
                             fetch="one")

    def pending_approvals(self):
        return self._run(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY ts", fetch="all"
        )

    def approval(self, approval_id):
        return self._run("SELECT * FROM approvals WHERE id=?", (approval_id,), fetch="one")

    # -- memory ------------------------------------------------------------
    def remember(self, key, value, author=None):
        self._run(
            "INSERT INTO memory (key,value,author,updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " author=excluded.author, updated_at=excluded.updated_at",
            (key, value, author, time.time()),
        )

    def recall(self, key):
        row = self._run("SELECT value FROM memory WHERE key=?", (key,), fetch="one")
        return row["value"] if row else None

    def memory_keys(self):
        return self._run("SELECT key, value, updated_at FROM memory ORDER BY key", fetch="all")

    # -- reminders ---------------------------------------------------------
    def add_reminder(self, due_at, text, created_by=None):
        rid = new_id("rem")
        self._run(
            "INSERT INTO reminders (id,due_at,text,created_by) VALUES (?,?,?,?)",
            (rid, due_at, text, created_by),
        )
        return rid

    def due_reminders(self, now=None):
        return self._run(
            "SELECT * FROM reminders WHERE fired=0 AND due_at<=? ORDER BY due_at",
            (now or time.time(),), fetch="all",
        )

    def upcoming_reminders(self, limit=50):
        return self._run(
            "SELECT * FROM reminders WHERE fired=0 ORDER BY due_at LIMIT ?",
            (limit,), fetch="all",
        )

    def mark_reminder_fired(self, rid):
        self._run("UPDATE reminders SET fired=1 WHERE id=?", (rid,))

    # -- usage -------------------------------------------------------------
    def add_usage_row(self, agent_id, model, turn, task_id=None):
        """Record one agent run. `turn` is an llm.Turn.

        Cost is stored as reported when the backend knows it (the Agent SDK
        does), and estimated from list prices otherwise.
        """
        cost = turn.cost_usd or self._estimate(model, turn)
        self._run(
            "INSERT INTO usage (ts,agent_id,model,input_tokens,output_tokens,"
            "cache_read,cache_write,cost_usd,task_id,turns) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), agent_id, model, turn.input_tokens, turn.output_tokens,
             turn.cache_read, turn.cache_write, cost, task_id,
             getattr(turn, "turns", 0) or 0),
        )

    def router_stats(self, since=0.0):
        """What the front desk did in the window: how often it answered,
        routed, or handed over, and how long it took to decide."""
        rows = self._run(
            "SELECT payload FROM events WHERE type='router.decided' AND ts >= ?",
            (since,), fetch="all") or []
        out = {"n": 0, "answered": 0, "routed": 0, "manager": 0, "avg_ms": 0}
        total_ms = 0
        for r in rows:
            try:
                p = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            out["n"] += 1
            out[{"answer": "answered", "route": "routed"}.get(p.get("action"), "manager")] += 1
            total_ms += int(p.get("ms") or 0)
        if out["n"]:
            out["avg_ms"] = round(total_ms / out["n"])
        return out

    @staticmethod
    def _estimate(model, turn):
        pin, pout = config.PRICING.get(model, (2.0, 10.0))
        return round((
            turn.input_tokens * pin
            + turn.cache_read * pin * config.CACHE_READ_DISCOUNT
            + turn.cache_write * pin * config.CACHE_WRITE_MULTIPLIER
            + turn.output_tokens * pout
        ) / 1_000_000, 6)

    def spend(self, since=0.0):
        row = self._run(
            "SELECT COALESCE(SUM(cost_usd),0) usd,"
            " COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_write),0) tok"
            " FROM usage WHERE ts >= ?",
            (since,), fetch="one",
        ) or {}
        return {"usd": round(row.get("usd") or 0.0, 4), "tokens": row.get("tok") or 0}

    def spend_since(self, since):
        return self.spend(since)

    # -- usage breakdown ---------------------------------------------------
    # The single spend number tells you the office cost something; it never
    # tells you who spent it. These group the same rows by the two axes that
    # answer that: which employee, and which model.
    _USAGE_COLS = (
        "COUNT(*) turns,"
        " COALESCE(SUM(input_tokens),0) input,"
        " COALESCE(SUM(output_tokens),0) output,"
        " COALESCE(SUM(cache_read),0) cache_read,"
        " COALESCE(SUM(cache_write),0) cache_write,"
        " COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_write),0) tokens,"
        " COALESCE(SUM(cost_usd),0) cost"
    )

    @staticmethod
    def _round_usage(row):
        row = dict(row or {})
        row["cost"] = round(row.get("cost") or 0.0, 6)
        return row

    def usage_totals(self, since=0.0):
        return self._round_usage(self._run(
            f"SELECT {self._USAGE_COLS} FROM usage WHERE ts >= ?",
            (since,), fetch="one",
        ))

    def usage_by_agent(self, since=0.0):
        rows = self._run(
            f"SELECT agent_id, {self._USAGE_COLS} FROM usage WHERE ts >= ?"
            " GROUP BY agent_id ORDER BY tokens DESC",
            (since,), fetch="all",
        ) or []
        return [self._round_usage(r) for r in rows]

    def usage_by_model(self, since=0.0):
        rows = self._run(
            f"SELECT model, {self._USAGE_COLS} FROM usage WHERE ts >= ?"
            " GROUP BY model ORDER BY tokens DESC",
            (since,), fetch="all",
        ) or []
        return [self._round_usage(r) for r in rows]

    def usage_recent(self, since=0.0, limit=40):
        return self._run(
            "SELECT ts, agent_id, model, input_tokens, output_tokens,"
            " cache_read, cache_write, cost_usd FROM usage"
            " WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            (since, limit), fetch="all",
        ) or []

    def tasks_done_by(self, since=0.0):
        """Finished tasks per assignee, so usage can be read per unit of work
        rather than per model call."""
        rows = self._run(
            "SELECT assignee, COUNT(*) n FROM tasks"
            " WHERE status IN ('done', 'partial')"
            " AND COALESCE(finished_at, created_at) >= ?"
            " GROUP BY assignee", (since,), fetch="all") or []
        return {r["assignee"]: r["n"] for r in rows}

    def usage_first_ts(self, since=0.0):
        """When the earliest turn in this window happened - what a rolling
        window is actually measuring from."""
        row = self._run("SELECT MIN(ts) t FROM usage WHERE ts >= ?",
                        (since,), fetch="one") or {}
        return row.get("t")
