"""The live staff roster: hiring, firing, and changing what someone can do.

`config.DEFAULT_ROSTER` is a seed, not the truth. On first boot it is written
into SQLite; after that the table owns who works here, so a hire survives a
restart exactly the way a task does. Everything reads the roster through
`config.ROSTER` / `config.ROLES` / `config.STAFF_IDS`, which resolve here.

Changes take effect on the next task, not the current one. A worker mid-task
keeps the model and tools it started with - swapping them underneath a running
agent loop would mean a request whose tool results no longer match its tools.
"""

import json
import os
import re
import threading
import time

from . import config, skills as skills_mod

# The live set. None until load() runs, so anything that reads the roster
# before the daemon has a store (config.validate, --help) sees the seed.
_ACTIVE = None
_LOCK = threading.RLock()

# Assigned round-robin to new hires so two people are never the same colour.
_PALETTE = ("#3d7ec2", "#7a4fc0", "#c04f8a", "#2f9e8f", "#b8952f", "#5a7d3a",
            "#a8503a", "#4f8fc0", "#9c5fb0", "#c2703d", "#5f9c7a", "#b0704f")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")

_COLUMNS = ("id", "name", "title", "emoji", "color", "desk_x", "desk_y",
            "persona", "office_tools", "native_tools", "model", "effort",
            "max_turns", "reports_to", "active", "hired_at")


class RosterError(ValueError):
    """A hire/fire/update the office refuses to make. Message is user-facing."""


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def snapshot():
    """Every active Role, manager first. Falls back to the seed if unloaded."""
    return _ACTIVE if _ACTIVE is not None else config.DEFAULT_ROSTER


def get(agent_id):
    for r in snapshot():
        if r.id == agent_id:
            return r
    raise KeyError(agent_id)


def exists(agent_id):
    return any(r.id == agent_id for r in snapshot())


def staff_ids():
    return tuple(r.id for r in snapshot() if r.id != config.MANAGER_ID)


def taken_desks():
    return {tuple(r.desk) for r in snapshot()}


def free_desks():
    taken = taken_desks()
    return [list(slot) for slot in config.DESK_SLOTS if slot not in taken]


def catalogue():
    """What the GUI may offer. Sent alongside the roster so the panel does not
    have to hardcode a copy of the tool list."""
    from . import tools
    return {
        "office_tools": sorted(tools.tool_names()),
        "native_tools": list(config.NATIVE_TOOL_CHOICES),
        "skills": skills_mod.discover(),
        "models": list(config.MODEL_CHOICES),
        "efforts": list(config.EFFORT_CHOICES),
        "free_desks": free_desks(),
        "default_model": config.MODEL_SMART,
    }


def as_dict(role, full=False):
    """Role -> JSON. `full` adds the fields only the staff editor needs."""
    out = {
        "id": role.id, "name": role.name, "title": role.title,
        "emoji": role.emoji, "color": role.color, "desk": list(role.desk),
        "manager": role.id == config.MANAGER_ID,
    }
    if full:
        out.update({
            "persona": role.persona,
            "office_tools": list(role.office_tools),
            "native_tools": list(role.native_tools),
            "skills": list(role.skills),
            "model": role.model,
            "model_id": role.model_id,
            "effort": role.effort,
            "max_turns": role.max_turns,
            "reports_to": role.reports_to,
        })
    return out


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _to_row(role, active=1, hired_at=None):
    return {
        "id": role.id, "name": role.name, "title": role.title,
        "emoji": role.emoji, "color": role.color,
        "desk_x": int(role.desk[0]), "desk_y": int(role.desk[1]),
        "persona": role.persona,
        "office_tools": json.dumps(list(role.office_tools)),
        "native_tools": json.dumps(list(role.native_tools)),
        "skills": json.dumps(list(role.skills)),
        "model": role.model, "effort": role.effort,
        "max_turns": int(role.max_turns), "reports_to": role.reports_to,
        "active": active, "hired_at": hired_at if hired_at is not None else time.time(),
    }


