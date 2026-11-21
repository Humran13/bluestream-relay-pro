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
    if tz == "UTC":
        return timezone.utc
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


def format_countup(anchor, tz, now_utc):
    secs = max(0, int((now_utc - parse_target(anchor, tz)).total_seconds()))
    days, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return ("%dd %02d:%02d:%02d" % (days, h, m, s)) if days else ("%02d:%02d:%02d" % (h, m, s))


def format_value(raw, fmt):
    """Presentation of a fetched value. Non-numeric input is returned unchanged."""
    if fmt == "raw":
        return raw
    try:
        num = float(str(raw).replace(",", ""))
    except ValueError:
        return raw
    if fmt == "int":
        return "%d" % round(num)
    if fmt == "thousands":
        return "{:,}".format(int(round(num)))
    if fmt == "decimal1":
        return "%.1f" % num
    if fmt == "percent":
        return "%.1f%%" % num
    return raw


def render_text(overlay, now_utc, now_playing="", data_value=""):
    """Current text for any text-backed overlay at `now_utc` (pure function)."""
    k = overlay.kind
    if k in ("text", "ticker"):
        return overlay.text
    if k == "clock":
        return format_clock(overlay.tz, now_utc)
    if k == "countdown":
        return format_countdown(overlay.target, overlay.tz, now_utc, overlay.finished_text)
    if k == "countup":
        return format_countup(overlay.target, overlay.tz, now_utc)
    if k == "pomodoro":
        label, left = pomodoro_phase(parse_target(overlay.target, overlay.tz), now_utc,
                                     overlay.work_min, overlay.break_min)
        return "%s %02d:%02d" % (label, left // 60, left % 60)
    if k == "data":
        return (overlay.text or "{value}").replace(
            "{value}", format_value(data_value, overlay.value_format) if data_value else "--")
    if k == "nowplaying":
        return (overlay.text + " " if overlay.text else "") + now_playing
    return ""
