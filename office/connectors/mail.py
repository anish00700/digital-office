"""Email connector for the Comms desk, over plain IMAP.

Read-only. Iris opens nothing, sends nothing, deletes nothing, and does not
mark anything as read - she reads headers and a short body excerpt so she can
triage, and that is all.

To enable (Gmail example):
  1. Turn on 2FA, then create an app password at
     https://myaccount.google.com/apppasswords
  2. Export:
       export MAIL_HOST=imap.gmail.com
       export MAIL_USER=you@gmail.com
       export MAIL_PASSWORD=your-app-password
       export MAIL_FOLDER=INBOX          # optional

Works with any IMAP server: Fastmail, Proton Bridge, a corporate Exchange with
IMAP enabled. Use an app-specific password, never your primary one.
"""

import email
import email.header
import email.utils
import imaplib
import os
import time


def _decode(raw):
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return " ".join(" ".join(out).split())


def _body_excerpt(msg, limit=300):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    return " ".join(payload.decode("utf-8", "replace").split())[:limit]
            return ""
        payload = msg.get_payload(decode=True) or b""
        return " ".join(payload.decode("utf-8", "replace").split())[:limit]
    except Exception:
        return ""


async def fetch(since_hours=12):
    host = os.environ.get("MAIL_HOST")
    user = os.environ.get("MAIL_USER")
    password = os.environ.get("MAIL_PASSWORD")
    if not (host and user and password):
        return ("Email is not configured. Set MAIL_HOST, MAIL_USER and "
                "MAIL_PASSWORD to enable this desk. Report this to your "
                "principal rather than guessing at content.")

    folder = os.environ.get("MAIL_FOLDER", "INBOX")
    since = time.strftime("%d-%b-%Y", time.localtime(time.time() - since_hours * 3600))

    try:
        conn = imaplib.IMAP4_SSL(host, timeout=25)
        conn.login(user, password)
        # readonly=True so nothing is silently marked as seen.
        conn.select(folder, readonly=True)
        typ, data = conn.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            conn.logout()
            return f"IMAP search failed in {folder}"

        ids = data[0].split()[-60:]
        out = []
        for mid in reversed(ids):
            typ, payload = conn.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            msg = email.message_from_bytes(payload[0][1])
            when = email.utils.parsedate_to_datetime(msg.get("Date")) \
                if msg.get("Date") else None
            if when and when.timestamp() < time.time() - since_hours * 3600:
                continue
            out.append(
                f"From: {_decode(msg.get('From'))}\n"
                f"Subject: {_decode(msg.get('Subject'))}\n"
                f"{_body_excerpt(msg)}"
            )
        conn.close()
        conn.logout()
        if not out:
            return f"No mail in {folder} in the last {since_hours}h."
        return "\n\n".join(out[:40])
    except Exception as exc:
        return f"Mail fetch failed: {type(exc).__name__}: {exc}"
