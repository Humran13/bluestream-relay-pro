"""End-to-end Studio streaming against a REAL nginx-rtmp and REAL FFmpeg (Docker/Linux only).

Enabled with BS_DOCKER_E2E=1 (set by tools/docker/run-tests.sh). The Studio runtime is invoked the way
production does it: `python3 -m studio.cli argv|widgets` with PYTHONPATH pointing at a directory that
contains the `studio` package, publishing into the repo's real rtmp.conf.template layout.
This is a container result: it does NOT prove systemd, reboot, sandbox-under-systemd or VPS performance."""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENABLED = os.environ.get("BS_DOCKER_E2E") == "1" and shutil.which("nginx") and shutil.which("ffmpeg") \
    and sys.platform.startswith("linux")
NGINX_MODULE = "/usr/lib/nginx/modules/ngx_rtmp_module.so"

sys.path.insert(0, str(REPO))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_json(url, timeout=25):
    r = run(["ffprobe", "-v", "error", "-rw_timeout", "15000000", "-show_streams", "-show_format", "-of", "json", url],
            timeout=timeout)
    return json.loads(r.stdout or "{}") if r.returncode == 0 else {}


@unittest.skipUnless(ENABLED, "Docker E2E (BS_DOCKER_E2E=1, nginx, ffmpeg, Linux)")
class StudioE2E(unittest.TestCase):
    procs = []

    @classmethod
    def setUpClass(cls):
        from webapp.studio import ed25519, entitlement
        cls.tmp = tempfile.mkdtemp(prefix="studio-e2e-")
        t = cls.tmp
        for d in ("hls", "media", "run", "studio", "etc", "logs", "pylib", "nginx"):
            os.makedirs("%s/%s" % (t, d), exist_ok=True)
        os.symlink(str(REPO / "webapp" / "studio"), t + "/pylib/studio")     # same layout as /usr/local/lib/bluestream
        tpl = (REPO / "config" / "nginx" / "rtmp.conf.template").read_text()
        rtmp = tpl.replace("__RTMP_BIND__", "127.0.0.1").replace("__RTMP_PORT__", "1935").replace("__HLS_ROOT__", t + "/hls")
        rtmp = rtmp.replace("rtmp {", "rtmp {\n    access_log off;", 1)    # the rtmp module defaults to /var/log/nginx (root only)
        conf = ("load_module %s;\nworker_processes 1;\npid %s/nginx/nginx.pid;\nerror_log %s/logs/nginx-error.log info;\n"
                "events { worker_connections 256; }\n"
                "http { access_log off; client_body_temp_path %s/nginx/body; proxy_temp_path %s/nginx/proxy; "
                "fastcgi_temp_path %s/nginx/fcgi; uwsgi_temp_path %s/nginx/uwsgi; scgi_temp_path %s/nginx/scgi; }\n%s\n"
                % (NGINX_MODULE, t, t, t, t, t, t, t, rtmp))
        open(t + "/nginx/nginx.conf", "w").write(conf)
        if run(["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/1935"]).returncode == 0:
            raise RuntimeError("port 1935 is already in use (a stale nginx from an earlier run?) - refusing to test against it")
        cls.nginx = subprocess.Popen(["nginx", "-c", t + "/nginx/nginx.conf", "-g", "daemon off;"],
                                     stdout=subprocess.DEVNULL, stderr=open(t + "/logs/nginx.out", "w"),
                                     start_new_session=True)
        for _ in range(50):
            if run(["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/1935"]).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise unittest.SkipTest("nginx-rtmp did not start: " + open(t + "/logs/nginx.out").read()[-400:])
        sk = bytes(range(32))
        cls.env = dict(os.environ, PYTHONPATH=t + "/pylib", BLUESTREAM_STUDIO_DIR=t + "/studio", BLUESTREAM_RUN_DIR=t + "/run",
                       BLUESTREAM_MEDIA_DIR=t + "/media", BLUESTREAM_ETC_DIR=t + "/etc", BLUESTREAM_HLS_ROOT=t + "/hls",
                       BLUESTREAM_STUDIO_PUBKEY_HEX=ed25519.public_key(sk).hex(),
                       BLUESTREAM_STUDIO_CACHE=t + "/run/studio-cache", BLUESTREAM_STUDIO_MAX_LIVE="3")
        os.environ.update({k: v for k, v in cls.env.items() if k.startswith("BLUESTREAM_")})
        entitlement.install_token(entitlement.make_token(sk, {"v": 1, "lic": "e2e", "plan": "studio", "exp": time.time() + 86400}))
        ff = ["ffmpeg", "-v", "error", "-y"]
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=20", "-f", "lavfi", "-i",
                             "sine=frequency=440:duration=20", "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", "-ar", "44100", "-ac", "2", t + "/media/loop.mp4"], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-c:v", "libx264",
                             "-pix_fmt", "yuv420p", t + "/media/pip.mp4"], check=True)
        for i, color in enumerate(("red", "green", "blue")):
            subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=%s:size=1920x1080:duration=1" % color, "-frames:v", "1",
                                 "%s/media/s%d.png" % (t, i)], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=orange@0.8:size=200x200:duration=1,format=rgba", "-frames:v", "1",
                             t + "/media/logo.png"], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "sine=frequency=330:duration=60", "-c:a", "libmp3lame", t + "/media/song.mp3"], check=True)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            cls.kill(p)
        cls.kill(cls.nginx)
        keep = os.environ.get("BS_E2E_LOGS")                  # keep logs for diagnosis (copied out before cleanup)
        if keep:
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(cls.tmp + "/logs", keep, dirs_exist_ok=True)
            shutil.copy(cls.tmp + "/nginx/nginx.conf", keep + "/nginx.conf")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def kill(p):
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)                 # whole group: nginx master AND its worker
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=10)
            except (ProcessLookupError, PermissionError):
                p.kill()
                p.wait(timeout=10)

    def tearDown(self):
        for p in list(self.procs):
            self.kill(p)
        self.procs.clear()
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "studio.cli"], capture_output=True)
        subprocess.run(["pkill", "-u", str(os.getuid()), "-x", "ffmpeg"], capture_output=True)
        time.sleep(0.5)
        for f in Path(self.tmp + "/run").glob("studio-*.live"):          # leases of the processes we just stopped
            f.unlink()
        for f in Path(self.tmp + "/run").glob("studio-*.progress.port"):
            f.unlink()

    # ---- helpers -------------------------------------------------------------
    def publish(self, project, relay=None, wait_stream=True):
        """Save+publish the project, start the sidecar and the FFmpeg argv exactly as run-relay.sh does."""
        from webapp.studio import store
        relay = relay or project.name
        store.save_draft(project.validate())
        store.publish(project.name)
        side = subprocess.Popen([sys.executable, "-m", "studio.cli", "widgets", relay, project.name], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=open(self.tmp + "/logs/%s-side.log" % relay, "w"),
                                start_new_session=True)
        self.procs.append(side)
        for _ in range(40):                                   # sidecar writes the progress port once listening
            if os.path.exists("%s/run/studio-%s.progress.port" % (self.tmp, relay)):
                break
            time.sleep(0.25)
        a = subprocess.run([sys.executable, "-m", "studio.cli", "argv", relay, project.name], env=self.env,
                           capture_output=True, timeout=120)
        self.assertEqual(a.returncode, 0, a.stderr.decode()[-600:])
        argv = ["ffmpeg"] + [x.decode() for x in a.stdout.split(b"\0") if x]    # the CLI emits arguments only
        ff = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=open(self.tmp + "/logs/%s-ffmpeg.log" % relay, "w"),
                              start_new_session=True)
        self.procs.append(ff)
        if wait_stream:
            self.wait_stream(relay)
        return side, ff, argv

    def wait_stream(self, relay, timeout=45):
        m3u8 = "%s/hls/relay/%s/index.m3u8" % (self.tmp, relay)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(m3u8) and "#EXTINF" in open(m3u8).read():
                return
            time.sleep(0.5)
        log = self.tmp + "/logs/%s-ffmpeg.log" % relay
        tail = open(log).read()[-500:] if os.path.exists(log) else "(no ffmpeg log: stream is published by another process)"
        self.fail("no HLS output for %s; ffmpeg log: %s; nginx: %s" % (
            relay, tail, open(self.tmp + "/logs/nginx-error.log").read()[-400:]))

    def url(self, relay):
        return "rtmp://127.0.0.1:1935/bluestream-relay/" + relay

    def assert_profile(self, relay):
        info = probe_json(self.url(relay))
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["codec_name"], v["width"], v["height"]), ("h264", 1280, 720))
        self.assertEqual(v["pix_fmt"], "yuv420p")
        num, den = (int(x) for x in v["r_frame_rate"].split("/"))
        self.assertAlmostEqual(num / den, 30.0, delta=0.6)
        self.assertEqual((a["codec_name"], int(a["channels"])), ("aac", 2))
        return info

    def segment_gops(self, relay):
        segs = sorted(Path("%s/hls/relay/%s" % (self.tmp, relay)).glob("*.ts"), key=os.path.getmtime)
        self.assertTrue(segs, "no HLS segments")
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
                   "frame=pts_time", "-of", "default=nw=1:nk=1", str(segs[-2] if len(segs) > 1 else segs[-1])]).stdout.split()
        keys = [float(x) for x in out if x]
        return [b - a for a, b in zip(keys, keys[1:])]

    def frame_hash(self, relay, crop):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-rw_timeout", "15000000", "-i", self.url(relay), "-frames:v", "1",
                              "-vf", "crop=%s" % crop, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True,
                             timeout=40).stdout
        return hashlib.md5(raw).hexdigest()

    def health(self, relay):
        from webapp.studio import health
        return health.read_health(self.tmp + "/run", relay, max_age=60)

    def project(self, name, kind, ref, overlays=(), **kw):
        from webapp.studio.model import Project
        return Project(name, kind, ref, list(overlays), **kw)

    # ---- tests ---------------------------------------------------------------
    def test_01_live_overlays_720p30_h264_aac_with_moving_clock_and_countdown(self):
        from webapp.studio.model import Overlay
        p = self.project("liveov", "media", "loop.mp4", [
            Overlay("rect", x=0.0, y=0.85, w=1.0, h=0.15, color="#000000", opacity=0.6),
            Overlay("clock", x=0.05, y=0.05, w=0.3, h=0.12, size=64),
            Overlay("countdown", x=0.4, y=0.4, w=0.3, h=0.15, size=64, target="2099-01-01T00:00:00"),
            Overlay("ticker", x=0.0, y=0.9, w=1.0, h=0.08, size=36, text="studio e2e ticker "),
            Overlay("image", x=0.8, y=0.05, w=0.12, h=0.12, asset="logo.png")])
        _, ff, argv = self.publish(p)
        self.assertIn("libx264", argv)
        self.assert_profile("liveov")
        gaps = self.segment_gops("liveov")
        self.assertTrue(gaps and max(gaps) <= 2.2, "keyframe gaps %s" % gaps)
        a = self.frame_hash("liveov", "300:80:60:30")          # the clock region
        time.sleep(3.2)
        b = self.frame_hash("liveov", "300:80:60:30")
        self.assertNotEqual(a, b, "clock did not advance on the live stream")
        h = None
        for _ in range(20):
            h = self.health("liveov")
            if h.get("state") == "HEALTHY":
                break
            time.sleep(1)
        self.assertEqual(h.get("state"), "HEALTHY", h)
        self.assertGreater(h["frames"], 100)
        self.assertGreater(h["cpu_cores"] or 0, 0.05)
        self.assertGreater(h["rss_mb"] or 0, 20)
        self.kill(ff)

    def test_02_slideshow_prerender_then_copy_streams_all_slides(self):
        p = self.project("slides", "slideshow", "s0.png,s1.png,s2.png", slide_seconds=2)
        side, ff, argv = self.publish(p)
        self.assertIn("copy", argv)                           # cheap path: no encode on the stream
        self.assertNotIn("libx264", argv)
        self.assert_profile("slides")
        colors = set()
        for _ in range(4):
            raw = subprocess.run(["ffmpeg", "-v", "error", "-rw_timeout", "15000000", "-i", self.url("slides"), "-frames:v", "1",
                                  "-vf", "crop=2:2:10:10", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                 capture_output=True, timeout=40).stdout
            colors.add("rgb" if raw[0] > 200 and raw[1] < 60 else "g" if raw[1] > 100 and raw[0] < 60 else "b" if raw[2] > 150 else "?")
            time.sleep(2.2)
        self.assertGreaterEqual(len(colors - {"?"}), 2, "slideshow did not advance: %s" % colors)
        self.kill(ff)

    def test_03_audio_source_with_waveform(self):
        p = self.project("radio", "audio", "song.mp3", background="#102040", visualizer=True)
        _, ff, argv = self.publish(p)
        self.assertIn("showwaves", " ".join(argv))
        self.assert_profile("radio")
        self.kill(ff)

    def test_04_video_picture_in_picture_and_countdown(self):
        from webapp.studio.model import Overlay
        p = self.project("pip", "media", "loop.mp4", [Overlay("video", x=0.65, y=0.05, w=0.3, h=0.3, asset="pip.mp4"),
                                                      Overlay("pomodoro", x=0.05, y=0.8, w=0.4, h=0.12, size=48,
                                                              target="2026-01-01T00:00:00")])
        _, ff, _ = self.publish(p)
        self.assert_profile("pip")
        a = self.frame_hash("pip", "300:150:830:40")
        time.sleep(1.3)
        self.assertNotEqual(a, self.frame_hash("pip", "300:150:830:40"), "picture-in-picture video is frozen")
        self.kill(ff)

    def test_05_recovery_ffmpeg_crash_with_the_real_supervisor(self):
        """Real lib/supervise.sh loop around the Studio argv: kill -9 ffmpeg -> automatic recovery."""
        from webapp.studio.model import Overlay
        p = self.project("recov", "media", "loop.mp4", [Overlay("clock", x=0.05, y=0.05, w=0.3, h=0.12, size=64)])
        from webapp.studio import store
        store.save_draft(p.validate())
        store.publish("recov")
        env = dict(self.env, BS_REPO=str(REPO), BS_RELAY="recov", BS_PROJECT="recov")
        sup = subprocess.Popen(["bash", str(REPO / "tools" / "docker" / "supervise_harness.sh")], env=env,
                               stdout=open(self.tmp + "/logs/sup.out", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(sup)
        self.wait_stream("recov", 60)
        status = Path(self.tmp + "/run/relay-recov.status")
        self.assertIn("STATE=LIVE", status.read_text())

        def ffpid():
            out = run(["pgrep", "-f", "bluestream-relay/recov"]).stdout.split()
            return [int(x) for x in out if int(x) != os.getpid()]
        before = ffpid()
        self.assertTrue(before, "no studio ffmpeg process found")
        for pid in before:
            os.kill(pid, signal.SIGKILL)
        deadline = time.time() + 45
        recovered = False
        while time.time() < deadline:
            time.sleep(1)
            now = [x for x in ffpid() if x not in before]
            if now and "STATE=LIVE" in status.read_text():
                recovered = True
                break
        self.assertTrue(recovered, "supervisor did not restart ffmpeg: %s" % status.read_text())
        time.sleep(4)
        self.assert_profile("recov")                          # stream is back and valid
        # deliberate stop: SIGTERM -> STOPPED, no restart, no orphan
        os.killpg(sup.pid, signal.SIGTERM)
        sup.wait(timeout=20)
        time.sleep(1)
        self.assertIn("STATE=STOPPED", status.read_text())
        self.assertEqual(ffpid(), [], "ffmpeg survived a deliberate stop")

    def test_06_source_disconnection_and_return(self):
        """A Studio stream reading a Relay source survives the source disappearing and coming back."""
        src = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-stream_loop", "-1", "-i", self.tmp + "/media/loop.mp4", "-c", "copy",
                                "-f", "flv", self.url("srcrelay")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        self.procs.append(src)
        self.wait_stream("srcrelay", 40)
        from webapp.studio import store
        p = self.project("fromrelay", "relay", "srcrelay", [])
        from webapp.studio.model import Overlay
        p.overlays = [Overlay("text", x=0.05, y=0.05, w=0.4, h=0.1, text="relay source")]
        store.save_draft(p.validate())
        store.publish("fromrelay")
        env = dict(self.env, BS_REPO=str(REPO), BS_RELAY="fromrelay", BS_PROJECT="fromrelay")
        sup = subprocess.Popen(["bash", str(REPO / "tools" / "docker" / "supervise_harness.sh")], env=env,
                               stdout=open(self.tmp + "/logs/sup2.out", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(sup)
        self.wait_stream("fromrelay", 60)
        self.assert_profile("fromrelay")
        self.kill(src)                                        # source disappears
        time.sleep(6)
        status = Path(self.tmp + "/run/relay-fromrelay.status").read_text()
        self.assertTrue("RECONNECTING" in status or "STATE=LIVE" in status, status)
        src2 = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-stream_loop", "-1", "-i", self.tmp + "/media/loop.mp4", "-c", "copy",
                                 "-f", "flv", self.url("srcrelay")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        self.procs.append(src2)
        deadline = time.time() + 90
        ok = False
        while time.time() < deadline and not ok:
            time.sleep(3)
            ok = "STATE=LIVE" in Path(self.tmp + "/run/relay-fromrelay.status").read_text() and bool(probe_json(self.url("fromrelay")))
        self.assertTrue(ok, "Studio stream did not resume after the source returned")
        os.killpg(sup.pid, signal.SIGTERM)
        sup.wait(timeout=20)

    def test_07_missing_media_fails_closed_and_corrupt_media_never_crashes_the_runtime(self):
        from webapp.studio import store
        store.save_draft(self.project("gone", "media", "nope.mp4").validate())
        store.publish("gone")
        r = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "gone", "gone"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr.decode())
        self.assertEqual(r.stdout, b"")
        open(self.tmp + "/media/corrupt.mp4", "wb").write(b"not a video" * 100)
        store.save_draft(self.project("corrupt", "media", "corrupt.mp4").validate())
        store.publish("corrupt")
        r = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "corrupt", "corrupt"], env=self.env, capture_output=True, timeout=60)
        self.assertIn(r.returncode, (0, 2, 4))                # produces argv (ffmpeg will then fail+retry) or refuses; never a traceback
        self.assertNotIn(b"Traceback", r.stderr)

    def test_09_browser_gate_follows_whether_chromiums_sandbox_really_starts(self):
        """With the flag on and Chromium installed, usable() must equal the REAL sandbox self-test result.
        Under Docker's default seccomp the sandbox cannot start, so the feature must refuse (never fall back to --no-sandbox)."""
        chrome = os.environ.get("BLUESTREAM_BROWSER_BIN", "")
        if not chrome:
            found = subprocess.run("find /opt/pw -type f -name chrome -path '*chromium-*' ! -path '*headless*' | head -1",
                                   shell=True, capture_output=True, text=True).stdout.strip()
            chrome = found
        if not chrome:
            self.skipTest("no Chromium binary available")
        from webapp.studio import browsercap, store
        from webapp.studio.model import Overlay
        env = {"BLUESTREAM_STUDIO_BROWSER": "1", "BLUESTREAM_BROWSER_BIN": chrome}
        from unittest import mock
        with mock.patch.dict(os.environ, env):
            browsercap._usable_cache.update(at=0.0, val=None)
            works = browsercap.sandbox_works(chrome)
            ok, why = browsercap.usable()
            self.assertEqual(ok, works, why)
            if not works:
                self.assertIn("sandbox", why)
            store.save_draft(self.project("brgate", "media", "loop.mp4", [Overlay("browser", url="https://example.com/", refresh_s=60)]).validate())
            store.publish("brgate")
            r = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "brgate", "brgate"],
                               env=dict(self.env, **env), capture_output=True, timeout=120)
            if works:
                self.assertIn(r.returncode, (0, 3))       # allowed (or refused only by admission control)
            else:
                self.assertEqual(r.returncode, 2, r.stderr.decode())            # refused: no sandbox, no browser source
                self.assertIn(b"browser source unavailable", r.stderr)
            browsercap._usable_cache.update(at=0.0, val=None)

    def test_08_licence_revocation_blocks_the_runtime(self):
        from webapp.studio import ed25519, entitlement, store
        sk = bytes(range(32))
        store.save_draft(self.project("lic", "media", "loop.mp4").validate())
        store.publish("lic")
        ok = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "lic", "lic"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        rev = entitlement.make_token(sk, {"revoked": ["e2e"]}, prefix="BS1R")
        open(self.tmp + "/etc/studio-revocations.token", "w").write(rev)
        blocked = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "lic", "lic"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(blocked.returncode, 5)
        os.unlink(self.tmp + "/etc/studio-revocations.token")
        self.assertEqual(subprocess.run([sys.executable, "-m", "studio.cli", "argv", "lic", "lic"], env=self.env,
                                        capture_output=True, timeout=60).returncode, 0)
        del ed25519


if __name__ == "__main__":
    unittest.main()
