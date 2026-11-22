"""Studio workspace preferences (global to this dashboard), validated server-side.

Only settings the workspace really honours live here. Per-project output settings (resolution, bitrate,
frame rate, scaler, preset) belong to the project's OutputProfile and are saved with the project.
Strict schema: unknown keys and wrong types are rejected, never stored.
"""
import json
import os
import re
import tempfile

from .model import ValidationError

DEFAULTS = {
    "general": {
        "theme": "dark",              # dark | light
        "confirm_stop": True,         # ask before stopping a stream
        "confirm_apply": True,        # ask before applying changes to a running stream
        "snap_enabled": True,
        "snap_sensitivity": 12,       # 1-40 (canvas pixels at 1280 wide)
        "snap_edges": True,
        "snap_sources": True,
        "snap_center": True,
        "safe_areas": False,          # draw the EBU R 95 title-safe/action-safe guides on the preview
        "show_grid": False,
    },
    "hotkeys": {
        "save": "Ctrl+S",
        "apply": "Ctrl+Shift+Enter",
        "duplicate": "Ctrl+D",
        "delete": "Delete",
        "toggle_visible": "H",
        "layer_up": "Ctrl+ArrowUp",
        "layer_down": "Ctrl+ArrowDown",
    },
    "advanced": {
        "status_poll_s": 5,           # how often stream status is refreshed (2-60 s)
        "text_preview_s": 5,          # how often live text (clock/countdown) previews refresh (2-60 s)
        "video_thumbs": True,         # load video thumbnails in the media browser
    },
}

HOTKEY_RE = re.compile(r"^(?:(?:Ctrl|Alt|Shift)\+){0,3}(?:[A-Z0-9]|Arrow(?:Up|Down|Left|Right)|Enter|Delete|Backspace|F(?:[1-9]|1[0-2]))$")
THEMES = ("dark", "light")


def _path():
    base = os.environ.get("BLUESTREAM_STUDIO_DIR", "/var/lib/bluestream/studio")
    return os.path.join(base, ".ui-settings.json")


def _clone():
    return {sec: dict(vals) for sec, vals in DEFAULTS.items()}


def validate(doc):
    """Return a complete, normalised settings document or raise ValidationError."""
    if not isinstance(doc, dict):
        raise ValidationError("settings must be an object")
    out = _clone()
    for sec, vals in doc.items():
        if sec not in DEFAULTS:
            raise ValidationError("unknown settings section")
        if not isinstance(vals, dict):
            raise ValidationError("settings section must be an object")
        for key, val in vals.items():
            if key not in DEFAULTS[sec]:
                raise ValidationError("unknown setting '%s'" % key)
            default = DEFAULTS[sec][key]
            if isinstance(default, bool):
                if not isinstance(val, bool):
                    raise ValidationError("'%s' must be on or off" % key)
            elif isinstance(default, int):
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValidationError("'%s' must be a whole number" % key)
            elif not isinstance(val, str):
                raise ValidationError("'%s' must be text" % key)
            out[sec][key] = val
    g, a = out["general"], out["advanced"]
    if g["theme"] not in THEMES:
        raise ValidationError("theme must be dark or light")
    if not 1 <= g["snap_sensitivity"] <= 40:
        raise ValidationError("snap sensitivity must be 1-40")
    for key in ("status_poll_s", "text_preview_s"):
        if not 2 <= a[key] <= 60:
            raise ValidationError("%s must be 2-60 seconds" % key)
    seen = {}
    for action, combo in out["hotkeys"].items():
        if not HOTKEY_RE.match(combo):
            raise ValidationError("'%s' is not a valid shortcut" % combo)
        if combo in seen:
            raise ValidationError("shortcut %s is used for both %s and %s" % (combo, seen[combo], action))
        seen[combo] = action
    return out


def load():
    try:
        with open(_path(), encoding="utf-8") as fh:
            raw = fh.read(64 * 1024)
        return validate(json.loads(raw))
    except (OSError, ValueError, ValidationError):
        return _clone()          # missing or damaged -> safe defaults, never an error page


def save(doc):
    clean = validate(doc)
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".w")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return clean
