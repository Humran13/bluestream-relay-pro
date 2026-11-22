"""Media browser for Studio: list, classify, search, and accept uploads of video, image and audio files.

Everything goes through the EXISTING Media Library: listing uses the engine's media_list, and uploads
are staged in the web upload directory and handed to the engine's root-owned, quarantined
media_import_staged (ffprobe gate, no overwrite). The browser never names a path: a file name is
reduced to a safe basename and checked against an extension allow-list, so there is no arbitrary
filesystem access.
"""
import os
import re

from .model import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

# What the engine's importer accepts for video (kept in step with webapp.engine.ALLOWED_UPLOAD_EXTENSIONS).
UPLOAD_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts")
UPLOAD_EXTS = UPLOAD_VIDEO_EXTS + IMAGE_EXTS + AUDIO_EXTS
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # same cap the preview/asset path enforces
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def classify(name):
    ext = os.path.splitext(name or "")[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "other"


def sanitize(filename):
    """Safe managed-media basename for an upload, or None (fail closed). Mirrors the Media page's
    normalisation ("My Logo.PNG" -> "my-logo.png") but with Studio's wider extension allow-list."""
    if not filename or ".." in filename:
        return None
    base = filename.replace("\\", "/").split("/")[-1]
    if not base or base.startswith(".") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in base):
        return None
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in UPLOAD_EXTS:
        return None
    stem = "".join(c if c in _SAFE else "-" for c in stem.lower())
    stem = re.sub(r"-{2,}", "-", stem).strip("-_.")
    if not stem:
        return None
    out = stem + ext
    return out if len(out) <= 255 else None


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def listing(items, query="", kind=""):
    """Engine media_list rows -> [{name, kind, ext, size, size_text}], filtered and sorted by name."""
    q = (query or "").strip().lower()[:80]
    out = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name or name.startswith("."):
            continue
        k = classify(name)
        if k == "other":
            continue
        if kind and k != kind:
            continue
        if q and q not in name.lower():
            continue
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"name": name, "kind": k, "ext": os.path.splitext(name)[1].lstrip(".").upper(),
                    "size": size, "size_text": human_size(size)})
    out.sort(key=lambda r: r["name"])
    return out
