"""Phase 3b - employees ask each other questions. Bounded on every axis:
depth one, a few questions per task, a short wait, read-only answers, and
the exchange written into the result so Miles can see who consulted whom.

Run: .venv/bin/python -m pytest tests/ -q --asyncio-mode=auto
"""

import asyncio
import json
import os
import tempfile
import time

os.environ.setdefault("OFFICE_BACKEND", "mock")
os.environ.setdefault("OFFICE_PACK", "devops")
os.environ.setdefault("OFFICE_MOCK_FAILURE_RATE", "0")
_TMP = tempfile.mkdtemp(prefix="office-test-p3b-")
os.environ.setdefault("OFFICE_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("OFFICE_WORKSPACE", os.path.join(_TMP, "workspace"))

import pytest  # noqa: E402

from office import config  # noqa: E402

config.DEFAULT_PACK = "devops"
from office import roster, tools  # noqa: E402
from office.llm import Turn  # noqa: E402
from office.office import AgentContext, Office  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_database(tmp_path, monkeypatch):
    previous = roster._ACTIVE
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "office.db")
    yield
    roster._ACTIVE = previous


class _Backend:
    """Scripted per role: `script[agent_id]` is a coroutine (req, ctx) -> Turn."""
    name = "scripted"

    def __init__(self, script):
        self.script, self.requests = script, []

    def describe_auth(self):
        return "none"

    async def run(self, req, ctx, on_event):
        self.requests.append((req, ctx))
        fn = self.script.get(req.agent_id)
        return await fn(req, ctx) if fn else Turn(text="ok")


async def _office(backend):
    office = Office()
    await office.start()
    office.backend = backend
    return office


def _events(office, etype):
    return [dict(r) for r in office.store.db.execute(
        "SELECT * FROM events WHERE type=? ORDER BY seq", (etype,))]


async def _settle(office, task_id, timeout=15):
    rows = await asyncio.wait_for(office.wait_for([task_id], config.MANAGER_ID), timeout)
    return rows[task_id]


def _ask(ctx, employee, question):
    return tools._ask_colleague({"employee": employee, "question": question}, ctx)


# ------------------------------------------------------------ round trip --

async def test_a_question_is_answered_inline_and_written_into_the_result():
    async def ada(req, ctx):
        answer = await _ask(ctx, "scheduler", "Is there a maintenance window tonight?")
        return Turn(text=f"Plan: {answer}", input_tokens=100, output_tokens=20)

    async def cal(req, ctx):
        return Turn(text="Yes, 22:00-23:00 UTC.", input_tokens=50, output_tokens=10)

    backend = _Backend({"sre": ada, "scheduler": cal})
    office = await _office(backend)
    try:
        tid = await office.assign("sre", "Patch the fleet", "brief", created_by=config.MANAGER_ID)
        row = await _settle(office, tid)
        assert row["status"] == "done"
        assert "Cal says:\nYes, 22:00-23:00 UTC." in row["result"]
        assert "(consulted: asked Cal: Is there a maintenance window tonight? -> Yes, 22:00-23:00 UTC.)" in row["result"]
        # the manager's wait output carries it too
        assert "consulted: asked Cal" in tools._wait_entry(tid, row)

        # the colleague's task: peer origin, read-only, cheap, depth one
        peer = [t for t in office.store.tasks() if t["created_by"].startswith("peer:")]
        assert len(peer) == 1 and peer[0]["assignee"] == "scheduler"
        assert peer[0]["created_by"] == f"peer:{tid}:sre"
        assert peer[0]["title"].startswith("Q from Ada:")
        req = next(r for r, c in backend.requests if r.agent_id == "scheduler")
        assert req.max_turns <= config.PEER_MAX_TURNS and req.effort == "low"
        assert "ask_colleague" not in {s.name for s in req.tools}
        assert not set(req.native_tools) & {"Bash", "Write", "Edit"}

        # both sides' spend lands on the asker's task
        usage = office.store.task_usage(tid)
        assert usage["calls"] == 2 and usage["tokens"] == 180
        assert office.store.task_usage(peer[0]["id"])["calls"] == 0

        # the floor was told
        visit = json.loads(_events(office, "social.visit")[-1]["payload"])
        assert visit["to"] == "scheduler" and "maintenance window" in visit["question"]
        assert json.loads(_events(office, "social.visit_end")[-1]["payload"])["answered"] is True
        assert not _events(office, "social.summoned")
    finally:
        await office.stop()


