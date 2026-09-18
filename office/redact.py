"""Secrets are found and replaced at every boundary the office controls.

A model will, sooner or later, echo whatever it reads. So nothing it reads
arrives unscanned, and nothing it says is stored unscanned: tool results
(office tools, and the CLI's built-in Bash/Read/Grep through a PostToolUse
hook), fetched Slack and mail, the model's own text before it reaches the
transcript, a task result, the chat, a notification, or a note.

Two kinds of pattern. *Shapes* - AWS/GCP/GitHub/Slack/Anthropic keys, JWTs,
private-key blocks, bearer tokens, `password=`, `user:pass@host` - are
matched with severity "high". A high-entropy token that fits no known shape
is "low": redacted, but never a reason to lock the office. The office's own
secret *values* (from the vault) are matched exactly and are always high.

What is logged is the kind of thing found, never the thing itself.
"""

import math
import re

from . import config, vault

_H, _L = "high", "low"

PATTERNS = (
    ("private_key", _H, re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws_key", _H, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("gcp_key", _H, re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("github_token", _H, re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", _H, re.compile(r"\bxox[abposre]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic_key", _H, re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", _H, re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("stripe_key", _H, re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", _H, re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", _H, re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})")),
    ("url_password", _H, re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]{4,})@")),
    ("assignment", _H, re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret)\s*[:=]\s*['\"]?([^\s'\"`,;]{6,})")),
)

_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _entropy(s):
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _user_patterns():
    out = []
    for raw in (config.REDACT_PATTERNS or ()):
        try:
            out.append(("custom", _H, re.compile(raw)))
        except re.error:
            continue
    return out


def _own_secrets():
    """The office's own credentials, exact. These are the ones we would be
    most embarrassed to see in a transcript, and the only ones we know."""
    values = set()
    for name, value in list(vault._REGISTRY.items()):
        if value and len(value) >= 8 and vault.is_secret_name(name):
            values.add(value)
    import os
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "OFFICE_TOKEN"):
        value = os.environ.get(name) or vault._REGISTRY.get(name)
        if value and len(value) >= 8:
            values.add(value)
    return values


def redact(text):
    """Return (clean_text, hits) where hits is a list of (kind, severity).
    A non-string is returned unchanged with no hits."""
    if not isinstance(text, str) or not text:
        return text, []
    hits = []
    out = text
    for value in _own_secrets():
        if value in out:
            out = out.replace(value, "[REDACTED:office_secret]")
            hits.append(("office_secret", _H))
    for name, severity, rx in PATTERNS + tuple(_user_patterns()):
        def _sub(m, name=name):
            if m.groups():
                # keep the label, replace only the value
                g = m.group(1)
                start = m.start(1) - m.start(0)
                return m.group(0)[:start] + f"[REDACTED:{name}]" + m.group(0)[start + len(g):]
            return f"[REDACTED:{name}]"
        out, n = rx.subn(_sub, out)
        if n:
            hits.append((name, severity))

    def _entropic(m):
        tok = m.group(0)
        if "[REDACTED" in tok or _HEX.match(tok) or _entropy(tok) < 4.2:
            return tok
        # mixed character classes: a random secret, not a long word or a path
        classes = sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
        if classes < 3:
            return tok
        hits.append(("high_entropy", _L))
        return "[REDACTED:high_entropy]"
    out = _TOKEN.sub(_entropic, out)
    return out, hits


def contains_secret(text, high_only=True):
    """True when `text` carries something that looks like a secret. Used on
    outbound things - a command line, a URL, a search query - where the
    right answer is to refuse, not to redact."""
    _, hits = redact(text)
    return any(sev == _H for _, sev in hits) if high_only else bool(hits)


def redact_obj(obj):
    """Deep-walk a tool response, redacting every string, keeping the shape
    so the CLI accepts it back. Returns (new_obj, hits)."""
    hits = []
    def walk(x):
        if isinstance(x, str):
            clean, h = redact(x)
            hits.extend(h)
            return clean
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x
    return walk(obj), hits


def summarise(hits):
    """'aws_key, jwt' - kinds only, never values, for the audit row."""
    return ", ".join(sorted({name for name, _ in hits}))


def is_high(hits):
    return any(sev == _H for _, sev in hits)
