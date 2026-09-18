"""Phase 2 - token architecture: the front desk, pausing instead of scolding
on a rate limit, cancelling work, waiting through a pause, remembering the
conversation, and knowing what each task cost.

Run: .venv/bin/python -m pytest tests/ -q --asyncio-mode=auto
"""

import asyncio
import os
import sys
import tempfile
import time

os.environ.setdefault("OFFICE_BACKEND", "mock")
os.environ.setdefault("OFFICE_PACK", "devops")
os.environ.setdefault("OFFICE_MOCK_FAILURE_RATE", "0")
_TMP = tempfile.mkdtemp(prefix="office-test-p2-")
os.environ.setdefault("OFFICE_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("OFFICE_WORKSPACE", os.path.join(_TMP, "workspace"))

import pytest  # noqa: E402

from office import config  # noqa: E402

config.DEFAULT_PACK = "devops"
from office import roster, router, tools  # noqa: E402
from office.llm import Turn  # noqa: E402
from office.office import Office  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_database(tmp_path, monkeypatch):
    """One database per test. Sharing one meant a task left running by the
    previous test was recovered on the next start and ate its scripted
    turns - a real Phase 0 feature making the tests lie."""
    previous = roster._ACTIVE
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "office.db")
    yield
    roster._ACTIVE = previous


class _Scripted:
    """Returns the turns it was handed, in order; sleeps if told to."""
    name = "scripted"

    def __init__(self, *turns, delay=0.0):
        self.turns, self.delay, self.calls = list(turns), delay, 0

    def describe_auth(self):
        return "none"

    async def run(self, req, ctx, on_event):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.turns.pop(0) if self.turns else Turn(text="ok")


async def _office(backend=None):
    office = Office()
    await office.start()
    if backend is not None:
        office.backend = backend
    return office


def _events(office, etype):
    return [dict(r) for r in office.store.db.execute(
        "SELECT * FROM events WHERE type=? ORDER BY seq", (etype,))]


async def _settle(office, task_id, timeout=10):
    rows = await asyncio.wait_for(office.wait_for([task_id], config.MANAGER_ID), timeout)
    return rows[task_id]


# ------------------------------------------------------------- front desk --

async def test_front_desk_answers_routes_and_hands_over():
    office = await _office()
    try:
        before = len(_events(office, "router.decided"))
        office.submit_user_message("hello there")
        office.submit_user_message("Quill, draft a two-line welcome note")
        office.submit_user_message("check staging and then write it up and email it")
        for _ in range(60):
            await asyncio.sleep(0.2)
            if len(_events(office, "router.decided")) - before >= 3:
                break
        decided = [r for r in _events(office, "router.decided")][before:]
        import json
        actions = [json.loads(r["payload"])["action"] for r in decided]
        assert actions == ["answer", "route", "manager"], actions
        # the answer arrived as a chat message from Miles, with no task
        msgs = office.store.messages(limit=20)
        assert any(m["sender"] == "manager" and "Hello" in m["body"] for m in msgs)
        # the routed one became a task for the writer, and its result was posted
        # back to chat by the writer - the direct path used to end in silence
        tasks = [t for t in office.store.tasks() if t["assignee"] == "writer"]
        assert tasks, "routed request produced no task"
        await _settle(office, tasks[0]["id"])
        for _ in range(30):
            await asyncio.sleep(0.1)
            if any(m["sender"] == "writer" for m in office.store.messages(limit=20)):
                break
        replies = [e for e in _events(office, "agent.message_user")
                   if e["agent_id"] == "writer"]
        assert replies and json.loads(replies[-1]["payload"]).get("routed") is True
        # the front desk's own spend is on the ledger, under its own name
        assert any(r["agent_id"] == "router" for r in office.store.usage_by_agent())
        # and the manager queue got the multi-ask message
        await asyncio.sleep(2.5)     # mock manager delegates; let it drain
    finally:
        await office.stop()


async def test_front_desk_respects_the_floor_and_the_kill_switch():
    office = await _office()
    try:
        async def timid(system, prompt, schema, model=""):
            return {"action": "route", "target": "writer", "confidence": 0.5}, Turn()
        office.backend.structured = timid
        import json
        before = len(_events(office, "router.decided"))
        office.submit_user_message("write me something short")
        for _ in range(30):
            await asyncio.sleep(0.1)
            if len(_events(office, "router.decided")) > before:
                break
        last = json.loads(_events(office, "router.decided")[-1]["payload"])
        assert last["action"] == "manager" and last["confidence"] == 0.5

        saved = config.ROUTER
        config.ROUTER = False
        try:
            n = len(_events(office, "router.decided"))
            office.submit_user_message("and this one goes straight to Miles")
            await asyncio.sleep(0.5)
            assert len(_events(office, "router.decided")) == n
        finally:
            config.ROUTER = saved
        await asyncio.sleep(3)       # let the mock manager drain both
    finally:
        await office.stop()


def test_front_desk_cleans_bad_answers():
    assert router._clean({"action": "route", "target": "nobody", "confidence": 0.9})["action"] == "manager"
    assert router._clean({"action": "answer", "confidence": 0.9})["action"] == "manager"
    assert router._clean({"action": "ANSWER", "reply": "hi", "confidence": "0.95"}) == {
        "action": "answer", "target": "", "confidence": 0.95, "reply": "hi"}
    assert router._clean("nonsense") is None
    assert router._clean({"action": "route", "target": "writer", "confidence": 7})["confidence"] == 1.0


