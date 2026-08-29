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

ALLOWED_OPERATIONS = frozenset({"version", "snapshot", "relay_list", "playlist_list"})

# GUI-1B.1: exactly these six lifecycle operations, and nothing else.
ALLOWED_MUTATION_OPERATIONS = frozenset(
    {
        "relay_start",
        "relay_stop",
        "relay_restart",
        "playlist_start",
        "playlist_stop",
        "playlist_restart",
    }
)

# Mirrors bs_valid_name in lib/common.sh: lowercase letters, digits, '-' and
# '_', first character alphanumeric, at most 48 characters.  This is the ONLY
# name rule used by the engine, so the web layer reuses it verbatim.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def valid_target_name(name) -> bool:
    """Return True only for names accepted by the engine's ``bs_valid_name``."""
    return isinstance(name, str) and bool(_NAME_RE.match(name))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Production (privileged) execution paths. These are fixed, absolute, trusted
# paths - never resolved through PATH or from caller input. The installer
# (GUI-1A.3B) will place web-ctl at INSTALLED_WEB_CTL as root-owned 0700.
SUDO_PATH = "/usr/bin/sudo"
INSTALLED_WEB_CTL = "/usr/local/lib/bluestream/web-ctl"


class EngineError(Exception):
    """Controlled, user-safe error when the engine bridge is unavailable."""


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

    def _mutation(self, operation: str, name: str, timeout: float | None = None):
        """Run one controlled lifecycle operation against a validated target.

        ``operation`` is fixed by the calling explicit method (never by browser
        input) and must be in ALLOWED_MUTATION_OPERATIONS.  ``name`` must pass
        :func:`valid_target_name` (the engine's own rule) before any subprocess
        is started.  web-ctl emits a JSON envelope on stdout for both success
        and failure; a non-zero exit or an ``ok: false`` envelope becomes a
        controlled :class:`EngineError` carrying the bridge's message.
        """
        if operation not in ALLOWED_MUTATION_OPERATIONS:
            raise EngineError("unsupported operation: %r" % operation)
        if not valid_target_name(name):
            raise EngineError("invalid target name")
        if self._production:
            cmd = [SUDO_PATH, "-n", INSTALLED_WEB_CTL, operation, name]
        else:
            if not self._web_ctl.is_file():
                raise EngineError("engine bridge not found")
            cmd = [self._bash, str(self._web_ctl), operation, name]
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
                raise EngineError(message)
            raise EngineError("engine command failed")
        return doc.get("data")

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