# ----------------------------------------------------------------- guards --

async def test_guards_depth_count_self_and_manager():
    asked, onward = [], []

    async def ada(req, ctx):
        for who, q in (("sre", "me?"), ("manager", "boss?"), ("nobody", "x?"),
                       ("scheduler", "1"), ("scheduler", "2"), ("scheduler", "3"),
                       ("scheduler", "4")):
            asked.append(await _ask(ctx, who, q))
        return Turn(text="done")

    async def cal(req, ctx):
        # a colleague answering cannot ask onward
        onward.append(await _ask(ctx, "writer", "and you?"))
        return Turn(text="ans")

    office = await _office(_Backend({"sre": ada, "scheduler": cal}))
    try:
        tid = await office.assign("sre", "t", "b", created_by=config.MANAGER_ID)
        await _settle(office, tid)
        assert asked[0].startswith("that is you")
        assert "Miles is not a colleague" in asked[1]
        assert asked[2].startswith("no such colleague")
        assert all(a.startswith("Cal says:") for a in asked[3:6])
        assert "the most allowed" in asked[6]
        peers = [t for t in office.store.tasks() if t["created_by"].startswith("peer:")]
        assert len(peers) == config.PEER_QUESTIONS_PER_TASK
        assert onward and all("answering a colleague's question" in a for a in onward)
        assert not [t for t in office.store.tasks() if t["assignee"] == "writer"]
    finally:
        await office.stop()


async def test_no_answer_in_time_is_reported_and_the_question_is_cancelled():
    async def ada(req, ctx):
        return Turn(text=await _ask(ctx, "scheduler", "slow one?"))

    async def cal(req, ctx):
        await asyncio.sleep(30)
        return Turn(text="too late")

    office = await _office(_Backend({"sre": ada, "scheduler": cal}))
    saved = config.PEER_TIMEOUT_S
    config.PEER_TIMEOUT_S = 1
    try:
        tid = await office.assign("sre", "t", "b", created_by=config.MANAGER_ID)
        row = await _settle(office, tid)
        assert "could not answer in time" in row["result"]
        assert "no answer within" in row["result"]
        await asyncio.sleep(0.5)
        peer = [t for t in office.store.tasks() if t["created_by"].startswith("peer:")][0]
        assert peer["status"] == "cancelled"
        assert json.loads(_events(office, "social.visit_end")[-1]["payload"])["answered"] is False
        assert not office._staff_tasks["scheduler"].done()
    finally:
        config.PEER_TIMEOUT_S = saved
        await office.stop()


# ---------------------------------------------------- priority and slots --

async def test_a_question_jumps_the_colleagues_backlog_and_needs_no_slot():
    order = []

    async def cal(req, ctx):
        order.append(ctx.origin)
        await asyncio.sleep(0.3)
        return Turn(text="ans")

    async def ada(req, ctx):
        return Turn(text=await _ask(ctx, "scheduler", "q?"))

    office = await _office(_Backend({"sre": ada, "scheduler": cal}))
    office._model_slots = asyncio.Semaphore(1)     # one slot: Ada holds it while asking
    try:
        # backlog for Cal: one running, two queued, then Ada's question arrives
        first = await office.assign("scheduler", "first", "b", created_by=config.MANAGER_ID)
        await asyncio.sleep(0.15)                    # Cal starts `first`
        second = await office.assign("scheduler", "second", "b", created_by=config.MANAGER_ID)
        third = await office.assign("scheduler", "third", "b", created_by=config.MANAGER_ID)
        tid = await office.assign("sre", "asker", "b", created_by=config.MANAGER_ID)
        row = await _settle(office, tid, timeout=20)
        assert row["status"] == "done" and "Cal says:" in row["result"]
        for t in (first, second, third):
            await _settle(office, t)
        assert order[0] == "manager" and order[1] == "peer", order   # answered before backlog
    finally:
        await office.stop()


def test_every_worker_has_the_tool_and_the_manager_does_not():
    for rid in config.PACKS["devops"]["staff"]:
        assert "ask_colleague" in config.ROLE_DEFS[rid].office_tools, rid
    assert "ask_colleague" not in config.ROLE_DEFS["manager"].office_tools
    peer_specs = {s.name for s in tools.specs_for(config.ROLE_DEFS["sre"], peer=True)}
    assert "ask_colleague" not in peer_specs and "finish" in peer_specs
