#!/usr/bin/env python3
"""VENDOR-ONLY licence tool. Never ship the private key; never run on a customer VPS.

    studio_license.py keygen  --out vendor.key          # prints the PUBLIC key hex
    studio_license.py issue   --key vendor.key --lic ID --days 30 [--trial] [--grace-days 3] [--inst INSTALL_ID]
    studio_license.py revoke  --key vendor.key --lic ID [--lic ID2 ...]

The public key hex goes into the customer install as
/usr/local/lib/bluestream/studio/vendor_pubkey.hex (see docs/bluestream/COMMERCIAL.md).
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from webapp.studio import ed25519, entitlement  # noqa: E402


def load_key(path):
    raw = bytes.fromhex(open(path).read().strip())
    if len(raw) != 32:
        sys.exit("bad key file")
    return raw


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--out", required=True)
    i = sub.add_parser("issue")
    i.add_argument("--key", required=True)
    i.add_argument("--lic", required=True)
    i.add_argument("--days", type=float, required=True)
    i.add_argument("--grace-days", type=float, default=3)
    i.add_argument("--trial", action="store_true")
    i.add_argument("--inst", default="")
    r = sub.add_parser("revoke")
    r.add_argument("--key", required=True)
    r.add_argument("--lic", action="append", required=True)
    a = ap.parse_args()
    if a.cmd == "keygen":
        if os.path.exists(a.out):
            sys.exit("refusing to overwrite existing key file")
        sk = os.urandom(32)
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(sk.hex() + "\n")
        print("public key (install on customers):", ed25519.public_key(sk).hex())
    elif a.cmd == "issue":
        now = int(time.time())
        doc = {"v": 1, "lic": a.lic, "plan": "studio", "state": "trial" if a.trial else "active",
               "iat": now, "nbf": now - 60, "exp": int(now + a.days * 86400),
               "grace": int(min(a.grace_days, 7) * 86400)}
        if a.inst:
            doc["inst"] = a.inst
        print(entitlement.make_token(load_key(a.key), doc))
    else:
        print(entitlement.make_token(load_key(a.key), {"revoked": a.lic, "iat": int(time.time())}, prefix="BS1R"))


if __name__ == "__main__":
    main()
