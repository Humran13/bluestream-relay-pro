#!/usr/bin/env python3
"""Throw-away Flask server for the Studio workspace Playwright tests.

Uses the real console app with a STATEFUL fake engine bridge (media library, streams, destinations) and a temp
state directory. Nothing here touches a real VPS, stream or credential. No licence is involved: Studio needs none.
"""
import os
import struct
import sys
import tempfile
import zlib
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
for d in ("etc", "media", "upload"):
    os.makedirs(tmp + "/" + d)

from webapp.test_console import ADMIN_PASSWORD, FakeEngine, make_app  # noqa: E402


def png(path, w, h, rgb):
    """A real, valid PNG (no external tools needed)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                 chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


png(tmp + "/media/logo.png", 200, 100, (220, 40, 40))      # 2:1
png(tmp + "/media/badge.png", 100, 100, (40, 120, 220))    # 1:1
png(tmp + "/media/banner.png", 400, 100, (40, 200, 90))    # 4:1
import shutil  # noqa: E402

FIX = ROOT / "tests" / "browser" / "fixtures"
shutil.copy(FIX / "motion.mp4", tmp + "/media/motion.mp4")    # a real, moving H.264 clip (testsrc2)
for name in ("loop.mp4", "intro.webm", "song.mp3"):         # placeholders: listed, not decoded here
    with open(tmp + "/media/" + name, "wb") as fh:
        fh.write(b"\x00" * 1024)


class E2EEngine(FakeEngine):
    """Stateful: uploads land in the library, streams can be created/started/stopped."""

    def __init__(self, payloads):
        super().__init__(payloads)
        self.healths = {}

    def media_import_staged(self, staging):
        src = tmp + "/upload/" + staging
        os.replace(src, tmp + "/media/" + staging)
        self.payloads["media_list"].append({"name": staging, "size": os.path.getsize(tmp + "/media/" + staging)})
        return self._mutation("media_import_staged", staging)

    def _relay(self, name):
        return next((r for r in self.payloads["relay_list"] if r["name"] == name), None)

    def relay_create_studio(self, name, project):
        self.payloads["relay_list"].append({"name": name, "type": "studio", "active": "no", "health": "STOPPED", "health_detail": ""})
        return self._mutation("relay_create_studio", name, project)

    def relay_start(self, name):
        self._relay(name).update(active="yes", health="HEALTHY", health_detail="hls fresh")
        return self._mutation("relay_start", name)

    def relay_stop(self, name):
        self._relay(name).update(active="no", health="STOPPED", health_detail="")
        return self._mutation("relay_stop", name)

    def relay_restart(self, name):
        return self._mutation("relay_restart", name)

    def relay_destinations_get(self, name):
        return list(self.payloads.get(("dest", name), []))

    def relay_destinations_set(self, name, ids):
        self.payloads[("dest", name)] = list(ids)
        return self._mutation("relay_destinations_set", name, tuple(ids))

    def read(self, op, *values):
        if op == "studio_health":
            from webapp.studio import store
            name = values[0]
            try:
                pub = store.load_published(name)
                rev, plan = pub.revision, ("live-composition" if pub.runs_live_encode else "prerender-then-copy")
            except Exception:
                rev, plan = 0, ""
            if name.startswith("ws-fail-"):
                rev = 1                      # simulates a restart that never brings the NEW revision up (drives the rollback test)
            return {"state": "HEALTHY", "detail": "encoding at real time", "speed": "1.01", "fps": "30", "cpu_cores": "1.2",
                    "rss_mb": "300", "dropped_frames": "0", "restarts": "0", "revision": str(rev), "plan": plan, "pending_restart": ""}
        if op == "relay_publisher_status":
            return [{"id": d, "display_name": d, "state": "LIVE", "detail": "publishing"} for d in self.payloads.get(("dest", values[0]), [])]
        raise KeyError(op)


payloads = {
    "relay_list": [{"name": "dino", "type": "local-file", "active": "yes", "health": "HEALTHY", "health_detail": "hls fresh"},
                   {"name": "sleepy", "type": "url", "active": "no", "health": "STOPPED", "health_detail": ""},
                   {"name": "ghost", "type": "url", "active": "yes", "health": "HEALTHY", "health_detail": ""}],
    "playlist_list": [{"name": "promo", "active": "yes", "health": "HEALTHY"}],
    "snapshot": {"domain": "127.0.0.1:4874", "https_configured": "no"},
    "media_list": [{"name": "logo.png", "size": 2048}, {"name": "badge.png", "size": 1024}, {"name": "banner.png", "size": 4096},
                   {"name": "loop.mp4", "size": 5 * 1024 * 1024}, {"name": "motion.mp4", "size": 157100}, {"name": "intro.webm", "size": 900000}, {"name": "song.mp3", "size": 3000000}],
    "destination_list": [{"name": "yt-main", "display_name": "YouTube main", "platform": "youtube", "server_url": "rtmps://a.rtmps.youtube.com/live2/<redacted>",
                          "enabled": "yes", "has_stream_key": "yes", "attached_count": "0"}],
}
app, state_dir = make_app(tmp, engine=E2EEngine(payloads), config={"WEB_UPLOAD_DIR": tmp + "/upload", "DISK_RESERVE_BYTES": 0})
# Public HLS exactly like nginx serves it (no login). "ghost" has no files -> 404, to test the disconnected state.
from flask import abort, send_from_directory  # noqa: E402


@app.route("/hls/<kind>/<name>/<path:f>")
def hls(kind, name, f):
    if name == "ghost":
        abort(404)
    return send_from_directory(str(FIX / "hls"), f)


if not shutil.which("ffmpeg"):
    # No FFmpeg on this machine (dev box): serve a prebuilt fragmented MP4 so the BROWSER side of the preview is still exercised.
    # The real proxy (copy + transcode) is verified against real FFmpeg in test_studio_live.py.
    from webapp.studio import preview  # noqa: E402

    class _Clip:
        def __init__(self):
            self.stdout = open(str(FIX / "motion_frag.mp4"), "rb")

        def kill(self):
            self.stdout.close()

        def wait(self, timeout=None):
            return 0
    preview.open_proxy = lambda asset: (_Clip(), "copy") if asset == "motion.mp4" else None

print("studio e2e server ready; admin password:", ADMIN_PASSWORD, "state:", state_dir, flush=True)
app.run(host="127.0.0.1", port=int(os.environ.get("STUDIO_E2E_PORT", "4874")), threaded=True)
