"""Real source preview frames and thumbnails for the editor, generated with a bounded,
argv-only FFmpeg call.

Where the bytes come from (permissions matter):
  * In the hardened production layout the console user (bluestream-web) can NOT read
    media (root:bluestream-relay 0640 in a 0750 dir) or HLS output (www-data 0750), and
    we deliberately do not widen that. Instead the console calls two read-only
    web-ctl operations (studio_frame / studio_thumb); web-ctl runs `studio.cli frame|thumb`
    as root, which immediately drops to the unprivileged account that legitimately owns
    the data (see choose_identity) before FFmpeg touches a single byte.
  * On dev boxes (or any host where the console user can read the files) the direct path
    below works without the bridge.
Anything unavailable yields None and the editor shows a neutral background - never a
fabricated image.
"""
import os
import re
import subprocess
import time

from .model import ASSET_RE

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".avi")
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
_cache = {}  # (path, mtime, size, kind) -> (expires, bytes)


def hls_root():
    return os.environ.get("BLUESTREAM_HLS_ROOT", "/var/www/bluestream/hls")


def _media_dir():
    return os.environ.get("BLUESTREAM_MEDIA_DIR", "/var/lib/bluestream/media")


def safe_media_path(name):
    """Plain file name inside the media dir, else None."""
    if not isinstance(name, str) or not ASSET_RE.match(name) or "/" in name or ".." in name:
        return None
    path = os.path.join(_media_dir(), name)
    return path if os.path.isfile(path) and not os.path.islink(path) else None


def asset_image(name):
    """(path, mimetype) for a Media Library IMAGE, else None."""
    path = safe_media_path(name)
    mime = IMAGE_EXT.get(os.path.splitext(name or "")[1].lower())
    if not path or not mime or os.path.getsize(path) > MAX_ASSET_BYTES:
        return None
    return path, mime


def _newest_segment(directory):
    try:
        segs = [os.path.join(directory, f) for f in os.listdir(directory)
                if f.endswith(".ts") and not os.path.islink(os.path.join(directory, f))]
    except OSError:
        return None
    return max(segs, key=os.path.getmtime) if segs else None


def source_path(project):
    k, ref = project.source_kind, project.source_ref
    if k == "media":
        return safe_media_path(ref)
    if k == "slideshow":
        names = project.slides()
        return safe_media_path(names[0]) if names else None
    if k == "audio":
        bg = project.background
        return safe_media_path(bg) if not bg.startswith("#") else None
    if k in ("relay", "playlist") and re.match(r"^[a-z0-9_-]{1,48}$", ref):
        return _newest_segment(os.path.join(hls_root(), k, ref))
    return None


# ---------------------------------------------------------------------------
# privilege drop (root -> the account that owns the data)
# ---------------------------------------------------------------------------
def choose_identity(path, hls=None, media=None):
    """Which service account should read `path`? -> ("nginx"|"relay") or None (unknown path).
    HLS output belongs to the nginx user; managed media to the relay account."""
    hls, media = hls or hls_root(), media or _media_dir()
    real = os.path.realpath(path)
    if real.startswith(os.path.realpath(hls) + os.sep):
        return "nginx"
    if real.startswith(os.path.realpath(media) + os.sep):
        return "relay"
    return None


def _ids(role):
    import pwd
    if role == "relay":
        names = [os.environ.get("BLUESTREAM_USER", "bluestream-relay")]
    else:
        names = [os.environ.get("BLUESTREAM_NGINX_USER", ""), "www-data", "nginx"]
    for n in names:
        if not n:
            continue
        try:
            pw = pwd.getpwnam(n)
            return pw.pw_uid, pw.pw_gid, n
        except KeyError:
            continue
    return None


