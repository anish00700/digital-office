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
