"""Now Playing for playlist-backed Studio streams.

The Playlist engine plays ONE looping concat process, so it cannot name the
current item. We reconstruct it: the playlist wrapper writes
`<run>/<name>.concat.txt` (artifact order) and `<run>/<name>.titles`
(display titles, same order); the supervisor status file records when the
current FFmpeg attempt went LIVE. Playback is real-time (`-re`), so
`position = (now - live_since) mod total_duration` locates the item.

Honest limits: accuracy is about a second or two (start-up latency), and it
resets correctly on every restart because the supervisor rewrites LIVE_SINCE.
If durations/titles/status are unavailable the result is "" (never a guess).
"""
import os
import re
import subprocess
import time

_FILE_RE = re.compile(r"^file '(/[^']+)'$")
_dur_cache = {}


def locate(durations, elapsed):
    """-> (index, seconds_into_item) or None. Pure function."""
    if not durations or any(d <= 0 for d in durations):
        return None
    total = sum(durations)
    pos = elapsed % total
    for i, d in enumerate(durations):
        if pos < d:
            return i, pos
        pos -= d
    return len(durations) - 1, durations[-1]


def parse_concat(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [m.group(1) for m in (_FILE_RE.match(l.strip()) for l in fh) if m]
    except OSError:
        return []


def read_titles(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [l.rstrip("\n")[:120] for l in fh]
    except OSError:
        return []


def _duration(path, timeout=10):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_size, int(st.st_mtime))
    if key in _dur_cache:
        return _dur_cache[key]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=timeout).stdout.strip()
        val = float(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    _dur_cache[key] = val
    return val


def live_since(run_dir, playlist):
    """(started_epoch, state) from the supervisor status file, or (None, state)."""
    try:
        with open(os.path.join(run_dir, "playlist-%s.status" % playlist), encoding="utf-8") as fh:
            kv = dict(l.rstrip("\n").split("=", 1) for l in fh if "=" in l)
    except OSError:
        return None, "UNKNOWN"
    state = kv.get("STATE", "UNKNOWN")
    try:
        return int(kv.get("UPDATED", "")), state
    except ValueError:
        return None, state


def current_title(run_dir, playlist, now=None, duration_fn=_duration):
    """Title of the item playing right now, or "" when it cannot be known."""
    now = time.time() if now is None else now
    since, state = live_since(run_dir, playlist)
    if since is None or state != "LIVE":
        return ""
    files = parse_concat(os.path.join(run_dir, playlist + ".concat.txt"))
    titles = read_titles(os.path.join(run_dir, playlist + ".titles"))
    if not files or len(titles) != len(files):
        return ""
    durs = [duration_fn(f) for f in files]
    if any(d is None for d in durs):
        return ""
    hit = locate(durs, max(0.0, now - since))
    return titles[hit[0]] if hit else ""
