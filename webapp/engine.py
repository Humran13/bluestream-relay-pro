"""Bridge client that calls the existing GUI-1A.1 ``web-ctl`` read-only bridge.

Used by the Flask console. ``web-ctl`` is located via a trusted project-relative
path derived from this file's location (never from cwd, PATH or environment
variables). It is invoked with an explicit argv list and ``shell=False``; only
the four read-only operations are allowed. Raw stderr is never surfaced to
browser users - failures become controlled :class:`EngineError` exceptions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ALLOWED_OPERATIONS = frozenset({"version", "snapshot", "relay_list", "playlist_list"})

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
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout if timeout is not None else self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError("engine request timed out") from exc
        except OSError as exc:
            raise EngineError("engine bridge could not be started") from exc
        if proc.returncode != 0:
            raise EngineError("engine command failed")
        try:
            text = proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EngineError("engine returned undecodable output") from exc
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise EngineError("engine returned invalid JSON") from exc
        if not isinstance(doc, dict) or doc.get("ok") is not True or "data" not in doc:
            raise EngineError("engine returned an invalid response envelope")
        return doc["data"]
