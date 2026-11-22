"""Live-production backend: hot vs staged apply (+ rollback), encoder selection, on-demand video preview, existing
relay/playlist monitoring, playlist creation, layer fields, restart forensics script guard."""
import dataclasses
import io
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from webapp.engine import EngineClient, EngineError
from webapp.studio import capabilities, ffmpeg, preview, store
from webapp.studio.model import OutputProfile, Overlay, Project, ValidationError
from webapp.test_studio_workspace import WS

ROOT = Path(__file__).resolve().parent.parent


class StructureTests(unittest.TestCase):
    def fp(self, *ov, **kw):
        return Project("p", kw.get("kind", "media"), kw.get("ref", "a.mp4"), list(ov)).structure_fingerprint()

    def test_text_content_is_not_structure_but_geometry_style_and_media_are(self):
        base = self.fp(Overlay("text", text="a"), Overlay("clock"), Overlay("countdown", target="2099-01-01T00:00:00"))
        same = self.fp(Overlay("text", text="CHANGED"), Overlay("clock", tz="Asia/Tokyo"), Overlay("countdown", target="2100-01-01T00:00:00", finished_text="GO"))
        self.assertEqual(base, same)
        for changed in (Overlay("text", text="a", x=0.2), Overlay("text", text="a", size=60), Overlay("text", text="a", color="#ff0000"),
                        Overlay("text", text="a", opacity=0.5), Overlay("text", text="a", align="center"), Overlay("text", text="a", font="DejaVu Serif"),
                        Overlay("text", text="a", bg_opacity=0.5), Overlay("text", text="a", visible=False)):
            self.assertNotEqual(self.fp(Overlay("text", text="a")), self.fp(changed), changed)
        self.assertNotEqual(self.fp(Overlay("image", asset="a.png")), self.fp(Overlay("image", asset="b.png")))
        self.assertNotEqual(self.fp(), self.fp(ref="b.mp4"))

    def test_layer_name_and_lock_never_count(self):
        self.assertEqual(self.fp(Overlay("text")), self.fp(Overlay("text", label="Title", locked=True)))

    def test_live_encode_detection(self):
        P = lambda *o, **k: Project("p", k.get("kind", "media"), "a", list(o))   # noqa: E731
        self.assertTrue(P(Overlay("clock")).runs_live_encode)
        self.assertFalse(P(Overlay("text")).runs_live_encode)                      # static: baked into a pre-render
        self.assertFalse(P().runs_live_encode)
        self.assertTrue(P(Overlay("text"), kind="relay").runs_live_encode)         # a live source with overlays is always a live encode
        self.assertFalse(P(kind="relay").runs_live_encode)

    def test_new_layer_fields_are_validated(self):
        Overlay("text", align="center", bg_color="#112233", bg_opacity=0.5, locked=True).validate()
        for bad in (Overlay("text", align="justify"), Overlay("text", bg_color="red"), Overlay("text", bg_opacity=2)):
            with self.assertRaises(ValidationError):
                bad.validate()
        with self.assertRaises(ValidationError):
            OutputProfile(encoder="vp9").validate()


class EncoderArgTests(unittest.TestCase):
    def args(self, **prof):
        p = Project("g", "media", "a.mp4", [Overlay("clock")], OutputProfile(**prof)).validate()
        a = ffmpeg.build_live_args(p, "/media/a.mp4", "rtmp://127.0.0.1:1935/bluestream-relay/g", "/run/studio-g.work", "/media")
        return a

    def test_software_is_the_default_and_nvenc_uses_the_gpu_encoder_with_cbr(self):
        sw = self.args()
        self.assertEqual(sw[sw.index("-c:v") + 1], "libx264")
        nv = self.args(encoder="h264_nvenc")
        self.assertEqual(nv[nv.index("-c:v") + 1], "h264_nvenc")
        self.assertIn("cbr", nv)
        self.assertNotIn("veryfast", nv)
        self.assertEqual(nv[nv.index("-c:a") + 1], "aac")

    def test_font_alignment_and_background_box_reach_the_filter_graph(self):
        o = Overlay("text", text="x", x=0.1, w=0.5, align="center", bg_color="#102030", bg_opacity=0.4, font="DejaVu Sans")
        p = Project("g", "media", "a.mp4", [o]).validate()
        a = ffmpeg.build_prerender_args(p, "/media/a.mp4", "/cache/o.mp4", "/run/studio-g.work", "/media")
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("(640-text_w)/2", g)                        # centred inside the 50% wide box (640 px of 1280)
        self.assertIn("box=1:boxcolor=0x102030@0.40", g)
        plain = Project("g", "media", "a.mp4", [Overlay("text", text="x")]).validate()
        g2 = ffmpeg.build_prerender_args(plain, "/media/a.mp4", "/cache/o.mp4", "/run/studio-g.work", "/media")
        self.assertNotIn("box=1", " ".join(g2))


