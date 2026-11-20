#!/usr/bin/env python3
"""Throw-away Flask server for the Studio editor Playwright tests.

Uses the real console app with a fake engine bridge and a temp state directory,
a locally generated test licence key, and the Studio feature flag on. Nothing
here touches a real VPS, stream or credential.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="studio-e2e-")
os.environ.update({
    "BLUESTREAM_STUDIO_ENABLED": "1",
    "BLUESTREAM_STUDIO_DIR": tmp + "/studio",
    "BLUESTREAM_ETC_DIR": tmp + "/etc",
    "BLUESTREAM_MEDIA_DIR": tmp + "/media",
})
os.makedirs(tmp + "/etc")
os.makedirs(tmp + "/media")

from webapp.studio import ed25519, entitlement  # noqa: E402
from webapp.test_console import ADMIN_PASSWORD, FakeEngine, make_app  # noqa: E402

SK = bytes(range(32))
os.environ["BLUESTREAM_STUDIO_PUBKEY_HEX"] = ed25519.public_key(SK).hex()
entitlement.install_token(entitlement.make_token(
    SK, {"v": 1, "lic": "e2e", "plan": "studio", "exp": time.time() + 86400}))

payloads = {"relay_list": [], "playlist_list": [], "media_list": [{"name": "loop.mp4"}], "snapshot": {}}
app, _ = make_app(tmp, engine=FakeEngine(payloads))
print("studio e2e server ready; admin password:", ADMIN_PASSWORD, flush=True)
app.run(host="127.0.0.1", port=int(os.environ.get("STUDIO_E2E_PORT", "4874")), threaded=True)
