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
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts", ".avi")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")

KINDS = ("text", "image", "rect", "clock", "countdown", "countup", "pomodoro", "ticker", "nowplaying", "data", "video", "browser")
# Kinds whose rendered output changes over time -> need live composition.
DYNAMIC_KINDS = frozenset({"clock", "countdown", "countup", "pomodoro", "ticker", "nowplaying", "data", "video", "browser"})
MAX_OVERLAYS = 24
RESOLUTIONS = ((854, 480), (1280, 720), (1920, 1080))
SCALERS = ("bilinear", "bicubic", "lanczos")
ENCODERS = ("libx264", "h264_nvenc")
ALIGNS = ("left", "center", "right")
# Layer kinds whose text is re-read from a small file once a second: their CONTENT can change without restarting FFmpeg.
TEXT_BACKED = frozenset({"text", "ticker", "clock", "countdown", "countup", "pomodoro", "nowplaying", "data"})
CONTENT_FIELDS = frozenset({"text", "target", "finished_text", "tz", "work_min", "break_min", "url", "json_path", "refresh_s", "value_format"})
UI_ONLY_FIELDS = frozenset({"label", "locked"})
LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f<>]{0,40}$")


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
    scaler: str = "bicubic"   # scaling filter used to fit the source onto the canvas
    encoder: str = "libx264"  # video encoder: libx264 (software, default) or h264_nvenc (only after a real NVENC init test)

    def validate(self):
        if (self.width, self.height) not in RESOLUTIONS:
            raise ValidationError("unsupported resolution")
        if self.fps not in (24, 25, 30, 60):
            raise ValidationError("unsupported fps")
        if not 1000 <= self.video_kbps <= 12000:
            raise ValidationError("video bitrate out of range")
        if not 64 <= self.audio_kbps <= 320:
            raise ValidationError("audio bitrate out of range")
        if self.preset not in ("ultrafast", "superfast", "veryfast", "faster"):
            raise ValidationError("unsupported preset")
        if self.scaler not in SCALERS:
            raise ValidationError("unsupported scaling filter")
        if self.encoder not in ENCODERS:
            raise ValidationError("unsupported encoder")
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
    target: str = ""  # ISO-8601 absolute deadline/anchor (countdown, countup, pomodoro)
    work_min: int = 25
    break_min: int = 5
    finished_text: str = "LIVE NOW"
    url: str = ""        # data overlay: public http(s) JSON endpoint (SSRF-guarded fetch)
    json_path: str = ""  # data overlay: dotted path to a scalar, e.g. "items.0.subscribers"
    refresh_s: int = 60
    crop_x: float = 0.0  # image/video/browser overlays: visible part of the source (fractions)
    crop_y: float = 0.0
    crop_w: float = 1.0
    crop_h: float = 1.0
    value_format: str = "raw"  # data overlay: raw | int | thousands | decimal1 | percent
    selector: str = ""         # browser overlay: CSS selector of the element to capture ("" = whole viewport)
    view_w: int = 1280         # browser overlay: page viewport
    view_h: int = 720
    allowed_hosts: str = ""    # browser overlay: extra hosts the page may load from (comma separated)
    label: str = ""            # optional layer name shown in the workspace (never rendered into the video)
    locked: bool = False       # workspace only: a locked layer cannot be dragged or resized
    align: str = "left"        # text layers: alignment inside the layer box (left | center | right)
    bg_color: str = "#000000"  # text layers: background box colour ...
    bg_opacity: float = 0.0    # ... and opacity (0 = transparent, no box)

    @property
    def cropped(self):
        return (self.crop_x, self.crop_y, self.crop_w, self.crop_h) != (0.0, 0.0, 1.0, 1.0)

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
        if self.align not in ALIGNS:
            raise ValidationError("unknown text alignment")
        if not COLOR_RE.match(self.bg_color) or not 0.0 <= self.bg_opacity <= 1.0:
            raise ValidationError("invalid background colour or opacity")
        if not LABEL_RE.match(self.label):
            raise ValidationError("layer name is too long or contains invalid characters")
        if self.kind in ("image", "video") and not ASSET_RE.match(self.asset):
            raise ValidationError("invalid asset name")
        if self.kind == "image" and not self.asset.lower().endswith(IMAGE_EXTS):
            raise ValidationError("image overlays need an image file (png/jpg/webp/gif)")
        if self.kind == "video" and not self.asset.lower().endswith(VIDEO_EXTS):
            raise ValidationError("video overlays need a video file")
        if self.kind in ("countdown", "countup", "pomodoro") and not self.target:
            raise ValidationError("%s needs a target/anchor time" % self.kind)
        if not (1 <= self.work_min <= 600 and 1 <= self.break_min <= 600):
            raise ValidationError("pomodoro minutes out of range")
        for v in (self.crop_x, self.crop_y, self.crop_w, self.crop_h):
            if not 0.0 <= v <= 1.0:
                raise ValidationError("crop outside 0-1")
        if self.crop_w < 0.05 or self.crop_h < 0.05 or self.crop_x + self.crop_w > 1.0001                 or self.crop_y + self.crop_h > 1.0001:
            raise ValidationError("crop area is invalid")
        if self.value_format not in ("raw", "int", "thousands", "decimal1", "percent"):
            raise ValidationError("unknown value format")
        if self.kind == "browser":
            from . import browsercap
            browsercap.validate_spec(self.url, self.selector, self.view_w, self.view_h, self.refresh_s,
                                     self.allowed_hosts)
        if self.kind == "data":
            if not (self.url.startswith(("http://", "https://")) and len(self.url) <= 500):
                raise ValidationError("data overlay needs an http(s) URL")
            if not re.match(r"^[A-Za-z0-9_.-]{1,100}$", self.json_path):
                raise ValidationError("invalid JSON path")
            if not 30 <= self.refresh_s <= 86400:
                raise ValidationError("refresh must be 30-86400 seconds")
        if len(self.finished_text) > 100:
            raise ValidationError("finished text too long")
        return self


