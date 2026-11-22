"""What this server can really do, so Studio never offers (or silently accepts) an output it cannot sustain.

Read-only and cheap: CPU count, load average, memory, and a one-off look at FFmpeg's encoder list
(cached). Nothing here starts a stream or changes the host.
"""
import os
import shutil
import subprocess
import time

from .model import RESOLUTIONS

# Cores one 720p30 `veryfast` live composition needs (measured guidance in docs: 1.5-2.5 with overlays).
BASE_CORES_720P30 = 1.5
PRESET_FACTOR = {"ultrafast": 0.6, "superfast": 0.75, "veryfast": 1.0, "faster": 1.3}
_cache = {"at": 0.0, "enc": None}


def cpu_count():
    return os.cpu_count() or 1


def load_avg():
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def mem_mb():
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _ffmpeg_encoders():
    """Set of FFmpeg encoder names, or None when FFmpeg cannot be run by the console user."""
    now = time.monotonic()
    if _cache["enc"] is not None and now - _cache["at"] < 300:
        return _cache["enc"] or None
    exe = shutil.which("ffmpeg")
    names = set()
    if exe:
        try:
            out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 timeout=8, check=False).stdout
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 2 and parts[0].startswith("V"):
                    names.add(parts[1])
        except (OSError, subprocess.TimeoutExpired):
            pass
    _cache.update(at=now, enc=names)
    return names or None


def gpu_present():
    return os.path.exists("/dev/nvidiactl") or shutil.which("nvidia-smi") is not None


def encoders():
    """Encoders Studio can use. Only software x264 is wired into the FFmpeg pipeline; a GPU encoder is
    reported honestly as unavailable, never advertised."""
    found = _ffmpeg_encoders()
    x264_ok = found is None or "libx264" in found      # unknown (no ffmpeg for this user) -> do not block
    nv_found = bool(found and "h264_nvenc" in found)
    if nv_found and gpu_present():
        nv_why = "An NVIDIA encoder is present, but BlueStream Studio's pipeline does not use it yet."
    elif gpu_present():
        nv_why = "An NVIDIA GPU was found, but this FFmpeg build has no NVENC encoder."
    else:
        nv_why = "No NVIDIA GPU was detected on this server."
    return [
        {"id": "libx264", "label": "Software (x264)", "available": x264_ok, "selectable": x264_ok,
         "why": "" if x264_ok else "This FFmpeg build has no libx264 encoder."},
        {"id": "h264_nvenc", "label": "Hardware (NVENC)", "available": False, "selectable": False, "why": nv_why},
    ]


def estimate_cores(profile):
    ratio = (profile.width * profile.height * profile.fps) / float(1280 * 720 * 30)
    return round(BASE_CORES_720P30 * ratio * PRESET_FACTOR.get(profile.preset, 1.0), 2)


def assess(profile, cores=None):
    """-> {"level": ok|warn|block, "need": float, "cores": int, "message": str}.
    `block` means the output cannot fit this machine at all; `warn` means it will crowd other streams."""
    cores = cores or cpu_count()
    need = estimate_cores(profile)
    res = "%dx%d at %d fps" % (profile.width, profile.height, profile.fps)
    if need > cores * 0.85:
        level, msg = "block", ("%s needs about %.1f CPU cores for a live encode but this server has %d. "
                               "Choose a lower resolution or frame rate." % (res, need, cores))
    elif need > cores * 0.5:
        level, msg = "warn", ("%s needs about %.1f of this server's %d CPU cores while a live composition runs, "
                              "which can crowd other streams." % (res, need, cores))
    else:
        level, msg = "ok", "%s needs about %.1f of %d CPU cores." % (res, need, cores)
    return {"level": level, "need": need, "cores": cores, "message": msg}


def summary(profile=None):
    cores = cpu_count()
    doc = {"cores": cores, "load": load_avg(), "mem_mb": mem_mb(), "encoders": encoders(),
           "resolutions": ["%dx%d" % r for r in RESOLUTIONS]}
    if profile is not None:
        doc["assessment"] = assess(profile, cores)
    return doc
