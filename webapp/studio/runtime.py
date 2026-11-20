"""Runtime helpers: ffprobe, source resolution, pre-render cache, live-slot
leases, and FFmpeg -progress health parsing. No network access except local
ffprobe of the private loopback bus."""
import json
import os
import subprocess
import time

from . import planner

BUS = "rtmp://127.0.0.1:1935"
APP_RELAY = "bluestream-relay"
APP_PLAYLIST = "bluestream-playlist"
DEFAULT_RUN = "/var/lib/bluestream/run"
DEFAULT_CACHE = "/var/lib/bluestream/run/studio-cache"  # inside the relay unit's existing ReadWritePaths
DEFAULT_MEDIA = "/var/lib/bluestream/media"
CACHE_LIMIT_BYTES = 5 * 1024 ** 3


def run_dir():
    return os.environ.get("BLUESTREAM_RUN_DIR", DEFAULT_RUN)


def cache_dir():
    return os.environ.get("BLUESTREAM_STUDIO_CACHE", DEFAULT_CACHE)


def media_dir():
    return os.environ.get("BLUESTREAM_MEDIA_DIR", DEFAULT_MEDIA)


def resolve_source(project):
    """-> (input string for FFmpeg, is_live). Raises ValueError if unavailable."""
    k, ref = project.source_kind, project.source_ref
    if k == "slideshow":
        return ensure_slideshow(project), False
    if k == "audio":
        path = os.path.join(media_dir(), os.path.basename(ref))
        if os.path.basename(ref) != ref or not os.path.isfile(path):
            raise ValueError("audio file not found: %s" % os.path.basename(ref))
        return path, False
    if k == "relay":
        return "%s/%s/%s" % (BUS, APP_RELAY, ref), True
    if k == "playlist":
        return "%s/%s/%s" % (BUS, APP_PLAYLIST, ref), True
    base = os.path.basename(ref)
    path = os.path.join(media_dir(), base)
    if base != ref or not os.path.isfile(path):
        raise ValueError("media file not found: %s" % base)
    return path, False


def slideshow_order(project):
    names = project.slides()
    if project.slide_shuffle:
        import random
        random.Random(",".join(names)).shuffle(names)  # deterministic: same list -> same order
    return names