def _from_row(row):
    def _tuple(raw):
        try:
            return tuple(json.loads(raw or "[]"))
        except (TypeError, json.JSONDecodeError):
            return ()

    return config.Role(
        id=row["id"], name=row["name"], title=row["title"], emoji=row["emoji"],
        color=row["color"], desk=(row["desk_x"], row["desk_y"]),
        persona=row["persona"],
        office_tools=_tuple(row["office_tools"]),
        native_tools=_tuple(row["native_tools"]),
        skills=_tuple(row.get("skills") if hasattr(row, "get") else row["skills"]),
        model=row["model"] or "", effort=row["effort"] or "low",
        max_turns=int(row["max_turns"] or 8),
        reports_to=row["reports_to"] or "",
    )


def setup_needed(store):
    """True until the owner has chosen a pack. The GUI blocks on this."""
    return store.setting("setup_complete") != "1"


def principal(store):
    """Who this office works for. Stored, so the GUI can change it; seeded
    from OFFICE_PRINCIPAL so a provisioned install can skip the question."""
    return store.setting("principal") or config.PRINCIPAL


def packs():
    """Pack menu for the setup screen, with the staff each one seats."""
    out = []
    for pid, pack in config.PACKS.items():
        staff = [config.ROLE_DEFS[i] for i in pack["staff"] if i in config.ROLE_DEFS]
        core = [config.ROLE_DEFS[i] for i in config.CORE_IDS if i in config.ROLE_DEFS]
        out.append({
            "id": pid,
            "name": pack["name"],
            "blurb": pack["blurb"],
            "principal": pack["principal"],
            "staff": [{"emoji": r.emoji, "name": r.name, "title": r.title,
                       "color": r.color} for r in core + staff],
        })
    return out


def _seat(store, role, taken):
    """Desks come from the floor plan, not from the role definition: a pack is
    an arbitrary set of people and their hardcoded seats would collide."""
    if role.id == config.MANAGER_ID:
        return role
    free = [d for d in config.DESK_SLOTS if d not in taken]
    if not free:
        return None
    taken.add(free[0])
    return config.Role(**{**role.__dict__, "desk": free[0]})


def install_pack(store, pack_id, principal_text=""):
    """First run. Seats the core staff plus the chosen pack, and records the
    choice so this never runs again."""
    with _LOCK:
        pack = config.PACKS.get(pack_id)
        if pack is None:
            raise RosterError(f"no such pack {pack_id!r}")

        taken = {(r["desk_x"], r["desk_y"]) for r in store.roster_rows()}
        known = {r["id"] for r in store.roster_rows(include_departed=True)}
        base = time.time()
        order = list(config.CORE_IDS) + [i for i in pack["staff"]
                                         if i not in config.CORE_IDS]
        for i, rid in enumerate(order):
            role = config.ROLE_DEFS.get(rid)
            if role is None or rid in known:
                continue
            seated = _seat(store, role, taken)
            if seated is None:
                break                              # floor is full
            store.write_role(_to_row(seated, hired_at=base + i * 0.001))

        text = (principal_text or "").strip() or pack["principal"]
        store.set_setting("principal", text)
        store.set_setting("pack", pack_id)
        store.set_setting("setup_complete", "1")
        return refresh(store)


