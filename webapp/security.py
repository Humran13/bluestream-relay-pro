"""Authentication, CSRF and rate-limiting helpers for the local Flask console.

GUI-1A.2 is a local development foundation. Credentials and the random secret
key live in a non-production runtime state directory (default under the
system temp directory); production ``/etc/bluestream`` integration is deferred.

Design notes:
* Passwords are stored only as ``hashlib.scrypt`` digests with a random salt.
* Verification is constant-time via ``hmac.compare_digest``.
* Passwords are never accepted from argv (only from stdin) and never logged.
* The admin credential file is never silently overwritten.
* The CSRF token is stored in the Flask session and compared constant-time.
* The login rate limiter is an in-memory, process-local, bounded structure
  (temporary; a later phase may move state to a runtime file).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# scrypt parameters (sensible single-VPS values; maxmem covers 128*n*r).
# ---------------------------------------------------------------------------
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R + 1024 * 1024

ADMIN_USERNAME = "admin"
STATE_DIR_NAME = "bluestream-console-dev"


# ---------------------------------------------------------------------------
# State directory / files
# ---------------------------------------------------------------------------
def default_state_dir() -> Path:
    """Runtime state directory for local development (clearly non-production)."""
    return Path(tempfile.gettempdir()) / STATE_DIR_NAME


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600 on platforms with POSIX modes (no-op elsewhere)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_private_write(path: Path, content: str) -> None:
    """Atomically write private file content (exclusive temp, 0600, replace).

    The temp name is random and created with ``O_EXCL`` so a pre-existing
    symlink at a predictable name cannot redirect the write (or clobber an
    arbitrary file). ``os.replace`` guarantees atomicity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp.%s" % (path.name, secrets.token_hex(8)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _restrict_permissions(tmp)
    os.replace(tmp, path)
    _restrict_permissions(path)


def _write_private_json(path: Path, data: dict) -> None:
    """Atomically write a JSON dict to a private (0600) file."""
    _atomic_private_write(path, json.dumps(data))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------
def ensure_secret_key(state_dir) -> str:
    """Return the per-instance secret key, generating and persisting it once.

    The key is never regenerated on restart and is never exposed to the app
    layer beyond Flask's config.
    """
    state_dir = Path(state_dir)
    key_file = state_dir / "secret_key"
    try:
        existing = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    key = secrets.token_hex(32)
    _atomic_private_write(key_file, key + "\n")
    return key


