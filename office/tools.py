"""Office tools, defined once in a neutral form and adapted per backend.

Descriptions are deliberately terse. Tool definitions are re-sent on every
single request, so verbose descriptions are a recurring tax on every task the
office ever runs. Say what the tool does in one line and stop.
"""

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import config
from .connectors import mail as mail_connector
from .connectors import slack as slack_connector


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, Any], Any]
    read_only: bool = True


# ---------------------------------------------------------------------------
# Worker tools
# ---------------------------------------------------------------------------

async def _note(args, ctx):
    text = (args.get("text") or "").strip()
    if text:
        ctx.emit("say", text=text)
    return "noted"


async def _ask_human(args, ctx):
    question = (args.get("question") or "").strip()
    if not question:
        return "no question given"
    answer = await ctx.ask_human(question)
    return answer or "no answer within the timeout; proceed on your best judgement"


async def _finish(args, ctx):
    summary = (args.get("summary") or "").strip()
    ctx.result = summary
    return "result recorded; stop now"


async def _now(args, ctx):
    now = dt.datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M %Z (%A)")


async def _add_reminder(args, ctx):
    when = (args.get("when") or "").strip()
    text = (args.get("text") or "").strip()
    due = _parse_when(when)
    if due is None:
        return f"could not parse {when!r}; use ISO 8601 or '+90m' / '+2h' / '+3d'"
    ctx.store.add_reminder(due, text, ctx.agent_id)
    ctx.emit("reminder", text=text)
    return f"reminder set for {dt.datetime.fromtimestamp(due):%Y-%m-%d %H:%M}"


async def _list_reminders(args, ctx):
    rows = ctx.store.upcoming_reminders()
    if not rows:
        return "no reminders set"
    return "\n".join(
        f"{dt.datetime.fromtimestamp(r['due_at']):%Y-%m-%d %H:%M} - {r['text']}"
        for r in rows
    )


def _parse_when(when):
    when = when.strip()
    if when.startswith("+"):
        unit = when[-1].lower()
        try:
            n = float(when[1:-1])
        except ValueError:
            return None
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit)
        return time.time() + n * mult if mult else None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(when, fmt).timestamp()
        except ValueError:
            continue
    return None


async def _fetch_slack(args, ctx):
    return await slack_connector.fetch(int(args.get("since_hours") or 12))


async def _fetch_mail(args, ctx):
    return await mail_connector.fetch(int(args.get("since_hours") or 12))


# ---------------------------------------------------------------------------
# Manager tools
# ---------------------------------------------------------------------------

async def _list_staff(args, ctx):
    lines = []
    statuses = {a["id"]: a["status"] for a in ctx.store.agents()}
    for r in config.ROSTER:
        if r.id == config.MANAGER_ID:
            continue
        lines.append(f"{r.id} ({r.title}) - {statuses.get(r.id, 'idle')}")
    return "\n".join(lines)


async def _assign(args, ctx):
    assignee = (args.get("assignee") or "").strip()
    if assignee not in config.STAFF_IDS:
        return f"no such employee {assignee!r}. Valid: {', '.join(config.STAFF_IDS)}"
    title = (args.get("title") or "Untitled").strip()[:120]
    brief = (args.get("brief") or "").strip()
    if not brief:
        return "brief is required - say what done looks like"
    task_id = await ctx.office.assign(assignee, title, brief, created_by=ctx.agent_id)
    return f"assigned {task_id} to {assignee}"


async def _wait(args, ctx):
    raw = args.get("task_ids") or ""
    ids = [t.strip() for t in str(raw).replace(",", " ").split() if t.strip()]
    results = await ctx.office.wait_for(ids, requester=ctx.agent_id)
    if not results:
        return "nothing to wait for"
    out = []
    for task_id, row in results.items():
        status = row.get("status")
        body = row.get("result") or row.get("error") or ""
        out.append(f"--- {task_id} [{status}] {row.get('title','')}\n{body}")
    return "\n\n".join(out)


async def _task_status(args, ctx):
    rows = ctx.store.tasks(limit=20)
    if not rows:
        return "no tasks yet"
    return "\n".join(
        f"{r['id']} [{r['status']}] {r['assignee']}: {r['title']}" for r in rows
    )


async def _message_user(args, ctx):
    text = (args.get("text") or "").strip()
    if not text:
        return "empty message not sent"
    ctx.store.add_message(ctx.agent_id, "user", text)
    ctx.emit("message_user", text=text)
    ctx.result = text
    return "delivered"


async def _remember(args, ctx):
    key = (args.get("key") or "").strip()
    value = (args.get("value") or "").strip()
    if not key:
        return "key required"
    ctx.store.remember(key, value, ctx.agent_id)
    return f"remembered {key}"


