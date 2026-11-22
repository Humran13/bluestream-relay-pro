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


def gpu_info():
    """{"name", "driver"} from nvidia-smi, or None. Informational only."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=6, check=False).stdout.strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    parts = [x.strip() for x in out[0].split(",")]
    return {"name": parts[0][:80], "driver": parts[1][:40] if len(parts) > 1 else ""}


_NV = {"at": 0.0, "res": None}
NVENC_REASONS = (
    ("Cannot load libcuda", "The NVIDIA driver library (libcuda) is not available to FFmpeg."),
    ("Cannot load libnvidia-encode", "The NVIDIA encoder library (libnvidia-encode) is not available to FFmpeg."),
    ("No NVENC capable devices", "No NVENC-capable NVIDIA GPU was found."),
    ("Driver does not support the required nvenc API", "The installed NVIDIA driver is too old for this FFmpeg's NVENC."),
    ("OpenEncodeSessionEx failed", "The GPU refused to open an encoder session (limit reached or not permitted)."),
    ("Unknown encoder", "This FFmpeg build has no h264_nvenc encoder."),
    ("Permission denied", "The dashboard's service user may not access the GPU device."),
)


def probe_nvenc(force=False, runner=None):
    """-> (usable, reason). A REAL two-frame NVENC encode on a tiny synthetic video: listing h264_nvenc in
    `ffmpeg -encoders` proves nothing. Cached (5 min on success, 1 min on failure); never raises."""
    now = time.monotonic()
    ttl = 300 if (_NV["res"] and _NV["res"][0]) else 60
    if not force and _NV["res"] is not None and now - _NV["at"] < ttl:
        return _NV["res"]
    exe = shutil.which("ffmpeg")
    if not exe:
        res = (False, "FFmpeg is not available to the dashboard.")
    else:
        run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False))
        argv = [exe, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:r=5:d=1",
                "-frames:v", "2", "-c:v", "h264_nvenc", "-f", "null", "-"]
        try:
            r = run(argv)
            if r.returncode == 0:
                res = (True, "NVENC initialised and encoded a test frame.")
            else:
                msg = (r.stderr or "")
                why = next((t for k, t in NVENC_REASONS if k.lower() in msg.lower()), "NVENC could not be initialised on this server.")
                res = (False, why)
        except (OSError, subprocess.TimeoutExpired):
            res = (False, "The NVENC test did not finish (timed out).")
    _NV.update(at=now, res=res)
    return res


def encoders(force=False, probe=True):
    """Encoders the pipeline can use RIGHT NOW. NVENC is selectable only after the real initialisation test passed.
    probe=False never runs the test (page loads stay instant): an untested NVENC is reported as such, not as usable."""
    found = _ffmpeg_encoders()
    x264_ok = found is None or "libx264" in found      # unknown (no ffmpeg for this user) -> do not block
    untested = (not probe) and _NV["res"] is None
    if untested:
        nv_ok, nv_why = False, "Not tested yet. Press 'Test NVIDIA encoding'."
    else:
        nv_ok, nv_why = probe_nvenc(force)
    gpu = gpu_info()
    return [
        {"id": "libx264", "label": "Software H.264 (libx264)", "available": x264_ok, "selectable": x264_ok,
         "why": "" if x264_ok else "This FFmpeg build has no libx264 encoder."},
        {"id": "h264_nvenc", "label": "NVIDIA H.264 (h264_nvenc)", "available": nv_ok, "selectable": nv_ok,
         "why": nv_why if not nv_ok else "Verified by a real test encode" + ((" on " + gpu["name"]) if gpu else "") + ".",
         "gpu": gpu, "untested": untested},
    ]


def encoder_usable(encoder):
    """(ok, reason) for saving/starting with `encoder`."""
    if encoder == "libx264":
        return True, ""
    if encoder == "h264_nvenc":
        return probe_nvenc()
    return False, "unknown encoder"


def estimate_cores(profile):
    ratio = (profile.width * profile.height * profile.fps) / float(1280 * 720 * 30)
    if getattr(profile, "encoder", "libx264") == "h264_nvenc":
        return round(0.4 * ratio + 0.2, 2)               # filters/composition still run on the CPU; the encode does not
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


def summary(profile=None, probe=False):
    cores = cpu_count()
    doc = {"cores": cores, "load": load_avg(), "mem_mb": mem_mb(), "encoders": encoders(force=probe, probe=probe),
           "resolutions": ["%dx%d" % r for r in RESOLUTIONS]}
    if profile is not None:
        doc["assessment"] = assess(profile, cores)
    return doc
