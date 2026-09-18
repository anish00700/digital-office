"""Routines: recurring work you set up once. "Sweep the inbox at 08:00."

A routine is a brief, an assignee, and a schedule. The ticker enqueues it as
a direct task to the specialist (no manager turn: the brief is already
written), and the result is posted to chat when it finishes. Token cost is
therefore predictable and yours to set - nothing here calls a model on its
own initiative, and an office with no routines still costs nothing idle.

Schedules, all in the office timezone (OFFICE_TZ, else the machine's):
    daily HH:MM        every day at that time
    weekdays HH:MM     Monday to Friday at that time
    every Nm | Nh      every N minutes / hours, from when it was created
"""

import datetime as dt
import re

from . import config

_DAILY = re.compile(r"^(daily|weekdays)\s+(\d{1,2}):(\d{2})$", re.I)
_EVERY = re.compile(r"^every\s+(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours)$", re.I)

HELP = "daily HH:MM · weekdays HH:MM · every 30m · every 2h"


def parse(schedule):
    """Normalise a schedule string. Raises ValueError with a readable reason."""
    text = " ".join((schedule or "").split()).lower()
    m = _DAILY.match(text)
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"{schedule!r}: the time must be 00:00-23:59")
        return f"{m.group(1)} {hour:02d}:{minute:02d}"
    m = _EVERY.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        seconds = n * (60 if unit == "m" else 3600)
        if seconds < 300:
            raise ValueError(f"{schedule!r}: the shortest interval is 5 minutes")
        if seconds > 7 * 86400:
            raise ValueError(f"{schedule!r}: use daily/weekdays for anything past a week")
        return f"every {n}{unit}"
    raise ValueError(f"{schedule!r} is not a schedule I know. Use: {HELP}")


def _now(ts=None):
    tz = config.TZ
    if ts is None:
        return dt.datetime.now(tz) if tz else dt.datetime.now().astimezone()
    return dt.datetime.fromtimestamp(ts, tz) if tz else dt.datetime.fromtimestamp(ts).astimezone()


def next_run(schedule, after=None):
    """The first firing strictly after `after` (a timestamp; default now)."""
    text = parse(schedule)
    base = _now(after)
    if text.startswith("every"):
        m = _EVERY.match(text)
        n, unit = int(m.group(1)), m.group(2)[0]
        return base.timestamp() + n * (60 if unit == "m" else 3600)
    kind, hhmm = text.split()
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base:
        candidate += dt.timedelta(days=1)
    if kind == "weekdays":
        while candidate.weekday() >= 5:
            candidate += dt.timedelta(days=1)
    return candidate.timestamp()


def describe(schedule):
    text = parse(schedule)
    if text.startswith("every"):
        m = _EVERY.match(text)
        n, unit = int(m.group(1)), m.group(2)[0]
        return f"every {n} {'minute' if unit == 'm' else 'hour'}{'' if n == 1 else 's'}"
    kind, hhmm = text.split()
    return f"{'every day' if kind == 'daily' else 'weekdays'} at {hhmm}"