def _dropper(role):
    """preexec_fn that permanently drops root to `role`'s account, or None when not root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    ident = _ids(role)
    if ident is None:
        raise PermissionError("service account for %s not found; refusing to run as root" % role)
    uid, gid, name = ident

    def drop():
        os.setgroups([])
        os.initgroups(name, gid)
        os.setgid(gid)
        os.setuid(uid)
    return drop


def _run(cmd, path, timeout):
    role = choose_identity(path)
    kw = {}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if role is None:
            raise PermissionError("refusing to read an unmanaged path as root")
        kw["preexec_fn"] = _dropper(role)
    return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw).stdout


def frame_jpeg(project, ffmpeg="ffmpeg", timeout=10, width=960, now=None):
    path = source_path(project)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime), st.st_size, "frame")
    now = time.monotonic() if now is None else now
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    is_still = os.path.splitext(path)[1].lower() in IMAGE_EXT
    for seek in ([] if is_still else ["-ss", "1"], []):
        cmd = [ffmpeg, "-v", "error", "-nostdin"] + seek + ["-i", path, "-frames:v", "1",
               "-vf", "scale=%d:-2" % width, "-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe", "pipe:1"]
        try:
            out = _run(cmd, path, timeout)
        except (OSError, subprocess.TimeoutExpired, PermissionError):
            return None
        if out[:2] == b"\xff\xd8" and len(out) <= MAX_PREVIEW_BYTES:
            if len(_cache) > 32:
                _cache.clear()
            _cache[key] = (now + 30, out)
            return out
    return None


def thumb_png(name, ffmpeg="ffmpeg", timeout=10, max_width=960):
    """Scaled PNG (alpha preserved) of a Media Library image for overlay previews."""
    found = asset_image(name)
    seeks = [[]]
    if not found:
        # A video from the Media Library: one small still for the media browser (first second, else frame 0).
        path = safe_media_path(name)
        if not path or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
            return None
        found, seeks, max_width = (path, None), [["-ss", "1"], []], min(max_width, 480)
    path = found[0]
    for seek in seeks:
        cmd = [ffmpeg, "-v", "error", "-nostdin"] + seek + ["-i", path, "-frames:v", "1",
               "-vf", "scale='min(%d,iw)':-2" % max_width, "-c:v", "png", "-f", "image2pipe", "pipe:1"]
        try:
            out = _run(cmd, path, timeout)
        except (OSError, subprocess.TimeoutExpired, PermissionError):
            return None
        if out[:8] == b"\x89PNG\r\n\x1a\n" and len(out) <= MAX_PREVIEW_BYTES:
            return out
    return None


# ---------------------------------------------------------------------------
# On-demand video preview for the editor (never touches a broadcast)
# ---------------------------------------------------------------------------
def probe_codecs(path, timeout=10):
    """{"video": codec, "pix_fmt": fmt, "audio": codec|None} via ffprobe (privilege-dropped), or None."""
    import json
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt", "-of", "json", path]
    try:
        doc = json.loads(_run(cmd, path, timeout) or b"{}")
    except (OSError, subprocess.TimeoutExpired, PermissionError, ValueError):
        return None
    out = {"video": None, "pix_fmt": None, "audio": None}
    for st in doc.get("streams", []):
        if st.get("codec_type") == "video" and out["video"] is None:
            out["video"], out["pix_fmt"] = st.get("codec_name"), st.get("pix_fmt")
        elif st.get("codec_type") == "audio" and out["audio"] is None:
            out["audio"] = st.get("codec_name")
    return out if out["video"] else None


def proxy_mode(codecs):
    """"copy" when a browser can play the streams as they are (H.264 yuv420p + AAC/MP3/none), else "transcode"."""
    ok = codecs and codecs["video"] == "h264" and codecs["pix_fmt"] == "yuv420p" and codecs["audio"] in (None, "aac", "mp3")
    return "copy" if ok else "transcode"


def proxy_args(path, mode, ffmpeg="ffmpeg"):
    """FFmpeg argv producing a progressive-playable FRAGMENTED MP4 on stdout. `copy` costs almost nothing; `transcode`
    is a single 540p ultrafast encode that runs ONLY while a browser is watching."""
    base = [ffmpeg, "-v", "error", "-nostdin", "-i", path]
    if mode == "copy":
        media = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    else:
        media = ["-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:540", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k"]
    return base + media + ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]


def open_proxy(name):
    """Start the preview process for Media Library video `name`; returns (Popen, mode) or None when unreadable/unsupported."""
    path = safe_media_path(name)
    if not path or os.path.splitext(name)[1].lower() not in VIDEO_EXT:
        return None
    mode = proxy_mode(probe_codecs(path))
    role = choose_identity(path)
    drop = None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if role is None:
            return None
        drop = _dropper(role)

    def pre():
        if drop:
            drop()
        try:
            os.nice(10 if mode == "transcode" else 5)
        except (AttributeError, OSError):
            pass
    kw = {"preexec_fn": pre} if hasattr(os, "fork") else {}
    try:
        proc = subprocess.Popen(proxy_args(path, mode), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        return None
    return proc, mode
