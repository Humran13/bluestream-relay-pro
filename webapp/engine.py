"""Bridge client that calls the existing ``web-ctl`` bridge.

Used by the Flask console. ``web-ctl`` is located via a trusted project-relative
path derived from this file's location (never from cwd, PATH or environment
variables). It is invoked with an explicit argv list and ``shell=False``.

* Read-only operations (``call``): version, snapshot, relay_list, playlist_list.
* Controlled lifecycle operations (GUI-1B.1, ``relay_start`` ... ``playlist_restart``):
  each is an explicit method, the operation string is fixed by the method (never
  by browser input) and the target name is validated here against the engine's
  own name rule before any subprocess runs.

Raw stderr is never surfaced to browser users - failures become controlled
:class:`EngineError` exceptions.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ALLOWED_OPERATIONS = frozenset(
    {"version", "snapshot", "relay_list", "playlist_list", "media_list"}
)

# GUI-1B.1/GUI-1C.1: exactly these controlled write operations, and nothing else.
ALLOWED_MUTATION_OPERATIONS = frozenset(
    {
        "relay_start",
        "relay_stop",
        "relay_restart",
        "playlist_start",
        "playlist_stop",
        "playlist_restart",
        "relay_create_url",
        "relay_create_media",
        "media_import_staged",
        # GUI-1D.1: fixed-argv playlist creation (validated in
        # _validate_mutation_values before any subprocess is started).
        "playlist_create",
    }
)

# GUI-1D.1: shared ceiling for playlist entries (mirrors
# BLUESTREAM_PLAYLIST_MAX_ITEMS in lib/playlist.sh). Bounds privileged argv and
# config writes; the web form validates the same limit before submission.
MAX_PLAYLIST_ITEMS = 64

# Mirrors bs_valid_name in lib/common.sh: lowercase letters, digits, '-' and
# '_', first character alphanumeric, at most 48 characters.  This is the ONLY
# relay-name rule used by the engine, so the web layer reuses it verbatim.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Mirrors bs_valid_media_name in lib/common.sh: [A-Za-z0-9._-], no leading
# dot, no "..", max 255 chars. Used for managed-media file names and the web
# upload staging id.
_MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

# Source URL schemes permitted by the engine (bs_valid_url / bs_classify_source_type).
_SUPPORTED_URL_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://")

# Conservative browser-upload extension set (engine ffprobe is the real gate).
ALLOWED_UPLOAD_EXTENSIONS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".ts")


def valid_target_name(name) -> bool:
    """Return True only for names accepted by the engine's ``bs_valid_name``."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def valid_media_name(name) -> bool:
    """Return True only for names accepted by the engine's ``bs_valid_media_name``."""
    return isinstance(name, str) and bool(_MEDIA_NAME_RE.match(name))


def valid_source_url(url) -> bool:
    """Treat a source URL strictly as data (never executed).

    Mirrors the engine's ``bs_valid_url``: supported scheme only, no whitespace,
    control characters or shell-hostile characters, bounded length.
    """
    if not isinstance(url, str) or not url:
        return False
    if len(url) > 4096:
        return False
    if not url.startswith(_SUPPORTED_URL_SCHEMES):
        return False
    for ch in url:
        o = ord(ch)
        if o < 0x21 or o == 0x7F:
            return False
    if any(ch in url for ch in '`"\\<>{}[]'):
        return False
    return True

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Production (privileged) execution paths. These are fixed, absolute, trusted
# paths - never resolved through PATH or from caller input. The installer
# (GUI-1A.3B) will place web-ctl at INSTALLED_WEB_CTL as root-owned 0700.
SUDO_PATH = "/usr/bin/sudo"
INSTALLED_WEB_CTL = "/usr/local/lib/bluestream/web-ctl"


class EngineError(Exception):
    """Controlled, user-safe error when the engine bridge is unavailable.

    ``code`` optionally carries the web-ctl failure code (e.g. INVALID_NAME,
    ALREADY_EXISTS) so callers can show specific, controlled messages.
    """

    def __init__(self, message: str = "", code: str | None = None):
        super().__init__(message)
        self.code = code


def default_web_ctl_path() -> Path:
    """Trusted project-relative location of the web-ctl bridge."""
    return PROJECT_ROOT / "web-ctl"


