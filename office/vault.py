"""The office's own credentials, kept out of every process environment.

Two sources, one registry:

  .env          parsed here at import time, before config reads anything.
                Parsed, never sourced: a config file must not run commands.
  environment   whatever systemd or a shell exported; the scrub in config
                moves secret-shaped names in here before dropping them.

Secret-shaped keys from .env go into the registry only. They are never
exported, so they never appear in this process's exec-time environment block
(what `ps e` shows) and never reach an agent subprocess. The SDK's own
credential is the one exception: the agent process must authenticate, so it
is set on the live environment - still absent from the exec-time block.
"""

import os
import re
from pathlib import Path

SECRET_PATTERN = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|_KEY$|API_KEY|ACCESS_KEY", re.I)
SDK_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_")
# Non-secret-shaped names that are still the office's business, not an agent's.
OFFICE_SECRET_NAMES = ("SLACK_TOKEN", "SLACK_CHANNELS", "MAIL_HOST", "MAIL_USER",
                       "MAIL_PASSWORD", "MAIL_FOLDER", "OFFICE_NOTIFY_URL",
                       "OFFICE_NOTIFY_TOKEN")

_REGISTRY: dict = {}
LOADED_FROM = None


def secret(name: str, default=None):
    """Registry first, environment second. The environment wins for anything a
    supervisor exported, because that is what `officectl` has always promised:
    an explicit export overrides the file."""
    if name in os.environ:
        return os.environ[name]
    return _REGISTRY.get(name, default)


def capture(name: str):
    """Move one value from the environment into the registry, ahead of the
    scrub deleting it. No-op if absent."""
    if name in os.environ:
        _REGISTRY[name] = os.environ[name]


def is_secret_name(name: str) -> bool:
    return bool(SECRET_PATTERN.search(name)) or name in OFFICE_SECRET_NAMES


def load_dotenv(path) -> int:
    """Parse KEY=VALUE lines. Secret-shaped keys -> registry. SDK credentials
    -> live environment (setdefault). Everything else -> live environment
    (setdefault). Returns the number of keys read."""
    global LOADED_FROM
    path = Path(path)
    if not path.is_file():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if key.startswith(SDK_PREFIXES):
            os.environ.setdefault(key, value)
        elif is_secret_name(key):
            if key not in os.environ:
                _REGISTRY.setdefault(key, value)
        else:
            os.environ.setdefault(key, value)
        n += 1
    LOADED_FROM = path
    return n