@dataclass
class Project:
    name: str
    source_kind: str  # "relay" | "playlist" | "media"
    source_ref: str
    overlays: list = field(default_factory=list)
    profile: OutputProfile = field(default_factory=OutputProfile)
    revision: int = 1
    slide_seconds: int = 10      # slideshow: seconds per image
    slide_shuffle: bool = False  # slideshow: deterministic shuffle (seeded by name list)
    background: str = "#0b1220"  # audio source: "#rrggbb" or a Media Library image name
    visualizer: bool = False     # audio source: live waveform strip (needs a live encode)

    def slides(self):
        return [s for s in self.source_ref.split(",") if s]

    def validate(self):
        if not NAME_RE.match(self.name):
            raise ValidationError("invalid project name")
        if self.source_kind not in ("relay", "playlist", "media", "slideshow", "audio"):
            raise ValidationError("invalid source kind")
        if not self.source_ref or len(self.source_ref) > 255 or "\x00" in self.source_ref:
            raise ValidationError("invalid source reference")
        if self.source_kind == "audio":
            if not ASSET_RE.match(self.source_ref) or not self.source_ref.lower().endswith(AUDIO_EXTS):
                raise ValidationError("audio source must be an audio file from the Media Library")
            if not (COLOR_RE.match(self.background) or (
                    ASSET_RE.match(self.background)
                    and self.background.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))):
                raise ValidationError("background must be a #rrggbb colour or an image name")
        if self.source_kind == "slideshow":
            names = self.slides()
            if not 1 <= len(names) <= 50 or not all(
                    ASSET_RE.match(n) and n.lower().endswith(IMAGE_EXTS) for n in names):
                raise ValidationError("slideshow needs 1-50 image file names (comma separated)")
            if not 2 <= self.slide_seconds <= 3600:
                raise ValidationError("seconds per slide must be 2-3600")
        if self.source_kind == "relay" and self.source_ref == self.name:
            raise ValidationError("a Studio stream cannot read from itself")
        if len(self.overlays) > MAX_OVERLAYS:
            raise ValidationError("too many overlays")
        self.profile.validate()
        for o in self.overlays:
            o.validate()
        return self

    @property
    def dynamic_source(self):
        """True when the SOURCE itself changes every frame (needs a live encode)."""
        return self.source_kind == "audio" and self.visualizer

    def visible_overlays(self):
        return [o for o in self.overlays if o.visible]

    def overlay_fingerprint(self):
        """Canonical digest of the overlay stack + profile (cache identity)."""
        doc = {
            "p": self.profile.__dict__,
            "s": [self.source_kind, self.background, self.visualizer],
            "o": [{k: v for k, v in o.__dict__.items() if k not in UI_ONLY_FIELDS} for o in self.visible_overlays()],
        }
        blob = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def structure_fingerprint(self):
        """Digest of everything baked into the FFmpeg filter graph. Two revisions with the same digest differ only in
        file-backed text CONTENT, so a running live composition can adopt the new one with no restart."""
        o_docs = []
        for o in self.visible_overlays():
            drop = UI_ONLY_FIELDS | (CONTENT_FIELDS if o.kind in TEXT_BACKED else frozenset())
            o_docs.append({k: v for k, v in o.__dict__.items() if k not in drop})
        doc = {"p": self.profile.__dict__,
               "s": [self.source_kind, self.source_ref, self.slide_seconds, self.slide_shuffle, self.background, self.visualizer],
               "o": o_docs}
        return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def runs_live_encode(self):
        """True when this project's pipeline is a continuous live composition (text files are re-read every second)."""
        vis = self.visible_overlays()
        if self.dynamic_source or any(o.dynamic for o in vis):
            return True
        return self.source_kind in ("relay", "playlist") and bool(vis)