async def _recall(args, ctx):
    key = (args.get("key") or "").strip()
    if not key:
        rows = ctx.store.memory_keys()
        return ", ".join(r["key"] for r in rows) or "nothing remembered yet"
    return ctx.store.recall(key) or f"nothing stored under {key!r}"


# ---------------------------------------------------------------------------

_SPECS = {
    "note": ToolSpec(
        "note", "Say something out loud in the office. Use sparingly.",
        {"text": str}, _note),
    "ask_human": ToolSpec(
        "ask_human", "Ask your principal a question and wait for their answer.",
        {"question": str}, _ask_human),
    "finish": ToolSpec(
        "finish", "Record your final result and stop.",
        {"summary": str}, _finish),
    "now": ToolSpec(
        "now", "Current local date, time and weekday.", {}, _now),
    "add_reminder": ToolSpec(
        "add_reminder", "Set a reminder. when: ISO date, or +30m / +2h / +3d.",
        {"when": str, "text": str}, _add_reminder, read_only=False),
    "list_reminders": ToolSpec(
        "list_reminders", "List pending reminders.", {}, _list_reminders),
    "fetch_slack": ToolSpec(
        "fetch_slack", "Fetch recent unread Slack messages.",
        {"since_hours": int}, _fetch_slack),
    "fetch_mail": ToolSpec(
        "fetch_mail", "Fetch recent unread email.",
        {"since_hours": int}, _fetch_mail),
    "list_staff": ToolSpec(
        "list_staff", "List employees, their specialities and current status.",
        {}, _list_staff),
    "assign": ToolSpec(
        "assign", "Give an employee a task. Returns a task id.",
        {"assignee": str, "title": str, "brief": str}, _assign, read_only=False),
    "wait": ToolSpec(
        "wait", "Block until the given task ids finish, then return their results. "
                "Space-separated; empty means all outstanding.",
        {"task_ids": str}, _wait),
    "task_status": ToolSpec(
        "task_status", "Recent tasks and their statuses.", {}, _task_status),
    "message_user": ToolSpec(
        "message_user", "Send your principal a message. This is your final answer.",
        {"text": str}, _message_user, read_only=False),
    "remember": ToolSpec(
        "remember", "Store a durable fact for the whole office.",
        {"key": str, "value": str}, _remember, read_only=False),
    "recall": ToolSpec(
        "recall", "Read a stored fact. Empty key lists all keys.",
        {"key": str}, _recall),
}


def specs_for(role):
    return [_SPECS[n] for n in role.office_tools if n in _SPECS]


# ---------------------------------------------------------------------------
# Permission gate for Claude Code's native tools
# ---------------------------------------------------------------------------

def _is_auto_allowed_shell(command):
    cmd = " ".join((command or "").split())
    if not cmd:
        return False
    # Chained commands defeat prefix matching, so refuse to auto-approve them.
    if any(sep in cmd for sep in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return False
    return any(cmd == p or cmd.startswith(p + " ") for p in config.SHELL_AUTO_ALLOW)


def _within_workspace(path):
    if not path:
        return False
    try:
        resolved = (config.WORKSPACE / path).resolve() if not str(path).startswith("/") \
            else __import__("pathlib").Path(path).resolve()
        resolved.relative_to(config.WORKSPACE.resolve())
        return True
    except (ValueError, OSError):
        return False


async def permission_gate(ctx, tool_name, input_data, _context):
    """Routed here by the Agent SDK whenever a call is not pre-approved.

    Read-only shell commands pass. Writes inside the workspace pass. Everything
    else becomes an approval card in the GUI and waits for a human.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    if tool_name == "Bash":
        command = input_data.get("command", "")
        if _is_auto_allowed_shell(command):
            ctx.emit("tool", tool="bash", args=command[:160])
            return PermissionResultAllow(updated_input=input_data)
        ok = await ctx.request_approval("shell", command, detail=ctx.task_title)
        if ok:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Your principal declined this command.")

    if tool_name in ("Write", "Edit", "NotebookEdit"):
        path = input_data.get("file_path") or input_data.get("path") or ""
        if _within_workspace(path):
            ctx.emit("tool", tool=tool_name.lower(), args=str(path)[-80:])
            return PermissionResultAllow(updated_input=input_data)
        ok = await ctx.request_approval("write", f"{tool_name} {path}",
                                        detail=ctx.task_title)
        if ok:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Writes outside the workspace are declined.")

    ok = await ctx.request_approval("tool", f"{tool_name} {str(input_data)[:200]}",
                                    detail=ctx.task_title)
    if ok:
        return PermissionResultAllow(updated_input=input_data)
    return PermissionResultDeny(message="Declined by your principal.")
