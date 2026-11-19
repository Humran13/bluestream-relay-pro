"""Studio project model: validated, serialisable, resolution-independent.

Layout coordinates are fractions (0..1) of the canvas so projects survive a
future 1080p/4K profile change. Every user-supplied value is validated here;
nothing downstream interpolates raw strings into shell code.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

KINDS = ("text", "image", "rect", "clock", "countdown", "ticker")
# Kinds whose rendered output changes over time -> need live composition.
DYNAMIC_KINDS = frozenset({"clock", "countdown", "ticker"})
MAX_OVERLAYS = 24


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class OutputProfile:
    width: int = 1280
    height: int = 720
    fps: int = 30
    video_kbps: int = 4000
    audio_kbps: int = 128
    gop_seconds: int = 2
    preset: str = "veryfast"

    def validate(self):
        if (self.width, self.height) not in ((1280, 720), (1920, 1080)):
            raise ValidationError("unsupported resolution")
        if self.fps not in (24, 25, 30, 60):
            raise ValidationError("unsupported fps")
        if not 1000 <= self.video_kbps <= 12000:
            raise ValidationError("video bitrate out of range")
        if not 64 <= self.audio_kbps <= 320:
            raise ValidationError("audio bitrate out of range")
        if self.preset not in ("ultrafast", "superfast", "veryfast", "faster"):
            raise ValidationError("unsupported preset")
        return self

    @property
    def gop(self):
        return self.fps * self.gop_seconds

    def monthly_terabytes(self, days=30):
        bits = (self.video_kbps + self.audio_kbps) * 1000 * 86400 * days
        return round(bits / 8 / 1e12, 2)


@dataclass(frozen=True)
class Overlay:
    kind: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.2
    h: float = 0.1
    opacity: float = 1.0
    visible: bool = True
    text: str = ""
    asset: str = ""
    color: str = "#ffffff"
    font: str = "DejaVu Sans"
    size: int = 48
    tz: str = "UTC"
    target: str = ""  # ISO-8601 absolute deadline for countdown

    @property
    def dynamic(self):
        return self.kind in DYNAMIC_KINDS

    def validate(self):
        if self.kind not in KINDS:
            raise ValidationError("unknown overlay kind")
        for v in (self.x, self.y, self.w, self.h):
            if not 0.0 <= v <= 1.0:
                raise ValidationError("layout outside canvas")
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValidationError("overlay exceeds canvas bounds")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValidationError("opacity out of range")
        if not COLOR_RE.match(self.color):
            raise ValidationError("invalid colour")
        if not FONT_RE.match(self.font):
            raise ValidationError("invalid font name")
        if not 8 <= self.size <= 400:
            raise ValidationError("font size out of range")
        if len(self.text) > 500:
            raise ValidationError("text too long")
        if self.kind == "image" and not ASSET_RE.match(self.asset):
            raise ValidationError("invalid asset name")
        if self.kind == "countdown" and not self.target:
            raise ValidationError("countdown needs a target")
        return self


@dataclass
class Project:
    name: str
    source_kind: str  # "relay" | "playlist" | "media"
    source_ref: str
    overlays: list = field(default_factory=list)
    profile: OutputProfile = field(default_factory=OutputProfile)
    revision: int = 1

    def validate(self):
        if not NAME_RE.match(self.name):
            raise ValidationError("invalid project name")
        if self.source_kind not in ("relay", "playlist", "media"):
            raise ValidationError("invalid source kind")
        if not self.source_ref or len(self.source_ref) > 255 or "\x00" in self.source_ref:
            raise ValidationError("invalid source reference")
        if len(self.overlays) > MAX_OVERLAYS:
            raise ValidationError("too many overlays")
        self.profile.validate()
        for o in self.overlays:
            o.validate()
        return self

    def visible_overlays(self):
        return [o for o in self.overlays if o.visible]

    def overlay_fingerprint(self):
        """Canonical digest of the overlay stack + profile (cache identity)."""
        doc = {
            "p": self.profile.__dict__,
            "o": [o.__dict__ for o in self.visible_overlays()],
        }
        blob = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
