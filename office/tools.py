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
    # config.TZ is None when OFFICE_TZ is unset, which means "the machine's
    # zone" - on a VPS that is usually UTC, and usually not what you meant.
    now = dt.datetime.now(config.TZ) if config.TZ else dt.datetime.now().astimezone()
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
            parsed = dt.datetime.strptime(when, fmt)
            if config.TZ:
                parsed = parsed.replace(tzinfo=config.TZ)
            return parsed.timestamp()
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
    """Titles alone are not enough to delegate well: a job whose deliverable is
    a file has to go to somebody who can write one, and the title never says
    who that is. Capabilities are listed so the choice is informed."""
    lines = []
    statuses = {a["id"]: a["status"] for a in ctx.store.agents()}
    for r in config.ROSTER:
        if r.id == config.MANAGER_ID:
            continue
        can = []
        if "Write" in r.native_tools or "Edit" in r.native_tools:
            can.append("saves files")
        if "Bash" in r.native_tools:
            can.append("runs commands")
        if "WebSearch" in r.native_tools or "WebFetch" in r.native_tools:
            can.append("searches the web")
        if "Read" in r.native_tools or "Grep" in r.native_tools:
            can.append("reads files")
        lines.append(f"{r.id} ({r.title}) - {statuses.get(r.id, 'idle')}"
                     + (f" - {', '.join(can)}" if can else " - answers in text only"))
    return "\n".join(lines)


async def _assign(args, ctx):
    assignee = (args.get("assignee") or "").strip()
    if assignee not in config.STAFF_IDS:
        return f"no such employee {assignee!r}. Valid: {', '.join(config.STAFF_IDS)}"
    title = (args.get("title") or "Untitled").strip()[:120]
    brief = (args.get("brief") or "").strip()
    if not brief:
        return "brief is required - say what done looks like"
    try:
        task_id = await ctx.office.assign(assignee, title, brief,
                                          created_by=ctx.agent_id)
    except KeyError:
        return f"{assignee} no longer works here. Valid: {', '.join(config.STAFF_IDS)}"
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
# Shared context and self-improvement
# ---------------------------------------------------------------------------

async def _read_context(args, ctx):
    from . import notebook
    text = notebook.read_context()
    if not text.strip():
        return "the shared context file is empty"
    return text[:notebook.MAX_CONTEXT_CHARS]


async def _write_context(args, ctx):
    """Rewrites the standing-context prose. The activity log the office keeps
    below it is preserved - an agent must not be able to erase the record."""
    from . import notebook
    body = (args.get("text") or "").strip()
    if len(body) < 20:
        return "give the whole standing-context section, not a fragment"
    current = notebook.read_context()
    _, mark, tail = current.partition(notebook._LOG_MARK)
    keep = (notebook._LOG_MARK + tail) if mark else ""
    notebook.write_context(f"# Office context\n\n{body}\n\n{keep}")
    ctx.emit("say", text="Updated the office context.")
    return "written"


async def _learn(args, ctx):
    """One durable note, for this employee only."""
    text = (args.get("lesson") or "").strip()
    if not text:
        return "nothing to record"
    ok = ctx.store.add_lesson(ctx.agent_id, text)
    if not ok:
        return "not recorded - too short, or you already know that"
    kept = len(ctx.store.lessons(ctx.agent_id))
    return (f"recorded. You now carry {kept} note(s); the oldest drops off "
            f"past {ctx.store.LESSON_LIMIT}, so keep them worth the space.")


# ---------------------------------------------------------------------------
# People Ops
# ---------------------------------------------------------------------------

