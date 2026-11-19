"""Live text widgets rendered to small files that FFmpeg's drawtext reloads.

State is an absolute deadline/timezone, never a browser or in-memory counter,
so timers survive logout, browser close and process restarts.
"""
import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import ValidationError


def _zone(tz):
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValidationError("unknown timezone")


def parse_target(target, tz):
    """Absolute deadline. Naive strings are interpreted in `tz` (DST-correct)."""
    try:
        dt = datetime.fromisoformat(target)
    except ValueError:
        raise ValidationError("invalid countdown target")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz))
    return dt.astimezone(timezone.utc)


def format_countdown(target, tz, now_utc, finished_text="LIVE NOW"):
    remaining = int((parse_target(target, tz) - now_utc).total_seconds())
    if remaining <= 0:
        return finished_text
    days, rem = divmod(remaining, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return ("%dd %02d:%02d:%02d" % (days, h, m, s)) if days else ("%02d:%02d:%02d" % (h, m, s))


def format_clock(tz, now_utc):
    return now_utc.astimezone(_zone(tz)).strftime("%H:%M:%S")


def pomodoro_phase(anchor_utc, now_utc, work_min=25, break_min=5):
    """Stateless Pomodoro: phase derives from an absolute anchor, so restarts
    resume in the right phase. Returns (label, seconds_left)."""
    cycle = (work_min + break_min) * 60
    pos = int((now_utc - anchor_utc).total_seconds()) % cycle
    if pos < work_min * 60:
        return "FOCUS", work_min * 60 - pos
    return "BREAK", cycle - pos


def atomic_write(path, text):
    """Replace the file atomically so drawtext never reads a partial write."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".w")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
