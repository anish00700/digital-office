"""The social life of the office: coffee breaks, patrols, and being called in.

None of this costs a token. The lines are canned on purpose - paying a model to
generate "how's it coming?" would be an absurd way to spend your balance.

These are real events on the bus, not client-side decoration, so every viewer
sees the same thing happen at the same time and the office feed records it.
The daemon says *what* happened; the browser decides where everyone walks.
"""

import asyncio
import logging
import os
import random
import time

from . import config

log = logging.getLogger("office.social")


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# All tunable, mostly so you can turn the office into a circus for a demo:
#   OFFICE_SOCIAL_IDLE=8 OFFICE_PATROL_MIN=20 OFFICE_PATROL_MAX=40 ./officectl run
IDLE_BEFORE_BREAK_S = _f("OFFICE_SOCIAL_IDLE", 100)
BREAK_CHANCE = _f("OFFICE_BREAK_CHANCE", 0.35)
BREAK_LENGTH_S = (_f("OFFICE_BREAK_MIN", 25), _f("OFFICE_BREAK_MAX", 55))

# How often the manager gets up to see what everyone is doing.
PATROL_EVERY_S = (_f("OFFICE_PATROL_MIN", 150), _f("OFFICE_PATROL_MAX", 320))
TICK_S = _f("OFFICE_SOCIAL_TICK", 12)

SCOLD_LINES = [
    "Explain to me, slowly, what happened here.",
    "I don't want excuses. I want it working.",
    "Did you read the brief, or did you skim it?",
    "We do not ship guesses. Do it again.",
    "This came back wrong. Own it and fix it.",
    "I've had better output from the printer.",
    "You had one job, and it had a deadline.",
    "Talk me through your thinking. Take your time.",
    "That's not the standard. You know that's not the standard.",
]

APOLOGY_LINES = [
    "...noted.",
    "That's fair.",
    "It won't happen again.",
    "I'd love to blame the network.",
    "Understood. Reassign it to me.",
    "I'll have it back to you within the hour.",
    "Yeah. That one's on me.",
]

PATROL_LINES = [
    "How's it coming?",
    "Everything under control over here?",
    "Don't let me interrupt.",
    "Good. Keep going.",
    "Anything blocking you?",
    "I want that by end of day.",
    "Nice work on the last one.",
    "Still on track?",
]

BUSY_REPLIES = [
    "Nearly there.",
    "Two minutes.",
    "It's compiling.",
    "Don't ask.",
    "All good.",
    "Ask me after this run.",
]

BREAK_LINES = [
    "Coffee.",
    "Back in five.",
    "Anyone else need one?",
    "This machine is broken again.",
    "I need to stare at a wall for a minute.",
    "Refill.",
]

RETURN_LINES = [
    "Right. Where were we.",
    "Back.",
    "Much better.",
    "Okay, I'm human again.",
]


class SocialLife:
    """Ambient behaviour, driven off a slow ticker. Purely additive: it never
    blocks, delays, or interferes with actual work."""

    def __init__(self, office):
        self.office = office
        self.bus = office.bus
        self.idle_since = {}
        self.on_break = {}      # agent_id -> when the break ends
        self.in_office = set()  # currently being told off; leave them be
        self.next_patrol = time.time() + random.uniform(*PATROL_EVERY_S)

    # -- called by the orchestrator ------------------------------------
    def note_status(self, agent_id, status):
        """Track how long someone has been doing nothing."""
        if status == "idle":
            self.idle_since.setdefault(agent_id, time.time())
        else:
            self.idle_since.pop(agent_id, None)
            if agent_id in self.on_break:
                # Work arrived - break's over, back to your desk.
                self._end_break(agent_id)

    def _end_break(self, agent_id):
        self.on_break.pop(agent_id, None)
        self.idle_since[agent_id] = time.time()
        self.bus.publish("social.return", agent_id=agent_id,
                         line=random.choice(RETURN_LINES))

    async def on_task_failed(self, agent_id, task_id, error):
        """Someone got it wrong. Miles would like a word."""
        if agent_id == config.MANAGER_ID or agent_id in self.in_office:
            return
        self.on_break.pop(agent_id, None)
        self.in_office.add(agent_id)
        try:
            reason = (error or "").strip().splitlines()[0][:90] if error \
                else "it came back wrong"
            self.bus.publish("social.summoned", agent_id=agent_id, task_id=task_id,
                             reason=reason)
            await asyncio.sleep(4.5)   # time to walk to the manager's office
            self.bus.publish("social.scold", agent_id=agent_id, task_id=task_id,
                             line=random.choice(SCOLD_LINES),
                             reply=random.choice(APOLOGY_LINES))
            await asyncio.sleep(6.0)
            self.bus.publish("social.dismissed", agent_id=agent_id)
        finally:
            self.in_office.discard(agent_id)
            self.idle_since[agent_id] = time.time()

    # -- ticker ---------------------------------------------------------
    async def run(self):
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self._end_finished_breaks()
                self._maybe_break()
                self._maybe_patrol()
            except Exception:
                log.exception("social tick failed")

    def _end_finished_breaks(self):
        now = time.time()
        for agent_id in [a for a, ends in self.on_break.items() if now >= ends]:
            self._end_break(agent_id)

    def _maybe_break(self):
        now = time.time()
        statuses = {a["id"]: a["status"] for a in self.office.store.agents()}
        # The whole floor emptying at once looks like a fire drill, not a
        # break. Keep at most a third of the staff away from their desks.
        room = max(1, len(config.STAFF_IDS) // 3) - len(self.on_break)
        if room <= 0:
            return
        candidates = []
        for agent_id in config.STAFF_IDS:
            if statuses.get(agent_id) != "idle" or agent_id in self.on_break:
                continue
            if agent_id in self.in_office:
                continue   # being told off is not a break
            if now - self.idle_since.setdefault(agent_id, now) < IDLE_BEFORE_BREAK_S:
                continue
            candidates.append(agent_id)

        random.shuffle(candidates)
        for agent_id in candidates[:room]:
            if random.random() > BREAK_CHANCE:
                continue
            seconds = random.uniform(*BREAK_LENGTH_S)
            self.on_break[agent_id] = now + seconds
            self.idle_since[agent_id] = now
            self.bus.publish("social.break", agent_id=agent_id,
                             line=random.choice(BREAK_LINES),
                             seconds=round(seconds))

    def _maybe_patrol(self):
        now = time.time()
        if now < self.next_patrol:
            return
        statuses = {a["id"]: a["status"] for a in self.office.store.agents()}
        if statuses.get(config.MANAGER_ID) not in ("idle", None):
            return  # he's busy; the floor can wait
        if self.in_office:
            return  # he's mid-telling-off; one drama at a time
        self.next_patrol = now + random.uniform(*PATROL_EVERY_S)

        visitable = [a for a in config.STAFF_IDS
                     if a not in self.on_break and a not in self.in_office]
        random.shuffle(visitable)
        if not visitable:
            return
        route = random.sample(visitable, k=min(3, len(visitable)))
        self.bus.publish(
            "social.patrol",
            agent_id=config.MANAGER_ID,
            route=[{"agent": a,
                    "line": random.choice(PATROL_LINES),
                    "reply": random.choice(BUSY_REPLIES)} for a in route],
        )