def _split(raw):
    """Tool lists arrive as one comma- or space-separated string: the tool
    schema is flat, and a string round-trips through every backend cleanly."""
    if isinstance(raw, (list, tuple)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [t.strip() for t in str(raw or "").replace(",", " ").split() if t.strip()]


async def _list_skills(args, ctx):
    from . import roster as roster_mod
    cat = roster_mod.catalogue()
    helps = describe_tools()
    lines = ["OFFICE TOOLS (grant by name):"]
    for name in sorted(cat["office_tools"]):
        lines.append(f"  {name} - {helps.get(name, '')}")
    lines.append("")
    lines.append("CLAUDE CODE TOOLS:")
    lines.append("  Read, Grep, Glob, WebSearch, WebFetch - run immediately.")
    lines.append("  Bash, Write, Edit - can change this machine; every use stops "
                 "for human approval.")
    lines.append("")
    default = cat["default_model"]
    lines.append("MODELS: " + ", ".join(m or f'"" (default, currently {default})'
                                        for m in cat["models"]))
    lines.append("EFFORT: " + ", ".join(cat["efforts"]))
    lines.append(f"FREE DESKS: {len(cat['free_desks'])}")
    return "\n".join(lines)


async def _hire_employee(args, ctx):
    """Design review happens in the model; the hire itself is a human decision.

    A new employee can be granted Bash and Write, so an agent creating agents
    is exactly the kind of thing that should stop and ask."""
    from . import roster as roster_mod

    fields = {
        "name": (args.get("name") or "").strip(),
        "title": (args.get("title") or "").strip(),
        "emoji": (args.get("emoji") or "").strip(),
        "persona": (args.get("persona") or "").strip(),
        "model": (args.get("model") or "").strip(),
        "effort": (args.get("effort") or "low").strip(),
        "max_turns": args.get("max_turns") or 8,
        "office_tools": _split(args.get("office_tools")) or ["note", "ask_human", "finish"],
        "native_tools": _split(args.get("native_tools")),
    }
    if not fields["name"]:
        return "name is required"
    if len(fields["persona"]) < 20:
        return ("persona is required, and it must be the whole system prompt for "
                "this employee - not a description of one")

    granted = fields["office_tools"] + fields["native_tools"]
    risky = [t for t in fields["native_tools"] if t in ("Bash", "Write", "Edit")]
    summary = (
        f"{fields['emoji']} {fields['name']} - {fields['title'] or 'Staff'}\n"
        f"model: {fields['model'] or 'default'}   effort: {fields['effort']}   "
        f"max turns: {fields['max_turns']}\n"
        f"tools: {', '.join(granted) or 'none'}\n"
        + (f"CAN CHANGE THIS MACHINE: {', '.join(risky)}\n" if risky else "")
        + f"\n{fields['persona']}"
    )
    ok = await ctx.request_approval("hire", summary, detail="Wren wants to hire")
    if not ok:
        return "your principal declined this hire. Ask what they would change."

    try:
        role = ctx.office.hire_employee(fields)
    except roster_mod.RosterError as exc:
        return f"could not hire: {exc}"
    ctx.emit("say", text=f"Hired {role.name} as {role.title}.")
    return (f"hired {role.id} ({role.name}, {role.title}) at desk "
            f"{list(role.desk)} on {role.model_id}. They start on the next task "
            f"assigned to them.")


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
    "read_context": ToolSpec(
        "read_context", "Read the office's shared context file: standing notes "
                        "and recent activity.", {}, _read_context),
    "write_context": ToolSpec(
        "write_context", "Replace the standing-context prose in the shared file. "
                         "The activity log is preserved.",
        {"text": str}, _write_context, read_only=False),
    "learn": ToolSpec(
        "learn", "Record one short, reusable lesson for yourself. Carried into "
                 "every later task you run.",
        {"lesson": str}, _learn, read_only=False),
    "list_skills": ToolSpec(
        "list_skills", "Every tool, model and effort level an employee can be "
                       "given, and how many desks are free.",
        {}, _list_skills),
    "hire_employee": ToolSpec(
        "hire_employee",
        "Hire a new employee. persona is their entire system prompt. "
        "office_tools/native_tools are space-separated names from list_skills. "
        "Your principal approves the hire.",
        {"name": str, "title": str, "emoji": str, "persona": str, "model": str,
         "effort": str, "max_turns": int, "office_tools": str,
         "native_tools": str},
        _hire_employee, read_only=False),
}


def specs_for(role):
    return [_SPECS[n] for n in role.office_tools if n in _SPECS]


def tool_names():
    """Every office tool an employee can be given. Used to validate edits from
    the staff panel and to populate its checkboxes."""
    return set(_SPECS)


def describe_tools():
    return {name: spec.description for name, spec in _SPECS.items()}


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
    # A read-only command pointed at a secret is not read-only in any sense
    # that matters once its output is in a model's context.
    if config.names_secret_path(cmd):
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
        detail = ctx.task_title
        if config.names_secret_path(command):
            detail = f"⚠ names a secret path · {ctx.task_title}"
        ok = await ctx.request_approval("shell", command, detail=detail)
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