# ---------------------------------------------------------------------------
# Password hashing (scrypt) and admin credential record
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> dict:
    """Hash a password with scrypt + random salt; returns a storeable record."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return {
        "algorithm": "scrypt",
        "salt": salt.hex(),
        "hash": dk.hex(),
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
        "maxmem": SCRYPT_MAXMEM,
    }


def verify_password(password: str, record) -> bool:
    """Constant-time verification of a stored admin record.

    Rejects malformed or implausible records (never crashes).
    """
    if not isinstance(record, dict) or record.get("algorithm") != "scrypt":
        return False
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        n = int(record.get("n", SCRYPT_N))
        r = int(record.get("r", SCRYPT_R))
        p = int(record.get("p", SCRYPT_P))
        dklen = int(record.get("dklen", SCRYPT_DKLEN))
        maxmem = int(record.get("maxmem", SCRYPT_MAXMEM))
    except (KeyError, ValueError, TypeError):
        return False
    if not (2 ** 10 <= n <= 2 ** 20 and 1 <= r <= 64 and 1 <= p <= 8 and 16 <= dklen <= 64):
        return False
    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=dklen,
            maxmem=maxmem,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def load_admin_record(state_dir):
    return _read_json(Path(state_dir) / "admin.json")


def save_admin_record(state_dir, record: dict) -> None:
    _write_private_json(Path(state_dir) / "admin.json", record)


def init_admin(state_dir, password: str, username: str = ADMIN_USERNAME) -> dict:
    """Create the local admin credential record. Refuses to overwrite.

    ``password`` must already be in memory (from stdin); it is never written
    to disk in plaintext and never appears in argv.
    """
    state_dir = Path(state_dir)
    record_file = state_dir / "admin.json"
    if record_file.exists():
        raise FileExistsError("admin credential file already exists: %s" % record_file)
    if not password:
        raise ValueError("password must not be empty")
    if not username:
        raise ValueError("username must not be empty")
    record = {
        "username": username,
        **hash_password(password),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save_admin_record(state_dir, record)
    return record


# ---------------------------------------------------------------------------
# CSRF tokens (session-backed, constant-time verification)
# ---------------------------------------------------------------------------
def generate_csrf_token(session) -> str:
    """Return (creating on first use) the session CSRF token."""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def verify_csrf_token(session, submitted) -> bool:
    """Constant-time CSRF check against the session token."""
    expected = session.get("_csrf_token")
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))


# ---------------------------------------------------------------------------
# Login rate limiting (in-memory, process-local, bounded)
# ---------------------------------------------------------------------------
class LoginRateLimiter:
    """Bounded in-memory failure tracking with temporary lockouts.

    Process-local by design for GUI-1A.2 (no Redis, no database). A later
    production phase may persist state to a runtime file.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        lockout_seconds: float = 300.0,
        max_keys: int = 500,
    ):
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self.max_keys = max(1, int(max_keys))
        self._failures: dict[str, list[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _normalize(key: str) -> str:
        key = (key or "unknown").strip()
        return key or "unknown"

    def prune(self, now: float | None = None) -> None:
        """Drop expired lockouts/failures and bound total key count."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            expired = [k for k, ts in self._lockouts.items() if ts <= now]
            for k in expired:
                del self._lockouts[k]
            for k in list(self._failures.keys()):
                kept = [t for t in self._failures[k] if t > cutoff]
                if kept:
                    self._failures[k] = kept
                else:
                    del self._failures[k]
            if len(self._failures) > self.max_keys:
                ordered = sorted(self._failures.items(), key=lambda kv: max(kv[1]))
                for k, _ in ordered[: len(self._failures) - self.max_keys]:
                    del self._failures[k]

    def is_locked(self, key: str) -> bool:
        self.prune()
        key = self._normalize(key)
        with self._lock:
            expiry = self._lockouts.get(key)
            if expiry is not None and expiry > time.monotonic():
                return True
            if expiry is not None:
                del self._lockouts[key]
        return False

    def record_failure(self, key: str) -> None:
        self.prune()
        key = self._normalize(key)
        now = time.monotonic()
        with self._lock:
            timestamps = self._failures.setdefault(key, [])
            timestamps.append(now)
            cutoff = now - self.window_seconds
            timestamps[:] = [t for t in timestamps if t > cutoff][-self.max_attempts :]
            if len(timestamps) >= self.max_attempts:
                self._lockouts[key] = now + self.lockout_seconds
                timestamps[:] = []  # lockout governs; reset the counter

    def record_success(self, key: str) -> None:
        self.prune()
        key = self._normalize(key)
        with self._lock:
            self._failures.pop(key, None)
            self._lockouts.pop(key, None)

    # -- introspection helpers (tests / diagnostics) -------------------------
    def failure_count(self, key: str) -> int:
        self.prune()
        key = self._normalize(key)
        with self._lock:
            return len(self._failures.get(key, []))

    def size(self) -> int:
        self.prune()
        with self._lock:
            return len(self._failures) + len(self._lockouts)


# ---------------------------------------------------------------------------
# CLI: local admin initialization (password from stdin only)
# ---------------------------------------------------------------------------
def _cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webapp.security")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-admin", help="Initialize the local admin account")
    init.add_argument("--state-dir", default=str(default_state_dir()))
    init.add_argument("--username", default=ADMIN_USERNAME)
    args = parser.parse_args(argv)

    if args.command == "init-admin":
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            sys.stderr.write("error: no password provided on stdin\n")
            return 2
        record = init_admin(args.state_dir, password, username=args.username)
        sys.stdout.write(
            "admin account '%s' initialized in %s\n" % (record["username"], args.state_dir)
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main())