class PublishModeTests(WS):
    """Apply to Live: hot (no interruption) vs staged restart, with explicit confirmation and rollback."""

    def running(self, name="ws"):
        self.payloads["relay_list"] = [{"name": name, "type": "studio", "active": "yes", "health": "HEALTHY"}]

    def apply(self, body=None):
        return self.jpost("/console/studio/ws/publish.json", body or {})

    def edit(self, **ov):
        p = store.load_draft("ws")
        p.overlays = [Overlay(**dict(dict(kind="clock", x=0.1, y=0.1, w=0.3, h=0.1), **ov)), Overlay("text", text="hello", x=0.1, y=0.3, w=0.4, h=0.1)]
        store.save_draft(p.validate())

    def setUp(self):
        super().setUp()
        self.project()
        self.edit()
        store.publish("ws")

    def test_content_only_change_on_a_running_live_composition_is_hot_and_never_restarts(self):
        self.running()
        p = store.load_draft("ws")
        p.overlays[1] = dataclasses.replace(p.overlays[1], text="A NEW HEADLINE")
        p.overlays[0] = dataclasses.replace(p.overlays[0], tz="Asia/Tokyo")
        store.save_draft(p)
        d = self.apply().get_json()
        self.assertTrue(d["ok"])
        self.assertEqual((d["mode"], d["restarted"]), ("hot", False))
        self.assertIn("no interruption", d["message"])
        self.assertEqual(self.engine.mutation_calls, [])                     # nothing touched: no restart, no start, no stop
        self.assertEqual(store.load_published("ws").overlays[1].text, "A NEW HEADLINE")

    def test_structural_change_needs_explicit_confirmation_then_restarts_only_this_stream(self):
        self.running()
        self.edit(x=0.5)                                                     # moved a layer: the filter graph changes
        r = self.apply()
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_restart"])
        self.assertEqual(self.engine.mutation_calls, [])
        d = self.apply({"restart": True}).get_json()
        self.assertEqual((d["mode"], d["previous"]), ("restart", 1))
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "ws")])

    def test_static_text_change_is_not_hot_because_it_is_baked_into_a_pre_render(self):
        p = Project("ws", "media", "loop.mp4", [Overlay("text", text="one")]).validate()
        store.save_draft(p)
        store.publish("ws")
        self.running()
        p.overlays[0] = dataclasses.replace(p.overlays[0], text="two")
        store.save_draft(p)
        self.assertEqual(self.apply().status_code, 409)

    def test_when_the_stream_is_stopped_apply_only_stores_the_revision(self):
        self.edit(x=0.6)
        d = self.apply().get_json()
        self.assertEqual(d["mode"], "stored")
        self.assertEqual(self.engine.mutation_calls, [])

    def test_unusable_encoder_blocks_a_restart_that_would_fail(self):
        self.running()
        p = store.load_draft("ws")
        p.profile = OutputProfile(encoder="h264_nvenc")
        p.overlays[0] = dataclasses.replace(p.overlays[0], x=0.4)
        store.save_draft(p)
        with mock.patch.object(capabilities, "encoder_usable", return_value=(False, "No NVIDIA GPU")):
            r = self.apply({"restart": True})
        self.assertEqual(r.status_code, 422)
        self.assertIn("No NVIDIA GPU", r.get_json()["error"])
        self.assertEqual(self.engine.mutation_calls, [])

    def test_revert_live_puts_the_earlier_revision_back_and_restarts_only_that_stream(self):
        self.edit(x=0.7)
        self.assertEqual(self.apply().get_json()["revision"], 2)
        self.running()
        d = self.jpost("/console/studio/ws/revert-live.json", {"revision": 1}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["revision"], 3)
        self.assertEqual(store.load_published("ws").overlays[0].x, 0.1)       # revision 1's layout is live again, as revision 3
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "ws")])
        self.assertEqual(self.jpost("/console/studio/ws/revert-live.json", {"revision": 99}).status_code, 400)

    def test_encoder_can_only_be_saved_when_it_really_works(self):
        with mock.patch.object(capabilities, "encoder_usable", return_value=(False, "no GPU")):
            r = self.jpost("/console/studio/ws/save.json", {"project": {"profile": {"encoder": "h264_nvenc"}}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(store.load_draft("ws").profile.encoder, "libx264")
        with mock.patch.object(capabilities, "encoder_usable", return_value=(True, "")):
            r = self.jpost("/console/studio/ws/save.json", {"project": {"profile": {"encoder": "h264_nvenc"}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.load_draft("ws").profile.encoder, "h264_nvenc")

    def test_status_exposes_the_running_revision_plan_and_pending_restart(self):
        self.running()
        self.engine.read = lambda op, *v: {"studio_health": {"state": "HEALTHY", "revision": "1", "plan": "live-composition", "pending_restart": "yes"},
                                           "relay_publisher_status": []}[op]
        s = self.client.get("/console/studio/ws/status.json").get_json()["stream"]
        self.assertEqual((s["revision"], s["plan"], s["pending_restart"]), ("1", "live-composition", True))


class MonitorTests(WS):
    def setUp(self):
        super().setUp()
        self.payloads["snapshot"] = {"domain": "tv.example.com", "https_configured": "yes"}
        self.payloads["relay_list"] = [{"name": "dino", "type": "local-file", "active": "yes", "health": "HEALTHY"},
                                       {"name": "old", "type": "url", "active": "no", "health": "STOPPED"},
                                       {"name": "ws", "type": "studio", "active": "yes", "health": "HEALTHY"}]
        self.payloads["playlist_list"] = [{"name": "promo", "active": "yes", "health": "HEALTHY"}]

    def test_sources_lists_existing_relays_and_playlists_with_public_hls_but_never_studio_streams(self):
        d = self.client.get("/console/studio/sources.json").get_json()
        self.assertEqual([r["name"] for r in d["relays"]], ["dino", "old"])
        dino = d["relays"][0]
        self.assertEqual((dino["active"], dino["hls_url"]), (True, "https://tv.example.com/hls/relay/dino/index.m3u8"))
        self.assertEqual(d["playlists"][0]["hls_url"], "https://tv.example.com/hls/playlist/promo/index.m3u8")
        self.assertEqual(self.app.test_client().get("/console/studio/sources.json").status_code, 401)

    def test_monitoring_a_relay_never_calls_a_mutating_operation(self):
        self.project("m1", ref="loop.mp4")
        p = Project("m2", "relay", "dino", []).validate()
        store.save_draft(p)
        d = self.client.get("/console/studio/m2/monitor.json").get_json()
        self.assertEqual((d["source"]["mode"], d["source"]["running"]), ("hls", True))
        self.assertTrue(d["source"]["url"].endswith("/hls/relay/dino/index.m3u8"))
        self.client.get("/console/studio/sources.json")
        self.assertEqual(self.engine.mutation_calls, [])                       # read-only: Dino is never touched

    def test_media_projects_play_through_the_on_demand_preview_and_stopped_sources_say_so(self):
        self.project("m1")
        d = self.client.get("/console/studio/m1/monitor.json").get_json()
        self.assertEqual(d["source"]["mode"], "file")
        self.assertIn("/studio/media/loop.mp4/play.mp4", d["source"]["url"])
        store.save_draft(Project("m3", "relay", "old", []).validate())
        d = self.client.get("/console/studio/m3/monitor.json").get_json()
        self.assertFalse(d["source"]["running"])
        self.assertIn("stopped", d["source"]["message"])

    def test_the_studio_output_is_monitorable_once_its_stream_exists(self):
        self.project("ws")
        d = self.client.get("/console/studio/ws/monitor.json").get_json()
        self.assertEqual(d["output"], {"exists": True, "running": True, "url": "https://tv.example.com/hls/relay/ws/index.m3u8"})

    def test_playlist_creation_uses_the_existing_engine_and_validates(self):
        url = "/console/studio/playlist/create.json"
        ok = self.jpost(url, {"name": "promo2", "items": ["loop.mp4", "intro.webm"]})
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertEqual(self.engine.mutation_calls, [("playlist_create", "promo2", "loop.mp4", "intro.webm")])
        for bad in ({"name": "x", "items": ["loop.mp4"]}, {"name": "x", "items": ["loop.mp4", "loop.mp4"]}, {"name": "Bad Name", "items": ["loop.mp4", "intro.webm"]},
                    {"name": "x", "items": ["loop.mp4", "logo.png"]}, {"name": "x", "items": ["loop.mp4", "ghost.mp4"]}, {"name": "x", "items": "loop.mp4"}):
            self.assertEqual(self.jpost(url, bad).status_code, 400, bad)
        self.assertEqual(len(self.engine.mutation_calls), 1)


class VideoPreviewTests(WS):
    def test_modes_and_args(self):
        self.assertEqual(preview.proxy_mode({"video": "h264", "pix_fmt": "yuv420p", "audio": "aac"}), "copy")
        self.assertEqual(preview.proxy_mode({"video": "h264", "pix_fmt": "yuv420p", "audio": None}), "copy")
        for c in ({"video": "hevc", "pix_fmt": "yuv420p", "audio": "aac"}, {"video": "h264", "pix_fmt": "yuv444p", "audio": "aac"},
                  {"video": "h264", "pix_fmt": "yuv420p", "audio": "ac3"}, {"video": "vp9", "pix_fmt": "yuv420p", "audio": None}, None):
            self.assertEqual(preview.proxy_mode(c), "transcode", c)
        cp = preview.proxy_args("/m/a.mp4", "copy")
        self.assertIn("copy", cp)
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", cp)
        self.assertNotIn("libx264", cp)
        tc = preview.proxy_args("/m/a.mp4", "transcode")
        self.assertIn("ultrafast", tc)
        self.assertIn("scale=-2:540", tc)

    def test_route_is_authenticated_validated_and_streams_video_mp4(self):
        self.assertEqual(self.app.test_client().get("/console/studio/media/loop.mp4/play.mp4").status_code, 401)
        for bad in ("..%2Fx.mp4", "logo.png", "song.mp3", "notes.txt"):
            self.assertEqual(self.client.get("/console/studio/media/%s/play.mp4" % bad).status_code, 404, bad)

        class Proc:
            def __init__(self):
                self.stdout, self.killed = io.BytesIO(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100000), False
            def kill(self):
                self.killed = True
            def wait(self, timeout=None):
                return 0
        proc = Proc()
        with mock.patch.object(preview, "open_proxy", return_value=(proc, "copy")):
            r = self.client.get("/console/studio/media/loop.mp4/play.mp4")
            body = r.get_data()
        self.assertEqual((r.status_code, r.mimetype), (200, "video/mp4"))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertTrue(body.startswith(b"\x00\x00\x00\x18ftyp"))
        self.assertTrue(proc.killed)                                          # the process never outlives the request

    def test_missing_file_is_a_clean_404_and_concurrency_is_capped(self):
        with mock.patch.object(preview, "open_proxy", return_value=None):
            self.assertEqual(self.client.get("/console/studio/media/loop.mp4/play.mp4").status_code, 404)

        # no free preview slot: a clear 429, never an unbounded number of FFmpeg processes
        import tempfile
        from webapp.test_console import login, make_app
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_PREVIEW_SLOTS": "0"}):
            app2, _ = make_app(d, engine=self.engine)
            c2 = app2.test_client()
            login(c2)
            self.assertEqual(c2.get("/console/studio/media/loop.mp4/play.mp4").status_code, 429)

    def test_bridge_argv_is_validated_and_never_a_shell(self):
        c = EngineClient(production=True)
        argv = c.proxy_argv("loop.mp4")
        self.assertEqual(argv[-2:], ["studio_proxy", "loop.mp4"])
        self.assertEqual(argv[1], "-n")
        for bad in ("../x.mp4", "a b.mp4", "", ".hidden.mp4", "a;b.mp4"):
            with self.assertRaises(EngineError):
                c.proxy_argv(bad)
        webctl = (ROOT / "web-ctl").read_text(encoding="utf-8")
        self.assertIn("studio_proxy)", webctl)
        self.assertRegex(webctl, r'bs_valid_media_name "\$name" \|\| \{ printf .invalid media name')


import shutil as _sh  # noqa: E402
import tempfile  # noqa: E402


@unittest.skipUnless(_sh.which("ffmpeg") and _sh.which("ffprobe") and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "needs ffmpeg + a non-root Linux user (UNTESTED on this machine, not passed)")
class VideoPreviewRealTests(unittest.TestCase):
    """The editor's on-demand preview against REAL FFmpeg: the bytes served to the browser are a playable fragmented MP4 with moving video."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(prefix="studio-play-")
        cls._old = os.environ.get("BLUESTREAM_MEDIA_DIR")
        os.environ["BLUESTREAM_MEDIA_DIR"] = cls.d
        def mk(name, *codec):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3"] + list(codec) + [os.path.join(cls.d, name)],
                           check=True, capture_output=True)
        mk("h264.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p")
        mk("vp9.webm", "-c:v", "libvpx-vp9", "-b:v", "300k")
        mk("hevc.mkv", "-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=none")

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("BLUESTREAM_MEDIA_DIR", None)
        else:
            os.environ["BLUESTREAM_MEDIA_DIR"] = cls._old
        _sh.rmtree(cls.d, ignore_errors=True)

    def served(self, name):
        started = preview.open_proxy(name)
        self.assertIsNotNone(started, name)
        proc, mode = started
        data = proc.stdout.read()
        proc.wait()
        return mode, data

    def inspect(self, data):
        path = os.path.join(self.d, "out.mp4")
        with open(path, "wb") as fh:
            fh.write(data)
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=codec_name,height,nb_read_frames",
                            "-of", "json", path], capture_output=True, text=True)
        st = json.loads(r.stdout)["streams"][0]
        f = [subprocess.run(["ffmpeg", "-v", "error", "-ss", t, "-i", path, "-frames:v", "1", "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"],
                            capture_output=True).stdout for t in ("0.3", "2.0")]
        moved = sum(1 for a, b in zip(*f) if abs(a - b) > 20)
        return st, moved

    def test_browser_compatible_h264_is_copied_not_re_encoded(self):
        mode, data = self.served("h264.mp4")
        self.assertEqual(mode, "copy")
        st, moved = self.inspect(data)
        self.assertEqual((st["codec_name"], int(st["height"])), ("h264", 360))   # untouched resolution: a pure remux
        self.assertGreater(int(st["nb_read_frames"]), 60)
        self.assertGreater(moved, 100)                                           # the picture really moves

    def test_other_codecs_get_one_light_540p_h264_transcode(self):
        for name in ("vp9.webm", "hevc.mkv"):
            mode, data = self.served(name)
            self.assertEqual(mode, "transcode", name)
            st, moved = self.inspect(data)
            self.assertEqual((st["codec_name"], int(st["height"])), ("h264", 540), name)
            self.assertGreater(moved, 100, name)

    def test_unknown_and_non_video_files_are_refused(self):
        self.assertIsNone(preview.open_proxy("missing.mp4"))
        self.assertIsNone(preview.open_proxy("notes.txt"))


class ForensicsScriptTests(unittest.TestCase):
    def test_the_restart_forensics_script_is_read_only(self):
        text = (ROOT / "tools" / "canary" / "restart_forensics.sh").read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        for bad in ("systemctl restart", "systemctl stop", "systemctl start", "systemctl kill", "systemctl daemon", "rm ", "kill ", "tee ",
                    "chmod", "chown", " > /", "curl", "wget", "docker"):
            self.assertNotIn(bad, code, bad)
        self.assertIn("redact", code)
        import shutil
        bash = shutil.which("bash")
        if bash:
            r = subprocess.run([bash, "-n", str(ROOT / "tools" / "canary" / "restart_forensics.sh")], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
