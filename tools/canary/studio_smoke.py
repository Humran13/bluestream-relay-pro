#!/usr/bin/env python3
"""Post-deployment smoke test of a RUNNING BlueStream console (loopback only).

It needs no password: run as root, it reads the console's local secret key (never printed) and mints a short-lived session
cookie for the admin, exactly as the console itself would after a login. Checks: anonymous refusal, every existing page,
the Studio tab/page/assets, CSRF rejection, and (with --roundtrip) a real Studio project create -> save -> publish -> delete
round trip inside the running, sandboxed console, which proves persistence/permissions. The test project is always deleted.
Output contains no secrets.

  python3 studio_smoke.py --port 8180 [--state-dir /var/lib/bluestream/web] [--expect-studio] [--roundtrip]
Exit 0 = all checks passed. The last line is machine-readable (SMOKE_RESULT {...}).
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import secrets
import sys
from pathlib import Path

RESULTS = []
CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))
    return ok


class Client:
    def __init__(self, port, cookie=None):
        self.port, self.cookie = port, cookie

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": "127.0.0.1"}
        if self.cookie:
            headers["Cookie"] = "session=" + self.cookie
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            conn.request(method, path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read(2_000_000).decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            for k, v in r.getheaders():
                if k.lower() == "set-cookie":
                    m = re.match(r"session=([^;]*)", v)
                    if m and self.cookie is not None:
                        self.cookie = m.group(1)
            return r.status, hdrs, data
        finally:
            conn.close()

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, fields, page):
        from urllib.parse import urlencode
        _, _, html = self.get(page)
        m = CSRF_RE.search(html)
        data = dict(fields)
        data["csrf_token"] = m.group(1) if m else ""
        return self.request("POST", path, urlencode(data))


def mint_cookie(state_dir):
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface
    key = (Path(state_dir) / "secret_key").read_text(encoding="utf-8").strip()
    app = Flask("smoke")
    app.secret_key = key
    return SecureCookieSessionInterface().get_signing_serializer(app).dumps({"authenticated": True, "username": "admin"})


def roundtrip(c):
    name = "zz-smoke-" + secrets.token_hex(3)
    created = False
    try:
        s, h, _ = c.post("/console/studio/new", {"name": name, "source_kind": "media", "source_ref": "smoke-test.mp4",
                                                 "template": "blank"}, "/console/studio")
        created = s == 302
        check("Studio: create project", created, "HTTP %d" % s)
        s, _, body = c.get("/console/studio/" + name)
        check("Studio: editor opens and the project persisted", s == 200 and name in body, "HTTP %d" % s)
        s, _, _ = c.post("/console/studio/%s/save" % name, {"count": "0", "action": "add:ticker", "source_ref": "smoke-test.mp4"},
                         "/console/studio/" + name)
        check("Studio: edit and save (add an overlay)", s == 302, "HTTP %d" % s)
        s, _, body = c.get("/console/studio/" + name)
        check("Studio: saved overlay is shown after reload", s == 200 and "ticker" in body.lower())
        s, _, _ = c.post("/console/studio/%s/publish" % name, {}, "/console/studio/" + name)
        check("Studio: publish (Apply to Live)", s == 302, "HTTP %d" % s)
        s, _, body = c.get("/console/studio/" + name)
        check("Studio: published revision recorded", s == 200 and re.search(r"[Rr]evision\s*1|Live revision", body) is not None)
    finally:
        if created:
            s, _, _ = c.post("/console/studio/%s/delete" % name, {}, "/console/studio/" + name)
            s2, _, _ = c.get("/console/studio/" + name)
            check("Studio: test project removed again", s == 302 and s2 in (302, 404), "HTTP %d/%d" % (s, s2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--state-dir", default="/var/lib/bluestream/web")
    ap.add_argument("--expect-studio", action="store_true")
    ap.add_argument("--roundtrip", action="store_true")
    a = ap.parse_args()
    anon = Client(a.port)

    s, h, _ = anon.get("/console/login")
    check("login page responds", s == 200, "HTTP %d" % s)
    for path in ("/console/", "/console/studio", "/console/streams"):
        s, h, _ = anon.get(path)
        check("anonymous %s is redirected to login" % path, s in (301, 302, 303) and "/console/login" in h.get("location", ""), "HTTP %d" % s)
    s, h, _ = anon.request("POST", "/console/studio/new", "name=x&source_kind=media&source_ref=x.mp4&template=blank")
    check("anonymous Studio write is refused", s in (302, 400, 401, 403), "HTTP %d" % s)

    try:
        c = Client(a.port, mint_cookie(a.state_dir))
    except Exception as exc:  # noqa: BLE001 - report, never print secrets
        check("test session could be created", False, type(exc).__name__)
        return finish()
    for path, label in (("/console/", "dashboard"), ("/console/streams", "streams"), ("/console/destinations", "destinations"),
                        ("/console/media", "media library"), ("/console/playlists", "playlists")):
        s, _, body = c.get(path)
        check("existing %s page works with the existing session" % label, s == 200, "HTTP %d" % s)
        if path == "/console/":
            check("Studio tab is in the navigation", 'href="/console/studio"' in body)

    s, _, body = c.get("/console/studio")
    if a.expect_studio:
        check("Studio page loads with the existing session", s == 200, "HTTP %d" % s)
    else:
        check("Studio page responds (feature may be off)", s in (200, 404), "HTTP %d" % s)
    if s == 200:
        text = re.sub(r"<[^>]+>", " ", body)
        needs = "Paste licence key" in body or re.search(r"NOT ACTIVE", text) is not None
        check("Studio needs no licence or payment", not needs)
    for asset in ("/console/static/js/studio_editor.js", "/console/static/css/console.css"):
        s, _, _ = c.get(asset)
        check("asset %s loads" % asset.rsplit("/", 1)[-1], s == 200, "HTTP %d" % s)

    s, _, _ = c.request("POST", "/console/studio/new", "name=smoke&source_kind=media&source_ref=x.mp4&template=blank")
    check("CSRF protection rejects a write without a token", s == 400, "HTTP %d" % s)
    if a.roundtrip and a.expect_studio:
        roundtrip(c)
    return finish()


def finish():
    ok = all(r for _, r in RESULTS)
    print("SMOKE_RESULT " + json.dumps({"passed": sum(1 for _, r in RESULTS if r), "failed": [n for n, r in RESULTS if not r]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
