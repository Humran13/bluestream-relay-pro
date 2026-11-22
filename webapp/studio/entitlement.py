"""Signed, expiring, revocable Studio entitlements.

Token:  BS1.<b64url(payload-json)>.<b64url(ed25519-signature over the payload bytes)>
Payload: {"v":1,"lic":"<id>","plan":"studio","state":"active|trial","iat":<epoch>,
          "nbf":<epoch>,"exp":<epoch>,"grace":<seconds, <= MAX_GRACE>,"inst":"<optional install id>"}

No network traffic is needed to validate: the token is verified offline against
the vendor public key. After `exp`, a short bounded grace window keeps an
already-running customer streaming through a renewal/outage; it never extends
a revoked or not-yet-valid licence. Revocation is a vendor-signed list
(`BS1R.<b64>.<sig>` -> {"revoked":[ids]}) that the client may refresh whenever
it is online; if never refreshed, expiry is the backstop.

There is no obscurity here: the security property is the signature plus the
backend checks in every Studio endpoint and in the runtime (see cli.py).
"""
import base64
import json
import os
import time

from . import ed25519

MAX_GRACE = 7 * 86400
DEFAULT_ETC = "/etc/bluestream"
DEFAULT_PUBKEY = "/usr/local/lib/bluestream/studio/vendor_pubkey.hex"


class Verdict:
    __slots__ = ("ok", "state", "reason", "expires", "lic")

    def __init__(self, ok, state, reason, expires=None, lic=""):
        self.ok, self.state, self.reason, self.expires, self.lic = ok, state, reason, expires, lic

    def __repr__(self):
        return "Verdict(%s,%s,%s)" % (self.ok, self.state, self.reason)


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(sk, payload, prefix="BS1"):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "%s.%s.%s" % (prefix, _b64e(raw), _b64e(ed25519.sign(sk, raw)))


def _open(token, pk, prefix):
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != prefix:
        return None
    try:
        raw, sig = _b64d(parts[1]), _b64d(parts[2])
    except (ValueError, TypeError):
        return None
    if not ed25519.verify(pk, raw, sig):
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def revoked_ids(revocation_token, pk):
    doc = _open(revocation_token, pk, "BS1R") if revocation_token else None
    if not doc:
        return frozenset()
    return frozenset(str(x) for x in doc.get("revoked", []) if isinstance(x, str))


def check(token, pk, now=None, revoked=frozenset(), install_id=""):
    """Offline verdict for a Studio entitlement token."""
    now = time.time() if now is None else now
    if not pk:
        return Verdict(False, "unconfigured", "licence verification key is not configured")
    if not token:
        return Verdict(False, "none", "no licence installed")
    doc = _open(token, pk, "BS1")
    if doc is None:
        return Verdict(False, "invalid", "licence signature is invalid")
    try:
        lic, exp, nbf = str(doc["lic"]), float(doc["exp"]), float(doc.get("nbf", 0))
        grace = min(max(float(doc.get("grace", 0)), 0.0), MAX_GRACE)
    except (KeyError, TypeError, ValueError):
        return Verdict(False, "invalid", "licence payload is malformed")
    if doc.get("plan") != "studio" or doc.get("v") != 1:
        return Verdict(False, "invalid", "licence is not a Studio entitlement")
    if lic in revoked:
        return Verdict(False, "revoked", "licence has been revoked", exp, lic)
    inst = doc.get("inst")
    if inst and inst != install_id:
        # A bound licence is only valid where the matching install id is configured; a missing
        # local id must NOT bypass the binding.
        return Verdict(False, "invalid", "licence belongs to a different installation", exp, lic)
    if now < nbf:
        return Verdict(False, "not-yet-valid", "licence is not valid yet", exp, lic)
    state = "trial" if doc.get("state") == "trial" else "active"
    if now <= exp:
        return Verdict(True, state, "licence valid", exp, lic)
    if now <= exp + grace:
        return Verdict(True, "grace", "licence expired; grace period active", exp, lic)
    return Verdict(False, "expired", "licence has expired", exp, lic)


# ---------------------------------------------------------------------------
# file-backed helpers used by the console and the runtime
# ---------------------------------------------------------------------------
def etc_dir():
    """Where the (non-secret, signed) licence files live. The hardened web unit can only
    write the Studio store, so that is the default; BLUESTREAM_ETC_DIR overrides (tests)."""
    explicit = os.environ.get("BLUESTREAM_ETC_DIR")
    if explicit:
        return explicit
    return os.environ.get("BLUESTREAM_STUDIO_DIR", "/var/lib/bluestream/studio")


def get_install_id(create=False):
    """Random id vendors may bind a licence to (`inst`). Created on demand, never changes."""
    path = os.path.join(etc_dir(), "install-id")
    cur = _read(path, 100)
    if cur or not create:
        return cur
    import secrets
    new = secrets.token_hex(8)
    os.makedirs(etc_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(new + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return new


def _read(path, limit=8192):
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("ascii", "ignore").strip()
    except OSError:
        return ""


def load_pubkey():
    hexkey = os.environ.get("BLUESTREAM_STUDIO_PUBKEY_HEX") or _read(
        os.environ.get("BLUESTREAM_STUDIO_PUBKEY_FILE", DEFAULT_PUBKEY), 200)
    try:
        pk = bytes.fromhex(hexkey)
        return pk if len(pk) == 32 else b""
    except ValueError:
        return b""


def enforced():
    """Licensing is DEFERRED: Studio is fully available unless an operator explicitly turns enforcement on later
    (BLUESTREAM_STUDIO_ENFORCE_LICENCE=1). The verification code below is kept dormant for that future work."""
    return os.environ.get("BLUESTREAM_STUDIO_ENFORCE_LICENCE", "") == "1"


def current(now=None):
    if not enforced():
        return Verdict(True, "open", "no licence is required in this build")
    pk = load_pubkey()
    token = _read(os.path.join(etc_dir(), "studio-license.token"))
    rev = _read(os.path.join(etc_dir(), "studio-revocations.token"))
    install = _read(os.path.join(etc_dir(), "install-id"), 100)
    return check(token, pk, now, revoked_ids(rev, pk) if pk else frozenset(), install)


def install_token(token):
    """Verify and persist a pasted token (activation). Returns the Verdict."""
    pk = load_pubkey()
    token = (token or "").strip()
    verdict = check(token, pk, install_id=_read(os.path.join(etc_dir(), "install-id"), 100))
    if verdict.ok or verdict.state in ("grace",):
        path = os.path.join(etc_dir(), "studio-license.token")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(token + "\n")
        os.chmod(tmp, 0o644)  # signed token, not a secret
        os.replace(tmp, path)
    return verdict