def load(store):
    """Read the roster into memory, seeding only what must always exist.

    Domain staff are chosen once, on first run, through install_pack. What is
    seeded here is the machinery every office needs whatever its subject:
    Miles to delegate and Wren to hire. Doing it by id, against every row
    active or not, means a role added in a later version arrives on upgrade
    while anyone you fired stays fired.
    """
    with _LOCK:
        known = {r["id"] for r in store.roster_rows(include_departed=True)}
        taken = {(r["desk_x"], r["desk_y"]) for r in store.roster_rows()}
        base = time.time()
        for i, rid in enumerate(config.CORE_IDS):
            role = config.ROLE_DEFS.get(rid)
            if role is None or rid in known:
                continue
            seated = _seat(store, role, taken)
            if seated is not None:
                store.write_role(_to_row(seated, hired_at=base + i * 0.001))

        # An office that predates staff packs is already set up by definition.
        # Two shapes: a roster table that already holds domain staff, or - from
        # before the roster table existed at all - only the original hardcoded
        # eight in `agents`. The second shape used to land on the first-run
        # screen with everyone gone; it now gets the infrastructure pack back.
        if store.setting("setup_complete") is None:
            if store.roster_count() > len(config.CORE_IDS):
                store.set_setting("setup_complete", "1")
                store.set_setting("pack", "devops")
            else:
                legacy = {a["id"] for a in store.agents()} - set(config.CORE_IDS)
                if legacy and legacy <= set(config.PACKS["devops"]["staff"]):
                    return install_pack(store, "devops", "")

        # A pack named in the environment answers the first-run question
        # without a browser, for provisioned and headless installs.
        if setup_needed(store) and config.DEFAULT_PACK in config.PACKS:
            return install_pack(store, config.DEFAULT_PACK,
                                os.environ.get("OFFICE_PRINCIPAL", ""))
        return refresh(store)


def refresh(store):
    global _ACTIVE
    with _LOCK:
        roles = [_from_row(r) for r in store.roster_rows()]
        # The manager sorts first; the GUI and the floor both read in this order.
        roles.sort(key=lambda r: (r.id != config.MANAGER_ID,))
        _ACTIVE = tuple(roles)
        return _ACTIVE


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

EXPORT_FORMAT = "digital-office/1"


def export_office(store):
    """The whole office as a portable document: who works here, what they know,
    and who they work for. Everything an office is, minus its history."""
    return {
        "format": EXPORT_FORMAT,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "principal": principal(store),
        "pack": store.setting("pack") or "",
        "staff": [as_dict(r, full=True) for r in snapshot()],
    }


def import_office(store, doc):
    """Replace the roster with an exported one. Every field goes through the
    same validation as a hand edit, because a file off disk is exactly as
    untrusted as a form post - it must not be able to grant a tool that does
    not exist, or a colour the renderer cannot draw.

    Returns (installed_ids, retired_ids).
    """
    if not isinstance(doc, dict):
        raise RosterError("that is not an office file")
    fmt = str(doc.get("format") or "")
    if fmt != EXPORT_FORMAT:
        raise RosterError(f"unsupported office format {fmt!r}; expected {EXPORT_FORMAT}")
    staff = doc.get("staff")
    if not isinstance(staff, list) or not staff:
        raise RosterError("that office file has nobody in it")
    if not any((e or {}).get("id") == config.MANAGER_ID for e in staff):
        raise RosterError(f"an office needs its {config.MANAGER_ID}; this file has none")

    with _LOCK:
        prepared, seen, taken = [], set(), set()
        for entry in staff:
            if not isinstance(entry, dict):
                raise RosterError("every entry must be an employee object")
            rid = str(entry.get("id") or "").strip().lower()
            if not _ID_RE.match(rid):
                raise RosterError(f"bad employee id {entry.get('id')!r}")
            if rid in seen:
                raise RosterError(f"{rid} appears twice")
            seen.add(rid)

            clean = _validate_common({
                "name": entry.get("name"),
                "title": entry.get("title", "Staff"),
                "emoji": entry.get("emoji", ""),
                "color": entry.get("color", _PALETTE[len(prepared) % len(_PALETTE)]),
                "persona": entry.get("persona"),
                "office_tools": entry.get("office_tools", []),
                "native_tools": entry.get("native_tools", []),
                "skills": entry.get("skills", []),
                "model": entry.get("model", ""),
                "effort": entry.get("effort", "low"),
                "max_turns": entry.get("max_turns", 8),
            }, existing_id=rid)

            # Seat from the file where the slot is legal and free, otherwise
            # from the floor plan. An office file should survive a floor plan
            # that has changed since it was written.
            desk = tuple(entry.get("desk") or ()) if entry.get("desk") else ()
            if rid == config.MANAGER_ID:
                clean["desk"] = config.ROLE_DEFS[config.MANAGER_ID].desk
            elif len(desk) == 2 and tuple(desk) in config.DESK_SLOTS \
                    and tuple(desk) not in taken:
                clean["desk"] = (int(desk[0]), int(desk[1]))
            else:
                free = [d for d in config.DESK_SLOTS if d not in taken]
                if not free:
                    raise RosterError("that office has more staff than this floor has desks")
                clean["desk"] = free[0]
            if rid != config.MANAGER_ID:
                taken.add(clean["desk"])

            prepared.append(config.Role(
                id=rid, reports_to=str(entry.get("reports_to") or config.MANAGER_ID),
                **clean))

        base = time.time()
        for i, role in enumerate(prepared):
            store.write_role(_to_row(role, hired_at=base + i * 0.001))

        # Anyone not in the file is no longer on the payroll. Soft delete, so
        # their past work stays readable.
        retired = [r.id for r in snapshot() if r.id not in seen]
        for rid in retired:
            store.set_role_active(rid, False)

        text = str(doc.get("principal") or "").strip()
        if text:
            store.set_setting("principal", text)
        if doc.get("pack"):
            store.set_setting("pack", str(doc["pack"]))
        store.set_setting("setup_complete", "1")
        refresh(store)
        return sorted(seen), retired