def default_bash_path() -> str:
    """Resolve a trusted bash for running web-ctl in development.

    Installed/privileged deployments use the standard absolute path; Windows
    development environments fall back to Git Bash and then a PATH lookup.
    """
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if Path(candidate).is_file():
            return candidate
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if found:
        return found
    return "/bin/bash"


class EngineClient:
    """Thin, fail-closed client over the web-ctl bridge.

    Local (development) mode runs the project's web-ctl through a trusted
    bash. Production mode runs the installed web-ctl exactly through
    ``/usr/bin/sudo -n /usr/local/lib/bluestream/web-ctl <operation>`` with an
    argv list and ``shell=False``; sudo and web-ctl paths are fixed module
    constants (no PATH lookup, no caller-controlled executable path).
    """

    def __init__(self, web_ctl=None, bash=None, timeout: float = 10.0, production: bool = False):
        self._production = bool(production)
        if self._production:
            # Fixed trusted paths; resolved at call time from module constants.
            self._web_ctl = None
            self._bash = None
        else:
            self._web_ctl = Path(web_ctl) if web_ctl is not None else default_web_ctl_path()
            self._bash = bash if bash is not None else default_bash_path()
        self.timeout = float(timeout)

    @property
    def web_ctl_path(self) -> Path:
        return self._web_ctl

    @property
    def bash_path(self) -> str:
        return self._bash

    @property
    def is_production(self) -> bool:
        return self._production

    def _run_argv(self, argv, timeout: float | None = None) -> tuple[int, str]:
        """Run one web-ctl argv list with ``shell=False``; return (rc, stdout).

        Timeout and missing-executable failures become :class:`EngineError`.
        The caller interprets the exit status and stdout (read-only operations
        fail on any non-zero exit; lifecycle operations parse the JSON envelope
        that web-ctl emits on stdout for both success and failure).
        """
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout if timeout is not None else self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("engine request timed out") from exc
        except OSError as exc:
            raise EngineError("engine bridge could not be started") from exc
        try:
            text = proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EngineError("engine returned undecodable output") from exc
        return proc.returncode, text

    def call(self, operation: str, timeout: float | None = None):
        """Run one read-only web-ctl operation and return its ``data`` payload.

        Raises :class:`EngineError` on unsupported operations, bridge failure,
        timeout, invalid JSON, or an unexpected envelope. Never returns raw
        process output.
        """
        if operation not in ALLOWED_OPERATIONS:
            raise EngineError("unsupported operation: %r" % operation)
        if self._production:
            cmd = [SUDO_PATH, "-n", INSTALLED_WEB_CTL, operation]
        else:
            if not self._web_ctl.is_file():
                raise EngineError("engine bridge not found")
            cmd = [self._bash, str(self._web_ctl), operation]
        rc, text = self._run_argv(cmd, timeout)
        if rc != 0:
            raise EngineError("engine command failed")
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise EngineError("engine returned invalid JSON") from exc
        if not isinstance(doc, dict) or doc.get("ok") is not True or "data" not in doc:
            raise EngineError("engine returned an invalid response envelope")
        return doc["data"]

    def _mutation(self, operation: str, *values, timeout: float | None = None):
        """Run one controlled lifecycle/creation operation against validated values.

        ``operation`` is fixed by the calling explicit method (never by browser
        input) and must be in ALLOWED_MUTATION_OPERATIONS.  Values are validated
        per operation (stream name, media name, or source URL) before any
        subprocess is started.  web-ctl emits a JSON envelope on stdout for both
        success and failure; a non-zero exit or an ``ok: false`` envelope becomes
        a controlled :class:`EngineError` carrying the bridge's message/code.
        """
        if operation not in ALLOWED_MUTATION_OPERATIONS:
            raise EngineError("unsupported operation: %r" % operation)
        validated = self._validate_mutation_values(operation, values)
        if self._production:
            cmd = [SUDO_PATH, "-n", INSTALLED_WEB_CTL, operation, *validated]
        else:
            if not self._web_ctl.is_file():
                raise EngineError("engine bridge not found")
            cmd = [self._bash, str(self._web_ctl), operation, *validated]
        rc, text = self._run_argv(cmd, timeout)
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise EngineError("engine returned invalid JSON") from exc
        if not isinstance(doc, dict):
            raise EngineError("engine returned an invalid response envelope")
        if rc != 0 or doc.get("ok") is not True:
            message = doc.get("error")
            if isinstance(message, str) and message.strip():
                raise EngineError(message, code=doc.get("code"))
            raise EngineError("engine command failed", code=doc.get("code"))
        return doc.get("data")

    @staticmethod
    def _validate_mutation_values(operation: str, values: tuple) -> list:
        """Validate argv values for a fixed mutation operation before execution.

        Returns the validated argv list; raises :class:`EngineError` on any
        invalid input so no subprocess is ever started with bad data.
        """
        if operation in (
            "relay_start", "relay_stop", "relay_restart",
            "playlist_start", "playlist_stop", "playlist_restart",
        ):
            if len(values) != 1 or not valid_target_name(values[0]):
                raise EngineError("invalid target name", code="INVALID_NAME")
            return list(values)
        if operation == "media_import_staged":
            if len(values) != 1 or not valid_media_name(values[0]):
                raise EngineError("invalid upload id", code="INVALID_STAGING")
            return list(values)
        if operation == "relay_create_url":
            if len(values) != 2:
                raise EngineError("invalid arguments", code="MISSING_ARGUMENT")
            name, url = values
            if not valid_target_name(name):
                raise EngineError("invalid stream name", code="INVALID_NAME")
            if not valid_source_url(url):
                raise EngineError("unsupported or malformed source URL", code="INVALID_URL")
            return [name, url]
        if operation == "relay_create_media":
            if len(values) != 2:
                raise EngineError("invalid arguments", code="MISSING_ARGUMENT")
            name, media = values
            if not valid_target_name(name):
                raise EngineError("invalid stream name", code="INVALID_NAME")
            if not valid_media_name(media):
                raise EngineError("invalid media name", code="INVALID_MEDIA")
            return [name, media]
        if operation == "playlist_create":
            # Fixed argv: name + 2..MAX_PLAYLIST_ITEMS unique, strictly-valid
            # media basenames.  Anything malformed raises BEFORE a subprocess
            # starts, so an arbitrary path/traversal/shell fragment can never
            # reach web-ctl.  Order is preserved exactly (argv order is the
            # playback order).
            if len(values) < 3:
                raise EngineError(
                    "a playlist needs a name and at least two media files",
                    code="TOO_FEW_ITEMS",
                )
            if len(values) > 1 + MAX_PLAYLIST_ITEMS:
                raise EngineError(
                    "too many media files (maximum is %d)" % MAX_PLAYLIST_ITEMS,
                    code="TOO_MANY_ITEMS",
                )
            name = values[0]
            if not valid_target_name(name):
                raise EngineError("invalid playlist name", code="INVALID_NAME")
            seen = set()
            for media in values[1:]:
                if not valid_media_name(media):
                    raise EngineError("invalid media name", code="INVALID_MEDIA")
                if media in seen:
                    raise EngineError(
                        "the same media file cannot be used more than once",
                        code="DUPLICATE_ITEM",
                    )
                seen.add(media)
            return list(values)
        raise EngineError("unsupported operation: %r" % operation)

    # ------------------------------------------------------------------
    # GUI-1B.1: the ONLY lifecycle entry points (operation fixed per method).
    # ------------------------------------------------------------------
    def relay_start(self, name: str):
        return self._mutation("relay_start", name)

    def relay_stop(self, name: str):
        return self._mutation("relay_stop", name)

    def relay_restart(self, name: str):
        return self._mutation("relay_restart", name)

    def playlist_start(self, name: str):
        return self._mutation("playlist_start", name)

    def playlist_stop(self, name: str):
        return self._mutation("playlist_stop", name)

    def playlist_restart(self, name: str):
        return self._mutation("playlist_restart", name)

    # ------------------------------------------------------------------
    # GUI-1C.1: relay creation + media import (operation fixed per method).
    # ------------------------------------------------------------------
    def media_list(self):
        return self.call("media_list")

    def relay_create_url(self, name: str, url: str):
        return self._mutation("relay_create_url", name, url)

    def relay_create_media(self, name: str, media: str):
        return self._mutation("relay_create_media", name, media)

    def media_import_staged(self, staging: str):
        return self._mutation("media_import_staged", staging)

    def playlist_create(self, name: str, items: list):
        """Create a stopped playlist from an ordered list of media basenames.

        ``items`` must be an ordered sequence of 2..MAX_PLAYLIST_ITEMS media
        basenames; the order is preserved exactly as the playback order.
        """
        return self._mutation("playlist_create", name, *items)
