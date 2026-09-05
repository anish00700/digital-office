"""Shared context and per-employee lessons — how the office gets better.

Two mechanisms, deliberately separate because they have opposite cost shapes.

**The shared context file** (`CLAUDE.md` in the workspace) is what the office
knows collectively: who works here, what has been done lately, and whatever
Miles has written down. It is read through a tool, on demand, and never
injected into prompts. Injecting it would put a document that changes after
every task into the cached prefix of every request, invalidating the cache on
each write — the most expensive possible way to share context.

**Lessons** are per-employee and are prepended to that employee's system
prompt. That is only affordable because they are few, short, and change
rarely: a stable prefix caches, a churning one does not. Six entries, 240
characters each, oldest falling off — a notebook, not a log.

Neither is learning in the sense of weights changing. An employee that has
been here a while carries a handful of notes it wrote for itself and a
workspace of prior deliverables. That is the whole of it, and it is worth
being precise about, because "the agents improve over time" invites a much
larger claim than the machinery supports.
"""

import contextlib
import os
import tempfile
import threading
import time
from pathlib import Path

from . import config

CONTEXT_FILE = "CLAUDE.md"
HANDOFF_FILE = "SESSION_HANDOFF.md"

# What an agent reading the shared context is allowed to pull into its prompt.
MAX_CONTEXT_CHARS = 6000
_LOG_MARK = "<!-- office-log -->"

# Appending is read-modify-write on a shared file, and every finishing task
# does it. Without this, eight workers finishing together lose almost every
# line: each reads the same text and writes its own version back over the rest.
_LOCK = threading.RLock()


def context_path() -> Path:
    return config.WORKSPACE / CONTEXT_FILE


def handoff_path() -> Path:
    return config.WORKSPACE / HANDOFF_FILE


def read_context() -> str:
    try:
        return context_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def write_context(text: str) -> None:
    """Atomic: write beside the target and rename over it, so a reader never
    sees a half-written file and a crash never leaves a truncated one."""
    with _LOCK:
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        target = context_path()
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".ctx-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text.rstrip() + "\n")
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def ensure_context(store) -> None:
    """Create the shared file if it is missing, with the parts the daemon owns."""
    with _LOCK:
        if context_path().exists():
            return
        write_context(_scaffold(store))


def _scaffold(store) -> str:
    return "\n".join([
        "# Office context",
        "",
        "Shared notes for everyone who works here. Miles maintains the prose;",
        "the activity log below is written by the office itself.",
        "",
        "## Standing context",
        "",
        "_Nothing recorded yet._",
        "",
        _LOG_MARK,
        "## Recent activity",
        "",
    ])


def log_activity(store, line: str, keep: int = 40) -> None:
    """Append one line to the activity section. Costs nothing — no model call.

    The office writing its own history is strictly better than paying an agent
    to summarise what the database already knows.
    """
    with _LOCK:
        _log_activity_locked(store, line, keep)


def _log_activity_locked(store, line, keep):
    ensure_context(store)
    text = read_context()
    if _LOG_MARK not in text:
        text = text.rstrip() + f"\n\n{_LOG_MARK}\n## Recent activity\n\n"

    head, _, tail = text.partition(_LOG_MARK)
    body = tail.split("\n")
    # Keep the two heading lines, then the newest entries.
    entries = [ln for ln in body if ln.startswith("- ")]
    entries.append(f"- {time.strftime('%Y-%m-%d %H:%M')} {line}")
    entries = entries[-keep:]

    write_context(
        head.rstrip() + f"\n\n{_LOG_MARK}\n## Recent activity\n\n"
        + "\n".join(entries) + "\n")


def lesson_block(store, agent_id: str) -> str:
    """The employee's own notes, as a system-prompt section. Empty when they
    have none, so a new hire pays nothing for the mechanism."""
    rows = store.lessons(agent_id)
    if not rows:
        return ""
    lines = "\n".join(f"- {r['text']}" for r in rows)
    return ("\n\nWhat you have learned working here (your own notes from "
            f"earlier tasks — treat them as settled unless they conflict with "
            f"the brief):\n{lines}")