# ------------------------------------------------------------- rate limit --

async def test_rate_limit_pauses_the_office_instead_of_failing_the_task():
    backend = _Scripted(Turn(stop="rate_limited", error="rate limited: 429", retry_after=60),
                        Turn(text="done after the pause"))
    office = await _office(backend)
    try:
        tid = await office.assign("writer", "t", "brief", created_by=config.MANAGER_ID)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if office._paused_reason:
                break
        assert "rate limit" in office._paused_reason
        assert office._paused_until > time.time() + 30
        assert office.store.task(tid)["status"] == "queued"
        assert not _events(office, "social.summoned")
        assert _events(office, "office.paused")

        # a wait during the pause returns now, not in 900s, and says why
        t0 = time.monotonic()
        rows = await office.wait_for([tid], config.MANAGER_ID)
        assert time.monotonic() - t0 < 2
        entry = tools._wait_entry(tid, rows[tid], office.paused_summary())
        assert "office is paused" in entry and "Do not wait again" in entry

        office.resume()
        row = await _settle(office, tid)
        assert row["status"] == "done" and row["result"] == "done after the pause"
        assert backend.calls == 2
        usage = office.store.task_usage(tid)
        assert usage["calls"] == 2
        assert _events(office, "office.resumed")
    finally:
        await office.stop()


async def test_timed_pause_lifts_itself():
    office = await _office()
    try:
        office.pause("the model reported a rate limit", time.time() - 1)
        assert office._paused_reason
        office._tick()
        assert not office._paused_reason
    finally:
        await office.stop()


# ----------------------------------------------------------------- cancel --

async def test_cancel_a_running_task_and_keep_the_worker():
    backend = _Scripted(delay=30)
    office = await _office(backend)
    try:
        tid = await office.assign("writer", "slow", "brief", created_by=config.MANAGER_ID)
        for _ in range(30):
            await asyncio.sleep(0.1)
            if office.store.task(tid)["status"] == "running":
                break
        assert office.cancel_task(tid) is not None
        row = await _settle(office, tid, timeout=5)
        assert row["status"] == "cancelled" and row["stop"] == "cancelled"
        assert not _events(office, "social.summoned")
        assert not office._staff_tasks["writer"].done(), "worker died with its task"
        assert office.cancel_task(tid) is None          # already finished

        backend.delay = 0
        tid2 = await office.assign("writer", "next", "brief", created_by=config.MANAGER_ID)
        assert (await _settle(office, tid2))["status"] == "done"
        assert "cancelled by your principal" in tools._wait_entry(tid, row)
    finally:
        await office.stop()


async def test_cancel_a_queued_task():
    office = await _office(_Scripted())
    try:
        office.pause("paused by you")
        tid = await office.assign("writer", "queued", "brief", created_by=config.MANAGER_ID)
        await asyncio.sleep(0.2)
        assert office.cancel_task(tid) is not None
        row = await _settle(office, tid, timeout=5)
        assert row["status"] == "cancelled"
        office.resume()
        await asyncio.sleep(0.3)
        assert office.store.task(tid)["status"] == "cancelled"   # not resurrected
    finally:
        await office.stop()


# ---------------------------------------------------------------- inbound --

async def test_duplicate_submit_is_dropped():
    office = await _office()
    try:
        saved = config.ROUTER
        config.ROUTER = False
        try:
            n = len(office.store.messages(limit=200))
            assert office.submit_user_message("twice") == {"to": "manager"}
            assert office.submit_user_message("twice") == {"duplicate": True}
            assert len(office.store.messages(limit=200)) == n + 1
            await asyncio.sleep(2.5)
        finally:
            config.ROUTER = saved
    finally:
        await office.stop()


async def test_manager_remembers_the_conversation():
    office = await _office()
    try:
        office.store.add_message("user", "manager", "check the staging cert")
        office.store.add_message("manager", "user", "Done - it expires in 30 days.")
        office.store.add_message("user", "manager", "now the same for prod")
        block = office._memory_block("now the same for prod")
        assert "You: check the staging cert" in block
        assert "Miles: Done - it expires in 30 days." in block
        assert "now the same for prod" not in block          # the new one is not history
        assert block.endswith("New message:\n")
        saved = config.MANAGER_MEMORY
        config.MANAGER_MEMORY = 0
        try:
            assert office._memory_block("x") == ""
        finally:
            config.MANAGER_MEMORY = saved
    finally:
        await office.stop()


# ------------------------------------------------------------ attribution --

async def test_task_rows_carry_their_cost():
    office = await _office(_Scripted(Turn(text="ok", input_tokens=500, output_tokens=50,
                                          turns=3, cost_usd=0.01)))
    try:
        tid = await office.assign("writer", "costed", "brief", created_by=config.MANAGER_ID)
        await _settle(office, tid)
        row = next(t for t in office.store.tasks() if t["id"] == tid)
        assert row["tokens"] == 550 and row["turns"] == 3 and row["calls"] == 1
        assert row["cost"] == pytest.approx(0.01) and row["model"]
        cols = {r[1] for r in office.store.db.execute("PRAGMA table_info(usage)")}
        assert {"task_id", "turns"} <= cols
    finally:
        await office.stop()


def test_manager_has_a_clock():
    assert "now" in config.ROLE_DEFS["manager"].office_tools
