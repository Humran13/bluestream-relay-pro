"""Real source preview frames for the editor (JPEG), generated with a bounded,
argv-only FFmpeg call on a file the console already manages.

Sources: media file (frame at ~1 s), slideshow (first image), relay/playlist
(newest HLS segment on disk). Anything unavailable yields None and the editor
shows a neutral background - never a fabricated image.
"""
import os
import re
import subprocess
import time

from .model import ASSET_RE

IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}
MAX_ASSET_BYTES = 10 * 1024 * 1024
_cache = {}  # (path, mtime, size) -> (expires, bytes)


def hls_root():
    return os.environ.get("BLUESTREAM_HLS_ROOT", "/var/www/bluestream/hls")


def _media_dir():
    return os.environ.get("BLUESTREAM_MEDIA_DIR", "/var/lib/bluestream/media")


def safe_media_path(name):
    """Plain file name inside the media dir, else None."""
    if not isinstance(name, str) or not ASSET_RE.match(name) or "/" in name or ".." in name:
        return None
    path = os.path.join(_media_dir(), name)
    return path if os.path.isfile(path) else None


def asset_image(name):
    """(path, mimetype) for a Media Library IMAGE, else None."""
    path = safe_media_path(name)
    mime = IMAGE_EXT.get(os.path.splitext(name or "")[1].lower())
    if not path or not mime or os.path.getsize(path) > MAX_ASSET_BYTES:
        return None
    return path, mime


def _newest_segment(directory):
    try:
        segs = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".ts")]
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
    if k in ("relay", "playlist") and re.match(r"^[a-z0-9_-]{1,48}$", ref):
        return _newest_segment(os.path.join(hls_root(), k, ref))
    return None


def frame_jpeg(project, ffmpeg="ffmpeg", timeout=10, width=960, now=None):
    path = source_path(project)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(st.st_mtime), st.st_size)
    now = time.monotonic() if now is None else now
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    is_still = os.path.splitext(path)[1].lower() in IMAGE_EXT
    for seek in ([] if is_still else ["-ss", "1"], []):
        cmd = [ffmpeg, "-v", "error", "-nostdin"] + seek + ["-i", path, "-frames:v", "1",
               "-vf", "scale=%d:-2" % width, "-c:v", "mjpeg", "-q:v", "5", "-f", "image2pipe", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out[:2] == b"\xff\xd8":
            if len(_cache) > 32:
                _cache.clear()
            _cache[key] = (now + 30, out)
            return out
    return None