def _clean_tools(values, valid, label):
    out = []
    for name in values or ():
        name = str(name).strip()
        if not name:
            continue
        if name not in valid:
            raise RosterError(f"unknown {label} {name!r}")
        if name not in out:
            out.append(name)
    return tuple(out)


def _validate_common(fields, *, existing_id=None):
    """Normalises and range-checks the editable fields. Returns a dict."""
    from . import tools
    out = {}

    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not 1 <= len(name) <= 40:
            raise RosterError("name must be 1-40 characters")
        out["name"] = name

    if "title" in fields:
        out["title"] = str(fields["title"] or "").strip()[:60] or "Staff"

    if "emoji" in fields:
        out["emoji"] = (str(fields["emoji"] or "").strip() or "🧑‍💻")[:8]

    if "color" in fields:
        color = str(fields["color"] or "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise RosterError("color must be a #rrggbb hex value")
        out["color"] = color

    if "persona" in fields:
        persona = str(fields["persona"] or "").strip()
        if len(persona) < 20:
            raise RosterError("persona must be at least 20 characters - it is the "
                              "entire system prompt for this employee")
        # BUDGET: the persona is re-sent on every request this employee runs.
        if len(persona) > 8000:
            raise RosterError("persona must be under 8000 characters")
        out["persona"] = persona

    if "office_tools" in fields:
        out["office_tools"] = _clean_tools(
            fields["office_tools"], tools.tool_names(), "office tool")

    if "native_tools" in fields:
        out["native_tools"] = _clean_tools(
            fields["native_tools"], set(config.NATIVE_TOOL_CHOICES), "native tool")

    if "skills" in fields:
        try:
            out["skills"] = skills_mod.clean(fields["skills"])
        except ValueError as exc:
            raise RosterError(str(exc)) from None

    if "model" in fields:
        model = str(fields["model"] or "").strip()
        if model not in config.MODEL_CHOICES:
            raise RosterError(
                f"model must be one of: {', '.join(m or '(default)' for m in config.MODEL_CHOICES)}")
        out["model"] = model

    if "effort" in fields:
        effort = str(fields["effort"] or "").strip()
        if effort not in config.EFFORT_CHOICES:
            raise RosterError(f"effort must be one of: {', '.join(config.EFFORT_CHOICES)}")
        out["effort"] = effort

    if "max_turns" in fields:
        try:
            turns = int(fields["max_turns"])
        except (TypeError, ValueError):
            raise RosterError("max_turns must be a whole number") from None
        if not 1 <= turns <= 40:
            raise RosterError("max_turns must be between 1 and 40")
        out["max_turns"] = turns

    if "desk" in fields and fields["desk"] is not None:
        desk = fields["desk"]
        try:
            desk = (int(desk[0]), int(desk[1]))
        except (TypeError, ValueError, IndexError):
            raise RosterError("desk must be a pair of tile coordinates") from None
        if desk not in config.DESK_SLOTS:
            raise RosterError("that desk is not on the floor plan")
        holder = next((r.id for r in snapshot() if tuple(r.desk) == desk), None)
        if holder is not None and holder != existing_id:
            raise RosterError(f"{holder} already sits there")
        out["desk"] = desk

    return out


def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:20]
    if not base or not base[0].isalpha():
        base = f"staff_{base}".strip("_")[:20]
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}_{n}"[:24]
        n += 1
    return candidate


