"""Tests for the machinery added on top of the original daemon.

Weighted toward what has actually gone wrong rather than what is easy to
assert: the roster is mutable state with validation, the shared context file
is written concurrently by every worker, the workspace is served over HTTP,
and an office is importable from a file. Each of those has produced a real
bug, and each has a test here that would have caught it.

Run: .venv/bin/python -m pytest tests/ -q --asyncio-mode=auto
"""

import json
import os
import tempfile
import threading

import pytest

os.environ.setdefault("OFFICE_BACKEND", "mock")
_TMP = tempfile.mkdtemp(prefix="office-test-office-")
os.environ.setdefault("OFFICE_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("OFFICE_WORKSPACE", os.path.join(_TMP, "workspace"))

from office import config, notebook, roster, skills  # noqa: E402
from office import llm, tools  # noqa: E402
from office.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A private database per test.

    The live roster is a module global, so it has to be saved and put back:
    without this these tests leak their staff into every test that runs after
    them, and the delegation test in test_safety.py fails looking for an
    employee this file quietly fired.
    """
    previous = roster._ACTIVE
    # OFFICE_PACK is resolved into config.DEFAULT_PACK once, at import, so
    # clearing the environment variable here would do nothing. Patch the
    # resolved value instead, or these tests depend on which test module
    # happened to import office.config first.
    monkeypatch.setattr(config, "DEFAULT_PACK", "")
    db = Store(path=tmp_path / "t.db")
    roster.refresh(db)
    try:
        yield db
    finally:
        db.db.close()
        roster._ACTIVE = previous


@pytest.fixture
def seeded(store):
    roster.load(store)
    roster.install_pack(store, "devops", "a test principal")
    return store


# ---------------------------------------------------------------- roster --

def test_legacy_office_gets_its_staff_back(store):
    """A database from before staff packs has the original eight in `agents`
    and nothing in `roster`. It used to land on the first-run screen with
    everyone gone; it comes back as the infrastructure team instead."""
    store.ensure_agents(["manager", "sre", "pipeline", "comms", "researcher",
                         "scheduler", "writer", "analyst"])
    roster.load(store)
    assert not roster.setup_needed(store)
    assert store.setting("pack") == "devops"
    ids = {r["id"] for r in store.roster_rows()}
    assert {"manager", "hr", "sre", "pipeline", "comms", "researcher",
            "scheduler", "writer", "analyst"} <= ids


def test_fresh_office_still_asks(store):
    """No legacy rows -> first-run screen, exactly as before."""
    roster.load(store)
    assert roster.setup_needed(store)
    assert {r["id"] for r in store.roster_rows()} == {"manager", "hr"}


def test_seeding_installs_core_then_pack(store):
    roster.load(store)
    # Before a pack is chosen, only the machinery exists.
    assert [r.id for r in roster.snapshot()] == list(config.CORE_IDS)
    assert roster.setup_needed(store)

    roster.install_pack(store, "research", "a researcher")
    ids = [r.id for r in roster.snapshot()]
    assert "critic" in ids and "sre" not in ids
    assert not roster.setup_needed(store)
    assert roster.principal(store) == "a researcher"


def test_desks_never_collide(seeded):
    """Packs are arbitrary sets of people, so seats are assigned, not declared."""
    desks = [tuple(r.desk) for r in roster.snapshot()]
    assert len(desks) == len(set(desks)), "two people share a desk"
    for r in roster.snapshot():
        if r.id != config.MANAGER_ID:
            assert tuple(r.desk) in config.DESK_SLOTS


def test_fired_staff_stay_fired_across_a_reseed(seeded):
    roster.fire(seeded, "writer")
    assert not roster.exists("writer")
    roster.load(seeded)                       # an upgrade re-runs seeding
    assert not roster.exists("writer"), "firing must survive a restart"


def test_manager_cannot_be_fired(seeded):
    with pytest.raises(roster.RosterError):
        roster.fire(seeded, config.MANAGER_ID)
    assert roster.exists(config.MANAGER_ID)


@pytest.mark.parametrize("fields", [
    {"model": "gpt-4"},
    {"effort": "extreme"},
    {"office_tools": ["rm_rf"]},
    {"native_tools": ["Nuke"]},
    {"max_turns": 999},
    {"color": "javascript:alert(1)"},
    {"persona": "too short"},
    {"skills": ["not a skill name"]},
])
def test_bad_edits_are_refused_and_change_nothing(seeded, fields):
    before = roster.get("sre")
    with pytest.raises(roster.RosterError):
        roster.update(seeded, "sre", fields)
    assert roster.get("sre") == before


def test_manager_keeps_the_tools_it_needs_to_delegate(seeded):
    with pytest.raises(roster.RosterError):
        roster.update(seeded, config.MANAGER_ID, {"office_tools": ["note"]})


def test_hiring_seats_and_persists(seeded):
    role = roster.hire(seeded, {
        "name": "Kit", "title": "Database",
        "persona": "You are Kit and you review schemas before forming opinions.",
        "office_tools": ["note", "finish"], "native_tools": ["Read"],
    })
    assert role.id == "kit"
    assert tuple(role.desk) in config.DESK_SLOTS
    roster.refresh(seeded)                    # prove it came from the database
    assert roster.get("kit").native_tools == ("Read",)


# ------------------------------------------------------- export / import --

def test_export_import_round_trip(seeded):
    roster.update(seeded, "sre", {"model": "opus", "max_turns": 12})
    doc = roster.export_office(seeded)
    before = {r.id: r for r in roster.snapshot()}

    roster.install_pack(seeded, "empty", "")   # tear the office down
    roster.import_office(seeded, doc)

    after = {r.id: r for r in roster.snapshot()}
    assert set(after) == set(before)
    assert after["sre"].model == "opus"
    assert after["sre"].max_turns == 12
    assert after["sre"].persona == before["sre"].persona


def test_import_retires_anyone_left_out(seeded):
    doc = roster.export_office(seeded)
    doc["staff"] = [e for e in doc["staff"] if e["id"] != "writer"]
    installed, retired = roster.import_office(seeded, doc)
    assert "writer" in retired and "writer" not in installed
    assert not roster.exists("writer")


@pytest.mark.parametrize("mutate, why", [
    (lambda d: d.update(format="nope/1"), "wrong format"),
    (lambda d: d.update(staff=[]), "empty"),
    (lambda d: d.update(staff=[e for e in d["staff"]
                               if e["id"] != "manager"]), "no manager"),
    (lambda d: d["staff"].append(dict(d["staff"][1])), "duplicate id"),
    (lambda d: d["staff"][1].update(office_tools=["rm_rf"]), "invented tool"),
    (lambda d: d["staff"][1].update(id="../../etc/passwd"), "path traversal id"),
    (lambda d: d["staff"][1].update(persona=""), "no persona"),
])
def test_bad_office_files_are_refused_atomically(seeded, mutate, why):
    """Validation completes before any write, so a rejected file must leave
    the office exactly as it was."""
    before = [r.id for r in roster.snapshot()]
    doc = json.loads(json.dumps(roster.export_office(seeded)))
    mutate(doc)
    with pytest.raises(roster.RosterError):
        roster.import_office(seeded, doc)
    assert [r.id for r in roster.snapshot()] == before, why


# ---------------------------------------------------------------- skills --

@pytest.mark.parametrize("bad", ["has space", "semi;colon", "pipe|d", "sla/sh"])
def test_skill_names_are_validated(bad):
    with pytest.raises(ValueError):
        skills.clean([bad])


def test_skill_names_normalise():
    assert skills.clean("a:b, c  d") == ("a:b", "c", "d")
    assert skills.clean(["x", "x"]) == ("x",)     # deduped


def test_too_many_skills_is_refused():
    with pytest.raises(ValueError):
        skills.clean([f"s{i}" for i in range(30)])


def test_granted_skills_persist(seeded):
    roster.update(seeded, "writer", {"skills": ["find-skills", "a:b"]})
    roster.refresh(seeded)
    assert roster.get("writer").skills == ("find-skills", "a:b")


# -------------------------------------------------------------- notebook --

def test_activity_log_survives_concurrent_writers(store, tmp_path, monkeypatch):
    """Eight workers finishing at once is normal load. Without a lock the
    read-modify-write cycle lost ~96% of the lines."""
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "ws")
    notebook.ensure_context(store)

    def worker(n):
        for i in range(12):
            notebook.log_activity(store, f"**agent{n}** done - task {i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = notebook.read_context()
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(lines) == 40, f"log kept {len(lines)} of a 40-line cap"
    assert text.count(notebook._LOG_MARK) == 1
    assert text.count("## Recent activity") == 1
    # An interrupted atomic write must not leave litter behind.
    assert not [p for p in (tmp_path / "ws").iterdir() if p.name.startswith(".ctx-")]


def test_rewriting_context_cannot_erase_the_log(store, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "ws")
    notebook.ensure_context(store)
    notebook.log_activity(store, "**Ada** failed - something broke")

    head, mark, tail = notebook.read_context().partition(notebook._LOG_MARK)
    notebook.write_context("# Office context\n\nfresh prose\n\n" + mark + tail)

    text = notebook.read_context()
    assert "fresh prose" in text
    assert "**Ada** failed" in text, "an agent must not be able to erase history"


def test_lessons_are_capped_and_deduped(store):
    for i in range(9):
        store.add_lesson("writer", f"Lesson number {i} about this office.")
    rows = store.lessons("writer")
    assert len(rows) == store.LESSON_LIMIT
    assert "number 0" not in rows[0]["text"], "oldest should fall off first"

    assert store.add_lesson("writer", "A brand new and distinct lesson here.")
    assert not store.add_lesson("writer", "A brand new and distinct lesson here.")
    assert not store.add_lesson("writer", "tiny")


def test_lesson_block_is_empty_when_there_are_none(store):
    assert notebook.lesson_block(store, "nobody") == ""
    store.add_lesson("nobody", "Something worth carrying between tasks.")
    assert "Something worth carrying" in notebook.lesson_block(store, "nobody")


def test_long_lessons_are_truncated(store):
    store.add_lesson("sre", "x" * 900)
    assert len(store.lessons("sre")[0]["text"]) == store.LESSON_CHARS


# ----------------------------------------------------------------- usage --

def test_usage_aggregates_per_agent_and_model(store):
    class Turn:
        def __init__(self, i, o, cr, cw, cost):
            self.input_tokens, self.output_tokens = i, o
            self.cache_read, self.cache_write, self.cost_usd = cr, cw, cost

    store.add_usage_row("writer", "sonnet", Turn(10, 100, 500, 200, 0.01))
    store.add_usage_row("writer", "sonnet", Turn(5, 50, 250, 100, 0.005))
    store.add_usage_row("sre", "haiku", Turn(1, 2, 3, 4, 0.001))

    totals = store.usage_totals()
    assert totals["turns"] == 3
    assert totals["tokens"] == (10 + 100 + 500 + 200) + (5 + 50 + 250 + 100) + 10

    by_agent = {r["agent_id"]: r for r in store.usage_by_agent()}
    assert by_agent["writer"]["turns"] == 2
    assert by_agent["writer"]["output"] == 150
    assert {r["model"] for r in store.usage_by_model()} == {"sonnet", "haiku"}


def test_cache_reads_are_cheap_relative_to_output():
    """The panel exists because volume and cost point in opposite directions;
    if this ever inverts, the advice in the UI is wrong."""
    pin, pout = config.PRICING["sonnet"]
    assert pin * config.CACHE_READ_DISCOUNT < pin
    assert pin * config.CACHE_READ_DISCOUNT * 10 <= pout
    assert pin * config.CACHE_WRITE_MULTIPLIER > pin


# ------------------------------------------------------ correctness (P1) --

def test_sdk_stop_reasons_map_to_one_vocabulary():
    """The CLI reports a max_turns cut-off as an *error* result and exits
    non-zero. Read literally, every task that hit its cap was a crash and
    its author was summoned to the manager's office."""
    stop = llm._sdk_stop
    assert stop(subtype="error_max_turns", is_error=True) == "max_turns"
    assert stop(terminal_reason="max_turns", is_error=True) == "max_turns"
    assert stop(subtype="error_max_budget_usd", is_error=True) == "budget_exhausted"
    assert stop(subtype="success", stop_reason="end_turn") == "end_turn"
    assert stop(subtype="success", terminal_reason="completed") == "end_turn"
    assert stop(subtype="success", is_error=True, api_status=429) == "api_error"
    assert stop(subtype="error_during_execution", is_error=True) == "error_during_execution"
    assert stop(terminal_reason="aborted_streaming") == "cancelled"
    assert stop(subtype="success", stop_reason="max_tokens") == "max_tokens"
    for reason in ("max_turns", "budget_exhausted", "max_tokens"):
        assert reason in llm.PARTIAL_STOPS
    assert "end_turn" not in llm.PARTIAL_STOPS


def test_haiku_never_gets_the_params_it_rejects():
    """effort + adaptive thinking is a 400 on haiku: two roles broken on the
    api backend, every task."""
    for effort in ("low", "medium", "high"):
        kw = llm._sampling_kwargs("claude-haiku-4-5", effort)
        assert "output_config" not in kw
        assert kw.get("thinking", {}).get("type") != "adaptive"
        if "thinking" in kw:
            assert kw["max_tokens"] > kw["thinking"]["budget_tokens"]
    assert "thinking" not in llm._sampling_kwargs("claude-haiku-4-5", "low")
    kw = llm._sampling_kwargs("claude-sonnet-5", "medium")
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["thinking"] == {"type": "adaptive"}


def test_wait_keeps_every_task_and_never_hides_an_error():
    """One 4000-char clip across all results lost the middle tasks with no
    marker naming which; and `result or error` meant the manager saw
    "(no output)" where the error should have been."""
    long = "x" * 5000
    rows = {
        "t1": {"status": "done", "title": "one", "result": long},
        "t2": {"status": "failed", "title": "two", "result": "(no output)",
               "error": "connection refused talking to staging"},
        "t3": {"status": "partial", "title": "three", "result": "half an answer",
               "stop": "max_turns"},
        "t4": {"status": "running", "title": "four"},
        "t5": {"status": "done", "title": "five", "result": long},
    }
    out = "\n\n".join(tools._wait_entry(k, v) for k, v in rows.items())
    for tid in rows:
        assert f"--- {tid} [" in out, f"{tid} vanished from wait output"
    assert "ERROR: connection refused" in out
    assert "(no output)\n" not in out.split("--- t2")[1].split("--- t3")[0]
    assert "stopped at max turns, not finished" in out
    assert "still running" in out
    # each long result is clipped on its own, not the whole report
    assert out.count("chars truncated") == 2
    assert len(out) < 2 * tools.WAIT_RESULT_CHARS + 800


def test_migration_adds_stop_and_strips_the_handoff_skill(store):
    cols = {r[1] for r in store.db.execute("PRAGMA table_info(tasks)")}
    assert "stop" in cols

    roster.load(store)
    roster.install_pack(store, "devops", "p")
    row = dict(store.roster_rows()[0])
    row["skills"] = json.dumps(["session-handoff:session-handoff", "other:thing"])
    store.write_role(row)
    # Re-run just that migration, as an upgraded database would.
    store.db.execute("DELETE FROM settings WHERE key='migrated:roster.no_session_handoff'")
    store._migrate()
    got = json.loads(store.db.execute(
        "SELECT skills FROM roster WHERE id=?", (row["id"],)).fetchone()[0])
    assert got == ["other:thing"]
    # and running it again changes nothing
    store.db.execute("DELETE FROM settings WHERE key='migrated:roster.no_session_handoff'")
    store._migrate()


def test_manager_default_has_no_skill_grant():
    """A skill grant widens setting_sources, which loads the workspace
    CLAUDE.md - rewritten after every task - into every manager request."""
    assert config.ROLE_DEFS["manager"].skills == ()


def test_shipped_devops_office_matches_the_role_definitions():
    """offices/devops.json is what a fresh install imports. It had drifted
    from ROLE_DEFS (no read_context/learn), which silently turned learning
    off for anyone who used it."""
    doc = json.load(open(os.path.join(os.path.dirname(__file__), "..",
                                      "offices", "devops.json")))
    staff = {s["id"]: s for s in doc["staff"]}
    assert set(staff) == set(config.CORE_IDS) | set(config.PACKS["devops"]["staff"])
    for sid, entry in staff.items():
        role = config.ROLE_DEFS[sid]
        assert tuple(entry["office_tools"]) == role.office_tools, sid
        assert tuple(entry["native_tools"]) == role.native_tools, sid
        assert tuple(entry.get("skills", ())) == role.skills, sid
        assert entry["model"] == role.model and entry["effort"] == role.effort, sid
        assert entry["max_turns"] == role.max_turns, sid
