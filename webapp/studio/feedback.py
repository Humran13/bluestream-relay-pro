"""Help & Feedback: bug reports / suggestions saved to a LOCAL queue (JSONL),
with redaction, consent, size limits and rate limiting. Nothing is transmitted
automatically. If BLUESTREAM_FEEDBACK_ENDPOINT is configured, an operator-run
forwarder may deliver queued items; delivery state is tracked per item.
That central endpoint does not exist yet: this module only prepares the data.
"""
import json
import os
import re
import time
import uuid

QUEUE_NAME = "feedback-queue.jsonl"
MAX_MESSAGE = 4000
MAX_PER_HOUR = 10
KINDS = ("bug", "suggestion")

# Things that must never leave the box: stream keys, tokens, passwords, URLs with creds.
_REDACTIONS = [
    (re.compile(r"(rtmps?://[^\s/]+/[^\s/]+/)[^\s\"']{4,}", re.I), r"\1[REDACTED-KEY]"),
    (re.compile(r"\b(BS1R?\.[A-Za-z0-9_.-]{20,})"), "[REDACTED-LICENCE]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|stream[_-]?key|authorization)\b\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
    (re.compile(r"\b[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3,4}\b"), "[REDACTED-KEY]"),  # YouTube-style keys
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), "[REDACTED-HEX]"),
]


class FeedbackError(Exception):
    pass


def redact(text):
    for rx, rep in _REDACTIONS:
        text = rx.sub(rep, text)
    return text


def queue_path():
    base = os.environ.get("BLUESTREAM_STUDIO_DIR", "/var/lib/bluestream/studio")
    return os.path.join(base, QUEUE_NAME)


def endpoint():
    return os.environ.get("BLUESTREAM_FEEDBACK_ENDPOINT", "")


def _entries():
    try:
        with open(queue_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        return []


def queue_summary():
    items = _entries()
    return {"total": len(items), "pending": sum(1 for i in items if i.get("delivery") == "pending"),
            "recent": items[-5:][::-1]}


def diagnostics():
    """Non-sensitive environment facts only (no config, no media, no keys)."""
    ver = "unknown"
    for p in ("/usr/local/lib/bluestream/VERSION", os.path.join(os.path.dirname(__file__), "..", "..", "VERSION")):
        try:
            ver = open(p).read().strip()
            break
        except OSError:
            pass
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"version": ver, "cpus": os.cpu_count(), "load1": load,
            "studio_enabled": os.environ.get("BLUESTREAM_STUDIO_ENABLED") == "1"}


def submit(kind, message, consent, include_diagnostics, client="?", now=None):
    now = time.time() if now is None else now
    if kind not in KINDS:
        raise FeedbackError("Choose bug report or suggestion.")
    message = (message or "").strip()
    if not 10 <= len(message) <= MAX_MESSAGE:
        raise FeedbackError("Please write between 10 and %d characters." % MAX_MESSAGE)
    if not consent:
        raise FeedbackError("Please tick the consent box so we know you agree to this being saved.")
    recent = [e for e in _entries() if now - e.get("ts", 0) < 3600]
    if len(recent) >= MAX_PER_HOUR:
        raise FeedbackError("Too many reports in the last hour; please try again later.")
    entry = {"id": uuid.uuid4().hex[:12], "ts": int(now), "kind": kind, "message": redact(message),
             "delivery": "pending", "attempts": 0}
    if include_diagnostics:
        entry["diagnostics"] = diagnostics()
    path = queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def export_text():
    """Plain-text export the user can attach to an email/issue by hand."""
    return "\n".join(json.dumps(e, sort_keys=True) for e in _entries())
