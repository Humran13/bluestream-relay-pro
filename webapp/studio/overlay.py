"""In-place overlays: Studio composition added to an EXISTING relay's broadcast, keeping its identity.

The relay keeps its name, its bus stream (`bluestream-relay/<name>`), its destination publishers and their stream keys. Only the
FFmpeg process that feeds the bus changes: instead of `-c copy` it decodes, draws the overlays and re-encodes. Because the
destination publishers pull from the bus (and nginx-rtmp keeps their play connection when the producer is replaced) the
YouTube session is not torn down by a swap (proven by the bus-swap tests; a restart of the relay UNIT is what would reconnect them).

State, all small files:
  <studio dir>/overlay-<relay>.json   written by the dashboard: {"project", "enabled", "nonce"}   (the on/off switch)
  <run dir>/studio-overlay-<relay>.*  written by the relay account: start log, healthy mark, tripped (circuit breaker)

Fail-safe: every refusal here makes the relay fall back to the ORIGINAL stream-copy command line. A compositor that cannot start,
or that crash-loops, can therefore never leave the stream without a picture.
"""
import os
import re
import time

from . import runtime, store
from .model import NAME_RE, ValidationError

WINDOW_S = 120          # starts counted by the circuit breaker
MAX_STARTS = 1          # a SECOND start inside WINDOW_S without a healthy run in between -> overlays are switched off (plain stream restored)
HEALTHY_AFTER_S = 20    # continuous HEALTHY output that proves a start was good
NONCE_RE = re.compile(r"^[0-9a-f]{8,32}$")
SUPPORTED_RELAY_TYPES = ("local-file", "http-file", "remote-hls", "rtmp", "rtmps", "rtsp", "youtube", "web-resolver")


def marker_path(relay):
    if not NAME_RE.match(relay or ""):
        raise ValidationError("invalid stream name")
    return os.path.join(store.studio_dir(), "overlay-%s.json" % relay)


def read_marker(relay):
    """{"project", "enabled", "nonce"} or None (also None for anything malformed: the relay then streams as before)."""
    import json
    try:
        path = marker_path(relay)
        if os.path.islink(path):
            return None
        with open(path, encoding="utf-8") as fh:
            doc = json.loads(fh.read(4096))
    except (OSError, ValueError, ValidationError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("project"), str) or not NAME_RE.match(doc["project"]):
        return None
    nonce = doc.get("nonce") if isinstance(doc.get("nonce"), str) and NONCE_RE.match(doc.get("nonce")) else ""
    return {"project": doc["project"], "enabled": doc.get("enabled") is True, "nonce": nonce}


def write_marker(relay, project, enabled):
    """Atomically (re)write the switch. A fresh nonce every time re-arms the circuit breaker."""
    import secrets
    if not NAME_RE.match(project or ""):
        raise ValidationError("invalid project name")
    store._atomic_json(marker_path(relay), {"project": project, "enabled": bool(enabled), "nonce": secrets.token_hex(8)})


def delete_marker(relay):
    try:
        os.unlink(marker_path(relay))
    except OSError:
        pass


# ---- circuit breaker (run dir: writable by the relay account) -----------------------------------------------------------
def _f(relay, suffix):
    return os.path.join(runtime.run_dir(), "studio-overlay-%s.%s" % (relay, suffix))


def tripped_reason(relay, nonce, now=None):
    try:
        with open(_f(relay, "tripped"), encoding="utf-8") as fh:
            n, _, reason = fh.read(300).partition("\n")
    except OSError:
        return ""
    return reason.strip() or "the compositor kept failing" if n.strip() == nonce else ""


def allow_start(relay, nonce, now=None, reason="start"):
    """Record one start attempt. -> (ok, reason). False when the breaker has tripped for THIS activation.
    Deliberate (re)builds (`start`, `swap`) are always allowed: quick successive edits must never switch overlays off. Only a `retry`
    (the previous compositor ended on its own) that follows a start which never proved healthy trips the breaker."""
    now = time.time() if now is None else now
    why = tripped_reason(relay, nonce)
    if why:
        return False, "overlays were switched off automatically: %s" % why
    path = _f(relay, "starts")
    recent = []
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh.read(4000).splitlines():
                t, _, n = ln.partition(" ")
                if n == nonce and now - float(t) <= WINDOW_S:
                    recent.append(float(t))
    except (OSError, ValueError):
        recent = []
    if reason == "retry" and len(recent) >= MAX_STARTS:
        reason = "it failed to stay up (restarted within %d s without a healthy run); the plain stream was restored" % WINDOW_S
        try:
            with open(_f(relay, "tripped"), "w", encoding="utf-8") as fh:
                fh.write("%s\n%s\n" % (nonce, reason))
        except OSError:
            pass
        return False, "overlays were switched off automatically: %s" % reason
    recent.append(now)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join("%.3f %s\n" % (t, nonce) for t in recent))
    except OSError:
        pass
    return True, ""


def mark_healthy(relay):
    """The compositor held HEALTHY output long enough: forget the recent starts."""
    for suffix in ("starts",):
        try:
            os.unlink(_f(relay, suffix))
        except OSError:
            pass


def state(relay):
    """Short state word for the dashboard: "" (no overlay switch), "off", "on", "tripped"."""
    m = read_marker(relay)
    if not m:
        return ""
    if not m["enabled"]:
        return "off"
    return "tripped" if tripped_reason(relay, m["nonce"]) else "on"
