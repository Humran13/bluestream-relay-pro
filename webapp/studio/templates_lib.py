"""Starter layouts. All coordinates are canvas fractions; every template validates."""
from datetime import datetime, timedelta, timezone

from .model import Overlay, ValidationError

TEMPLATES = {
    "blank": "Blank (no overlays: cheapest, direct copy when compatible)",
    "countdown": "Countdown to an event",
    "pomodoro": "Study / Pomodoro timer",
    "radio": "Music radio with Now Playing",
    "relax": "Relaxation loop with clock",
    "clock": "World clock",
    "ticker": "Promotional ticker",
    "scoreboard": "Basic information board",
}


def _soon(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0, tzinfo=None).isoformat()


def default_overlay(kind):
    d = {
        "text": Overlay("text", x=0.05, y=0.05, w=0.5, h=0.12, text="Your text"),
        "image": Overlay("image", x=0.85, y=0.05, w=0.1, h=0.1, asset="logo.png"),
        "rect": Overlay("rect", x=0.0, y=0.88, w=1.0, h=0.12, color="#000000", opacity=0.55),
        "clock": Overlay("clock", x=0.7, y=0.05, w=0.25, h=0.1, size=56),
        "countdown": Overlay("countdown", x=0.3, y=0.4, w=0.4, h=0.15, size=96, target=_soon()),
        "countup": Overlay("countup", x=0.3, y=0.4, w=0.4, h=0.15, size=72, target=_soon(-1)),
        "pomodoro": Overlay("pomodoro", x=0.3, y=0.4, w=0.4, h=0.15, size=96, target=_soon(0)),
        "ticker": Overlay("ticker", x=0.0, y=0.92, w=1.0, h=0.07, size=36, text="Welcome - thanks for watching"),
        "data": Overlay("data", x=0.05, y=0.8, w=0.5, h=0.1, size=40, text="Subscribers: {value}",
                        url="https://example.com/stats.json", json_path="subscribers"),
        "browser": Overlay("browser", x=0.55, y=0.1, w=0.4, h=0.3, url="https://example.com/",
                           refresh_s=60, view_w=1280, view_h=720),
        "video": Overlay("video", x=0.7, y=0.05, w=0.25, h=0.25, asset="clip.mp4"),
        "nowplaying": Overlay("nowplaying", x=0.05, y=0.9, w=0.6, h=0.08, size=36, text="Now playing:"),
    }
    if kind not in d:
        raise ValidationError("unknown overlay kind")
    return d[kind]


def build(name):
    if name not in TEMPLATES:
        raise ValidationError("unknown template")
    o = default_overlay
    return {
        "blank": [],
        "countdown": [o("rect"), Overlay("text", x=0.25, y=0.28, w=0.5, h=0.1, size=48, text="Starting in"),
                      o("countdown")],
        "pomodoro": [Overlay("text", x=0.3, y=0.28, w=0.4, h=0.1, size=48, text="Study session"),
                     o("pomodoro")],
        "radio": [o("rect"), o("nowplaying"), o("clock")],
        "relax": [o("clock"), Overlay("text", x=0.05, y=0.9, w=0.6, h=0.08, size=36, text="Relax and breathe")],
        "clock": [Overlay("clock", x=0.3, y=0.2, w=0.4, h=0.15, size=96, tz="UTC"),
                  Overlay("clock", x=0.3, y=0.5, w=0.4, h=0.15, size=96, tz="UTC")],
        "ticker": [o("rect"), o("ticker")],
        "scoreboard": [o("rect"), Overlay("text", x=0.05, y=0.9, w=0.4, h=0.08, size=40, text="Home 0"),
                       Overlay("text", x=0.55, y=0.9, w=0.4, h=0.08, size=40, text="Away 0")],
    }[name]
