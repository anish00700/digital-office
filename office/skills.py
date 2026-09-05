"""Agent Skills: packaged instruction sets an employee can be given.

A skill is a folder with a SKILL.md whose frontmatter carries a name and a
description. Claude Code discovers them from the user's skills directory, the
project's, and from installed plugins; the Agent SDK takes the enabled set as
`ClaudeAgentOptions.skills`, so granting one to an employee is a matter of
putting its name on that list.

Discovery here exists to populate a picker, not to be authoritative. Plugin
skill names are qualified `plugin:skill`, and the plugin's own name does not
always match the directory it is cached in - the Vercel plugin caches under
`vercel-plugin` but addresses its skills as `vercel:...`. So the id derived
below is a best guess, the GUI lets a name be typed by hand, and a name the
SDK does not recognise fails loudly at connect() rather than silently doing
nothing.
"""

import json
import os
import re
from pathlib import Path

# Rebuilt at most this often; scanning a plugin cache is cheap but not free.
_CACHE = {"at": 0.0, "skills": []}
_TTL_S = 30.0

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _read_skill(path: Path):
    """Pull name and description out of a SKILL.md frontmatter block."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    match = _FRONTMATTER.search(head)
    name, description = "", ""
    if match:
        for line in match.group(1).splitlines():
            if line.startswith("name:") and not name:
                name = line.split(":", 1)[1].strip()
            elif line.startswith("description:") and not description:
                description = line.split(":", 1)[1].strip()
    return {
        "name": name or path.parent.name,
        "description": description,
    }


def _plugin_name(plugin_dir: Path) -> str:
    """A plugin's declared name, falling back to its directory."""
    for candidate in (plugin_dir / ".claude-plugin" / "plugin.json",
                      plugin_dir / ".plugin" / "plugin.json",
                      plugin_dir / "plugin.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = str(data.get("name") or "").strip()
        if name:
            return name
    return plugin_dir.name


def _scan(root: Path, source: str, plugin: str = ""):
    out = []
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        info = _read_skill(skill_md)
        if not info:
            continue
        bare = info["name"]
        out.append({
            "id": f"{plugin}:{bare}" if plugin else bare,
            "name": bare,
            "plugin": plugin,
            "source": source,
            "description": info["description"],
            "path": str(skill_md.parent),
        })
    return out


def discover(force=False):
    """Every skill this machine can offer, newest scan cached briefly."""
    import time
    now = time.time()
    if not force and _CACHE["skills"] and now - _CACHE["at"] < _TTL_S:
        return _CACHE["skills"]

    home = _home()
    found = []
    found += _scan(home / "skills", "user")
    found += _scan(Path.cwd() / ".claude" / "skills", "project")

    # Plugins: ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/*
    cache = home / "plugins" / "cache"
    if cache.is_dir():
        for marketplace in sorted(p for p in cache.iterdir() if p.is_dir()):
            for plugin_dir in sorted(p for p in marketplace.iterdir() if p.is_dir()):
                versions = sorted((p for p in plugin_dir.iterdir() if p.is_dir()),
                                  key=lambda p: p.name, reverse=True)
                for version in versions[:1]:          # newest installed only
                    name = _plugin_name(version)
                    found += _scan(version / "skills", "plugin", plugin=name)

    # Two plugins can ship a skill of the same name; keep the first and let the
    # id disambiguate rather than dropping one silently.
    seen, unique = set(), []
    for skill in found:
        if skill["id"] in seen:
            continue
        seen.add(skill["id"])
        unique.append(skill)
    unique.sort(key=lambda s: (s["source"] != "user", s["id"]))

    _CACHE.update(at=now, skills=unique)
    return unique


def known_ids():
    return {s["id"] for s in discover()} | {s["name"] for s in discover()}


def clean(values):
    """Normalise a granted-skill list.

    Unknown names are kept rather than rejected: the picker's derived id can be
    wrong for a plugin whose declared name differs from its directory, and the
    person typing the exact name is more likely to be right than this module's
    guess. A genuinely bad name surfaces as a loud SDK error on the next task.
    """
    if isinstance(values, str):
        values = [v.strip() for v in values.replace(",", " ").split()]
    out = []
    for value in values or ():
        value = str(value).strip()
        if not value or len(value) > 120:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise ValueError(f"{value!r} is not a valid skill name")
        if value not in out:
            out.append(value)
    if len(out) > 24:
        raise ValueError("that is more than 24 skills; every enabled skill's "
                         "description is loaded into the prompt")
    return tuple(out)
