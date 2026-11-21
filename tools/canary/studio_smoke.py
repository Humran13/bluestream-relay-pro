#!/usr/bin/env python3
"""Post-deployment smoke test of a RUNNING BlueStream console (loopback only, read-only GETs plus one CSRF probe).

It needs no password: run as root, it reads the console's local secret key (never printed) and mints a short-lived
session cookie for the admin, exactly as the console itself would after a login. Nothing is created or changed:
every request is a GET, except one POST WITHOUT a CSRF token that must be rejected. Output has no secrets.

  python3 studio_smoke.py --port 8180 [--state-dir /var/lib/bluestream/web] [--expect-studio]
Exit 0 = all checks passed. The last line is a machine-readable summary (SMOKE_RESULT {...}).
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
from pathlib import Path

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


def request(port, method, path, cookie=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Host": "127.0.0.1"}
    if cookie:
        headers["Cookie"] = "session=" + cookie
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read(2_000_000)
        return r.status, dict(r.getheaders()), data.decode("utf-8", "replace")
    finally:
        conn.close()


def mint_cookie(state_dir):
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    key = (Path(state_dir) / "secret_key").read_text(encoding="utf-8").strip()
    app = Flask("smoke")
    app.secret_key = key
    ser = SecureCookieSessionInterface().get_signing_serializer(app)
    return ser.dumps({"authenticated": True, "username": "admin"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state-dir", default="/var/lib/bluestream/web")
    ap.add_argument("--expect-studio", action="store_true")
    a = ap.parse_args()
    p = a.port

    s, h, _ = request(p, "GET", "/console/login")
    check("login page responds", s == 200, "HTTP %d" % s)
    for path in ("/console/", "/console/studio", "/console/streams"):
        s, h, _ = request(p, "GET", path)
        check("anonymous %s is redirected to login" % path, s in (301, 302, 303) and "/console/login" in h.get("Location", ""), "HTTP %d" % s)
    s, h, _ = request(p, "POST", "/console/studio/new", body="name=x&source_kind=media&source_ref=x.mp4&template=blank")
    check("anonymous Studio write is refused", s in (302, 400, 401, 403), "HTTP %d" % s)

    try:
        cookie = mint_cookie(a.state_dir)
    except Exception as exc:  # noqa: BLE001 - report, never print secrets
        check("test session could be created", False, type(exc).__name__)
        return finish()
    for path, label in (("/console/", "dashboard"), ("/console/streams", "streams"), ("/console/destinations", "destinations"),
                        ("/console/media", "media library"), ("/console/playlists", "playlists")):
        s, _, body = request(p, "GET", path, cookie)
        check("existing %s page works with the existing session" % label, s == 200, "HTTP %d" % s)
        if path == "/console/":
            check("Studio tab is in the navigation", 'href="/console/studio"' in body)

    s, _, body = request(p, "GET", "/console/studio", cookie)
    if a.expect_studio:
        check("Studio page loads with the existing session", s == 200, "HTTP %d" % s)
    else:
        check("Studio page responds (feature may be off)", s in (200, 404), "HTTP %d" % s)
    text = re.sub(r"<[^>]+>", " ", body)
    if s == 200:
        if re.search(r"Licence\s+ACTIVE", text):
            lic = "ACTIVE"
        elif "verification key is not configured" in text:
            lic = "NO-VENDOR-KEY"
        elif re.search(r"Licence\s+NOT ACTIVE", text):
            lic = "NOT-ACTIVE"
        else:
            lic = "UNKNOWN"
        print("INFO  licence state: %s" % lic)
    for asset in ("/console/static/js/studio_editor.js", "/console/static/css/console.css"):
        s, h, _ = request(p, "GET", asset, cookie)
        check("asset %s loads" % asset.rsplit("/", 1)[-1], s == 200, "HTTP %d" % s)

    s, _, _ = request(p, "POST", "/console/studio/new", cookie, body="name=smoke&source_kind=media&source_ref=x.mp4&template=blank")
    check("CSRF protection rejects a write without a token", s == 400, "HTTP %d" % s)
    return finish()


def finish():
    ok = all(r for _, r in RESULTS)
    print("SMOKE_RESULT " + json.dumps({"passed": sum(1 for _, r in RESULTS if r), "failed": [n for n, r in RESULTS if not r]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