def hire(store, fields):
    """Add an employee. Returns the new Role."""
    with _LOCK:
        current = snapshot()
        taken_ids = {r.id for r in current}

        agent_id = str(fields.get("id") or "").strip().lower()
        if agent_id:
            if not _ID_RE.match(agent_id):
                raise RosterError("id must be 2-24 chars, lowercase letters, "
                                  "digits and underscores, starting with a letter")
        else:
            agent_id = _slug(fields.get("name") or "staff", taken_ids)
        if agent_id in taken_ids:
            raise RosterError(f"{agent_id} already works here")

        if len(current) >= len(config.DESK_SLOTS) + 1:
            raise RosterError("the floor is full - fire someone or add a desk to "
                              "DESK_SLOTS in office/config.py")

        clean = _validate_common(
            {
                "name": fields.get("name"),
                "title": fields.get("title", "Staff"),
                "emoji": fields.get("emoji", ""),
                "persona": fields.get("persona"),
                "office_tools": fields.get("office_tools",
                                           ["note", "ask_human", "finish"]),
                "native_tools": fields.get("native_tools", []),
                "skills": fields.get("skills", []),
                "model": fields.get("model", ""),
                "effort": fields.get("effort", "low"),
                "max_turns": fields.get("max_turns", 8),
            },
            existing_id=agent_id,
        )

        desk = fields.get("desk")
        if desk:
            clean.update(_validate_common({"desk": desk}, existing_id=agent_id))
        else:
            free = free_desks()
            if not free:
                raise RosterError("every desk is occupied")
            clean["desk"] = tuple(free[0])

        used_colors = {r.color for r in current}
        color = fields.get("color") or next(
            (c for c in _PALETTE if c not in used_colors), _PALETTE[0])
        clean.update(_validate_common({"color": color}))

        role = config.Role(id=agent_id, reports_to=config.MANAGER_ID, **clean)
        store.write_role(_to_row(role))
        refresh(store)
        return role


def fire(store, agent_id):
    """Mark an employee departed. Soft delete: their tasks, transcript and
    usage history stay readable, and the desk frees up for the next hire."""
    with _LOCK:
        if agent_id == config.MANAGER_ID:
            raise RosterError("Miles runs the office - he cannot be let go")
        if not exists(agent_id):
            raise RosterError(f"{agent_id} does not work here")
        store.set_role_active(agent_id, False)
        refresh(store)


def update(store, agent_id, fields):
    """Change an existing employee. Returns the updated Role."""
    with _LOCK:
        try:
            current = get(agent_id)
        except KeyError:
            raise RosterError(f"{agent_id} does not work here") from None

        editable = {k: v for k, v in fields.items() if k in (
            "name", "title", "emoji", "color", "persona", "office_tools",
            "native_tools", "skills", "model", "effort", "max_turns", "desk")}
        if not editable:
            raise RosterError("nothing to change")

        clean = _validate_common(editable, existing_id=agent_id)
        if agent_id == config.MANAGER_ID and "office_tools" in clean:
            missing = {"assign", "wait"} - set(clean["office_tools"])
            if missing:
                raise RosterError("the manager needs assign and wait to delegate; "
                                  f"missing {', '.join(sorted(missing))}")

        role = config.Role(**{**current.__dict__, **clean})
        # Preserve hire order so the listing does not reshuffle on every edit.
        row = next((r for r in store.roster_rows() if r["id"] == agent_id), None)
        store.write_role(_to_row(role, hired_at=row["hired_at"] if row else None))
        refresh(store)
        return role
