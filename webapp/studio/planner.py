"""Deterministic output planner: pick the cheapest valid pipeline and say why."""
import hashlib
from dataclasses import dataclass, field

DIRECT = "direct-copy"
PRERENDER = "prerender-then-copy"
LIVE = "live-composition"

MAX_PRERENDER_SECONDS = 6 * 3600
MAX_GOP_SECONDS = 4.0


@dataclass(frozen=True)
class SourceInfo:
    """Facts from ffprobe/relay state. Unknown values must be None, never guessed."""
    vcodec: str = None
    pix_fmt: str = None
    width: int = None
    height: int = None
    fps: float = None
    acodec: str = None
    gop_seconds: float = None
    progressive: bool = True
    live: bool = False
    duration: float = None  # seconds; None for live/unknown
    content_id: str = ""    # stable identity (hash or path+size+mtime)


@dataclass(frozen=True)
class Plan:
    path: str
    reasons: tuple
    cache_key: str = ""
    encodes: bool = True
    notes: tuple = field(default_factory=tuple)


def copy_incompatibilities(src, profile):
    """Why this source cannot be stream-copied to the output profile."""
    why = []
    if src.vcodec != "h264":
        why.append("video codec is %s, need h264" % src.vcodec)
    if src.pix_fmt != "yuv420p":
        why.append("pixel format is %s, need yuv420p" % src.pix_fmt)
    if (src.width, src.height) != (profile.width, profile.height):
        why.append("resolution %sx%s differs from %sx%s"
                   % (src.width, src.height, profile.width, profile.height))
    if src.fps is None or abs(src.fps - profile.fps) > 0.5:
        why.append("frame rate %s differs from %s" % (src.fps, profile.fps))
    if src.acodec != "aac":
        why.append("audio is %s, need aac" % src.acodec)
    if src.gop_seconds is None or src.gop_seconds > MAX_GOP_SECONDS:
        why.append("keyframe interval unknown or above %.0fs" % MAX_GOP_SECONDS)
    if not src.progressive:
        why.append("interlaced video")
    return why


def cache_key(project, src):
    raw = "%s|%s" % (src.content_id, project.overlay_fingerprint())
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def plan_output(project, src):
    profile = project.profile
    overlays = project.visible_overlays()
    dynamic = [o for o in overlays if o.dynamic]
    incompat = copy_incompatibilities(src, profile)

    if dynamic:
        kinds = sorted({o.kind for o in dynamic})
        return Plan(LIVE, ("changing overlays (%s) require one live encode" % ", ".join(kinds),))
    if src.live:
        if not overlays and not incompat:
            return Plan(DIRECT, ("live source already compatible",), encodes=False)
        return Plan(LIVE, ("live source cannot be pre-rendered",) + tuple(incompat))
    if not overlays and not incompat:
        return Plan(DIRECT, ("source is stream-copy compatible, no overlays",), encodes=False)

    # Finite source with static overlays and/or normalisation needs.
    if src.duration is None or src.duration > MAX_PRERENDER_SECONDS:
        return Plan(LIVE, ("source duration unknown or too long to pre-render",) + tuple(incompat))
    reasons = []
    if overlays:
        reasons.append("static overlays rendered once then stream-copied")
    reasons.extend(incompat)
    return Plan(PRERENDER, tuple(reasons), cache_key=cache_key(project, src),
                encodes=False, notes=("one-time encode while building cache",))


def admit(plan, running_live_encodes, max_live_encodes=1, cpu_cores=4, load_avg=0.0):
    """Admission control. Returns (ok, reason). Never silently degrades."""
    if plan.path != LIVE:
        return True, "no sustained encode needed"
    if running_live_encodes >= max_live_encodes:
        return False, "live encode limit reached (%d)" % max_live_encodes
    if load_avg > cpu_cores * 0.75:
        return False, "host load %.1f too high for another encode" % load_avg
    return True, "within live-encode budget"
