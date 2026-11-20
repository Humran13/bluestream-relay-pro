"""Real Studio health: derived from FFmpeg's own progress stream, the process
table and the supervisor status - never from "the process exists".

The Studio sidecar owns a loopback TCP listener; FFmpeg is started with
`-progress tcp://127.0.0.1:<port>` and streams key=value blocks to it. The
tracker keeps only the latest block (bounded memory) and a few counters, and
the sidecar publishes a small JSON document every couple of seconds.
"""
import json
import os
import socket
import threading
import time

STALL_SECONDS = 20
MIN_SPEED = 0.97
HEALTH_NAME = "studio-%s.health.json"
PORT_NAME = "studio-%s.progress.port"


def health_path(run_dir, relay):
    return os.path.join(run_dir, HEALTH_NAME % relay)


def port_path(run_dir, relay):
    return os.path.join(run_dir, PORT_NAME % relay)


class Tracker:
    """Thread-safe accumulator fed by the progress listener."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._lock = threading.Lock()
        self._clock, self._wall = clock, wall
        self.reset_totals()

    def reset_totals(self):
        with self._lock:
            self.connections = 0       # each FFmpeg (re)start connects once
            self.block = {}            # latest complete progress block
            self.last_frame = None
            self.last_frame_change = self._clock()
            self.started = self._clock()
            self.last_block_at = None
            self.max_drop = 0

    def on_connect(self):
        with self._lock:
            self.connections += 1
            self.last_frame = None
            self.last_frame_change = self._clock()
            self.block = {}

    def on_block(self, block):
        with self._lock:
            self.block = block
            self.last_block_at = self._clock()
            try:
                frame = int(block.get("frame", ""))
            except ValueError:
                return
            if frame != self.last_frame:
                self.last_frame = frame
                self.last_frame_change = self._clock()
            try:
                self.max_drop = max(self.max_drop, int(block.get("drop_frames", "0")))
            except ValueError:
                pass

    def snapshot(self, proc=None, supervisor=None):
        """-> JSON-able dict with an honest verdict."""
        with self._lock:
            now = self._clock()
            b = dict(self.block)
            stalled_for = int(now - self.last_frame_change)
            have = bool(b) and self.last_block_at is not None
            speed = _num(b.get("speed", "").rstrip("x"))
            fps = _num(b.get("fps"))
            restarts = max(0, self.connections - 1)
            conns = self.connections
            drop = self.max_drop
            frames = _int(b.get("frame"))
            out_s = (_int(b.get("out_time_us")) or 0) / 1e6 if b.get("out_time_us") else None
            block_age = None if self.last_block_at is None else int(now - self.last_block_at)
        sup = supervisor or {}
        if sup.get("state") in ("STOPPED", "COMPLETED"):
            state, detail = "STOPPED", "stream stopped"
        elif sup.get("state") == "FAILED":
            state, detail = "FAILED", sup.get("detail") or "supervisor gave up"
        elif conns == 0 or not have:
            state, detail = "STARTING", "waiting for the encoder to report progress"
        elif block_age is not None and block_age > STALL_SECONDS:
            state, detail = "STALLED", "no progress report for %ds" % block_age
        elif stalled_for >= STALL_SECONDS:
            state, detail = "STALLED", "no new frames for %ds" % stalled_for
        elif speed is not None and speed < MIN_SPEED:
            state, detail = "DEGRADED", "encoder speed %.2fx is below real time" % speed
        else:
            state, detail = "HEALTHY", "frames are flowing"
        doc = {
            "state": state, "detail": detail, "speed": speed, "fps": fps, "frames": frames,
            "dropped_frames": drop, "output_seconds": out_s,
            "restarts": restarts, "supervisor_attempt": _int(sup.get("attempt")),
            "last_failure": sup.get("detail") if sup.get("state") in ("RECONNECTING", "FAILED") else "",
            "updated": int(self._wall()),
        }
        doc.update(proc or {})
        return doc


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_stream(lines):
    """Yield complete progress blocks from an iterable of text lines."""
    cur = {}
    for line in lines:
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur[k] = v[:64]
        if k == "progress":
            yield cur
            cur = {}
        elif len(cur) > 64:  # a malformed/hostile stream cannot grow memory
            cur = {}


class ProgressServer(threading.Thread):
    """Loopback-only listener that feeds a Tracker. Handles reconnects forever."""

    def __init__(self, tracker):
        super().__init__(daemon=True)
        self.tracker = tracker
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.sock.settimeout(1.0)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.tracker.on_connect()
            conn.settimeout(30)
            try:
                with conn.makefile("r", encoding="utf-8", errors="replace") as fh:
                    for block in parse_stream(fh):
                        self.tracker.on_block(block)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# process stats (Linux /proc; returns {} elsewhere)
# ---------------------------------------------------------------------------
def find_ffmpeg_pid(out_url):
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    want = out_url.encode()
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % e, "rb") as fh:
                parts = fh.read().split(b"\0")
        except OSError:
            continue
        if parts and os.path.basename(parts[0]) == b"ffmpeg" and want in parts:
            return int(e)
    return None


class ProcSampler:
    def __init__(self):
        self.prev = None

    def sample(self, pid, now=None):
        now = time.monotonic() if now is None else now
        if not pid:
            self.prev = None
            return {"cpu_cores": None, "rss_mb": None}
        try:
            with open("/proc/%d/stat" % pid) as fh:
                f = fh.read().rsplit(")", 1)[1].split()
            ticks = (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
            rss = 0.0
            with open("/proc/%d/status" % pid) as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) / 1024.0
        except (OSError, ValueError, IndexError):
            self.prev = None
            return {"cpu_cores": None, "rss_mb": None}
        cores = None
        if self.prev and self.prev[0] == pid and now > self.prev[2]:
            cores = round(max(0.0, (ticks - self.prev[1]) / (now - self.prev[2])), 2)
        self.prev = (pid, ticks, now)
        return {"cpu_cores": cores, "rss_mb": round(rss, 1)}


def write_health(run_dir, relay, doc):
    path = health_path(run_dir, relay)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_health(run_dir, relay, max_age=30, now=None):
    """Latest health doc, or a STALE marker when the sidecar stopped updating."""
    now = time.time() if now is None else now
    try:
        with open(health_path(run_dir, relay), encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError
    except (OSError, ValueError):
        return {"state": "UNKNOWN", "detail": "no health data (stream not running?)"}
    if now - doc.get("updated", 0) > max_age:
        doc["state"], doc["detail"] = "STALE", "health data is %ds old" % int(now - doc.get("updated", 0))
    return doc
