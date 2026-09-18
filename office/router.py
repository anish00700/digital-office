"""The front desk: a cheap classifier that reads each message before Miles.

Measured live, one manager turn costs ~7k prompt tokens (persona + ten tool
definitions + SDK overhead) before a single word of work. Most messages do
not need that: "thanks", "what time is it", "Ada, check the staging cert"
each have one obvious handler. The front desk is a Haiku call with no tools
and a fixed JSON answer that decides which of three things happens:

  answer   - reply in one line as Miles, no task, no manager turn
  route    - hand the whole message to one specialist as a task; their result
             is posted to chat when they finish
  manager  - queue it for Miles exactly as before

Anything with two asks, a follow-up reference, an approval, or a judgement
call goes to Miles, as does anything below the confidence floor, as does
everything when OFFICE_ROUTER=0. It is a saving on the common case, never a
gate on the hard one.
"""

import logging
import time

from . import config

log = logging.getLogger("office.router")

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["answer", "route", "manager"]},
        "target": {"type": "string"},
        "confidence": {"type": "number"},
        "reply": {"type": "string"},
    },
    "required": ["action", "confidence"],
}

SYSTEM = """You are the front desk of a small office of specialists. One message arrives from the office's principal. Decide, in JSON, which of three things should happen. Do not do the work yourself.

"answer": the message is a greeting, thanks, or a question fully answerable from the facts given below (who works here, what they do, the time). Put the one-line reply in "reply", written as Miles the chief of staff would, plainly.
"route": exactly one specialist listed below can do the whole message on their own, with no decision needed from the manager first. Put their id in "target". Only choose someone whose tools fit the deliverable (files need a writer; commands need a runner).
"manager": everything else. Always "manager" when the message has two or more asks, refers back to earlier work ("the same for prod", "that report", "it"), asks for something risky or irreversible, needs a plan, names nobody and could fit two people, or when you are unsure.

"confidence" is 0 to 1 for the action you chose. Below the office's floor it goes to the manager anyway, so be honest rather than bold."""


def _staff_lines():
    lines = []
    for r in config.ROSTER:
        if r.id == config.MANAGER_ID:
            continue
        can = []
        if "Bash" in r.native_tools:
            can.append("runs commands")
        if "Write" in r.native_tools or "Edit" in r.native_tools:
            can.append("writes files")
        if "WebSearch" in r.native_tools or "WebFetch" in r.native_tools:
            can.append("searches the web")
        if "fetch_slack" in r.office_tools or "fetch_mail" in r.office_tools:
            can.append("reads slack/mail")
        lines.append(f"- {r.id}: {r.name}, {r.title}"
                     + (f" ({', '.join(can)})" if can else " (answers in text only)"))
    return "\n".join(lines)


def prompt_for(text, now_text):
    return (f"Staff:\n{_staff_lines()}\n\nTime now: {now_text}\n\n"
            f"Message:\n{text.strip()}")


async def classify(office, text, now_text=""):
    """Returns (decision dict, llm.Turn, elapsed ms). The decision is None when
    the backend cannot classify (no structured() support, an error, or a
    malformed answer) - the caller then does what it always did."""
    backend = office.backend
    structured = getattr(backend, "structured", None)
    if structured is None:
        return None, None, 0
    started = time.monotonic()
    data, turn = await structured(SYSTEM, prompt_for(text, now_text), SCHEMA)
    ms = int((time.monotonic() - started) * 1000)
    if turn.error:
        log.warning("front desk failed (%s); handing to the manager", turn.error[:200])
        return None, turn, ms
    decision = _clean(data)
    return decision, turn, ms


def _clean(data):
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in ("answer", "route", "manager"):
        return None
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    target = str(data.get("target") or "").strip().lower()
    reply = str(data.get("reply") or "").strip()
    if action == "route" and target not in config.STAFF_IDS:
        return {"action": "manager", "target": "", "confidence": 0.0, "reply": ""}
    if action == "answer" and not reply:
        return {"action": "manager", "target": "", "confidence": 0.0, "reply": ""}
    return {"action": action, "target": target if action == "route" else "",
            "confidence": max(0.0, min(1.0, confidence)), "reply": reply[:600]}