def ensure_slideshow(project):
    """Render (once, cached) the slideshow clip and return its path."""
    import hashlib
    from . import ffmpeg as sff
    names = slideshow_order(project)
    paths = []
    ident = [str(project.slide_seconds), str(project.profile.__dict__)]
    for n in names:
        path = os.path.join(media_dir(), n)
        if not os.path.isfile(path):
            raise ValueError("slideshow image not found: %s" % n)
        st = os.stat(path)
        ident.append("%s:%d:%d" % (n, st.st_size, int(st.st_mtime)))
        paths.append(path)
    final = os.path.join(cache_dir(), "ss-" + hashlib.sha256("|".join(ident).encode()).hexdigest()[:28] + ".mp4")
    if cache_valid(final):
        os.utime(final)
        return final
    os.makedirs(cache_dir(), exist_ok=True)
    tmp = final[:-4] + ".tmp"
    args = sff.build_slideshow_args(paths, project.slide_seconds, project.profile, tmp)
    r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not cache_valid(tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ValueError("slideshow render failed")
    os.replace(tmp, final)
    return final


def probe_source(path_or_url, live, timeout=15):
    """ffprobe -> SourceInfo. Unknown fields stay None (never guessed)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format"]
    if live:
        cmd += ["-rw_timeout", "10000000"]
    cmd.append(path_or_url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        doc = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return planner.SourceInfo(live=live)
    v = next((s for s in doc.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in doc.get("streams", []) if s.get("codec_type") == "audio"), {})
    fps = None
    try:
        n, d = v.get("r_frame_rate", "0/1").split("/")
        fps = float(n) / float(d) if float(d) else None
    except (ValueError, AttributeError):
        pass
    dur = None
    try:
        dur = float(doc.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    ident = ""
    if not live and os.path.isfile(path_or_url):
        st = os.stat(path_or_url)
        ident = "%s:%d:%d" % (os.path.basename(path_or_url), st.st_size, int(st.st_mtime))
    return planner.SourceInfo(
        vcodec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"), width=v.get("width"),
        height=v.get("height"), fps=fps, acodec=a.get("codec_name") or None,
        gop_seconds=None, progressive=v.get("field_order", "progressive") in ("progressive", "unknown"),
        live=live, duration=None if live else dur, content_id=ident)


# --------------------------------------------------------------------------
# pre-render cache
# --------------------------------------------------------------------------
def cache_path(key):
    return os.path.join(cache_dir(), key + ".mp4")


def cache_valid(path, min_bytes=1024):
    return os.path.isfile(path) and os.path.getsize(path) >= min_bytes


def evict_cache(limit=CACHE_LIMIT_BYTES, keep=()):
    """Oldest-used-first eviction until under `limit`; also removes stale temp files."""
    d = cache_dir()
    if not os.path.isdir(d):
        return []
    removed, files = [], []
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if f.endswith(".tmp") and time.time() - os.path.getmtime(p) > 3600:
            os.unlink(p)
            removed.append(f)
        elif f.endswith(".mp4"):
            files.append((os.path.getatime(p), p, os.path.getsize(p)))
    total = sum(s for _, _, s in files)
    for _, p, size in sorted(files):
        if total <= limit:
            break
        if os.path.basename(p) in keep:
            continue
        os.unlink(p)
        total -= size
        removed.append(os.path.basename(p))
    return removed


def disk_free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


# --------------------------------------------------------------------------
# live-slot leases (admission control across Studio runners)
# --------------------------------------------------------------------------
def _pid_alive(pid):
    if os.name == "nt":  # dev boxes only: os.kill(pid, 0) would TERMINATE on Windows
        return pid == os.getpid()
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running_live_encodes(exclude=""):
    d = run_dir()
    n = 0
    if not os.path.isdir(d):
        return 0
    for f in os.listdir(d):
        if f.startswith("studio-") and f.endswith(".live") and f != "studio-%s.live" % exclude:
            try:
                pid = int(open(os.path.join(d, f)).read().strip())
            except (OSError, ValueError):
                continue
            if _pid_alive(pid):
                n += 1
            else:
                try:
                    os.unlink(os.path.join(d, f))  # stale lease from a crash
                except OSError:
                    pass
    return n


def take_live_lease(name, pid=None):
    path = os.path.join(run_dir(), "studio-%s.live" % name)
    with open(path, "w") as fh:
        fh.write(str(pid or os.getpid()))
    return path


def release_live_lease(name):
    try:
        os.unlink(os.path.join(run_dir(), "studio-%s.live" % name))
    except OSError:
        pass


# --------------------------------------------------------------------------
# health from FFmpeg -progress output (frame age/speed/drops, not systemctl)
# --------------------------------------------------------------------------
def parse_progress(text):
    """Last complete -progress block -> dict (frame, fps, speed, drop_frames, out_time_us)."""
    last, cur = {}, {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur[k.strip()] = v.strip()
        if k.strip() == "progress":
            last, cur = dict(cur), {}
    out = {}
    for key in ("frame", "drop_frames", "dup_frames", "out_time_us"):
        try:
            out[key] = int(last.get(key, ""))
        except ValueError:
            pass
    for key in ("fps",):
        try:
            out[key] = float(last.get(key, ""))
        except ValueError:
            pass
    sp = last.get("speed", "").rstrip("x")
    try:
        out["speed"] = float(sp)
    except ValueError:
        pass
    return out


def health_verdict(prev, cur, wall_age_s, stall_s=20, min_speed=0.97):
    """-> (state, detail). `prev`/`cur` are parse_progress dicts from two samples."""
    if not cur:
        return "STARTING", "no progress output yet"
    if prev and cur.get("frame") == prev.get("frame") and wall_age_s >= stall_s:
        return "STALLED", "no new frames for %ds" % wall_age_s
    if cur.get("speed") is not None and cur["speed"] < min_speed:
        return "DEGRADED", "encode speed %.2fx is below real time" % cur["speed"]
    return "HEALTHY", "frames flowing"
