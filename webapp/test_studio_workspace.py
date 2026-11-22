"""Studio workspace backend: media browser + uploads, strict JSON save/apply, stream controls,
destinations, workspace settings, capacity checks and the FFmpeg scaling option."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from webapp.studio import capabilities, ffmpeg, media, preview, settings, store
from webapp.studio.model import ASSET_RE, OutputProfile, Overlay, Project, ValidationError
from webapp.test_studio_console import Base, SNAP, StudioEngine

MEDIA = [{"name": "loop.mp4", "size": 5 * 1024 * 1024}, {"name": "logo.png", "size": 2048},
         {"name": "song.mp3", "size": 3000000}, {"name": "intro.webm", "size": 900}, {"name": "notes.txt", "size": 5},
         {"name": ".hidden.png", "size": 1}]


class WS(Base):
    """A Studio console with a media library and an upload staging directory."""

    def setUp(self):
        super().setUp()
        self.payloads["media_list"] = list(MEDIA)
        self.upload = os.path.join(self._tmp.name, "upload")
        self.app.config["WEB_UPLOAD_DIR"] = self.upload
        self.app.config["DISK_RESERVE_BYTES"] = 0

    def hdr(self, page="/console/studio"):
        return {"X-CSRF-Token": self.csrf(page)}

    def jpost(self, url, body, page="/console/studio"):
        return self.client.post(url, json=body, headers=self.hdr(page))

    def project(self, name="ws", ref="loop.mp4", overlays=()):
        p = Project(name, "media", ref, list(overlays)).validate()
        store.save_draft(p)
        return p


class MediaModelTests(unittest.TestCase):
    def test_classify_and_listing(self):
        self.assertEqual([media.classify(n) for n in ("a.MP4", "b.PNG", "c.opus", "d.txt", "")], ["video", "image", "audio", "other", "other"])
        rows = media.listing(MEDIA)
        self.assertEqual([r["name"] for r in rows], ["intro.webm", "logo.png", "loop.mp4", "song.mp3"])   # no txt, no hidden
        self.assertEqual([r["name"] for r in media.listing(MEDIA, "LOG")], ["logo.png"])
        self.assertEqual([r["name"] for r in media.listing(MEDIA, kind="audio")], ["song.mp3"])
        self.assertEqual(media.listing(MEDIA, "loop")[0]["size_text"], "5.0 MB")
        self.assertEqual(media.listing(None), [])

    def test_sanitize_is_a_strict_allow_list(self):
        self.assertEqual(media.sanitize("My Logo.PNG"), "my-logo.png")
        self.assertEqual(media.sanitize("C:/x/Clip 1.MP4"), "clip-1.mp4")
        for bad in ("../x.png", "a.exe", "noext", ".png", "", "a\x00b.png", "..\\..\\x.png", "x.svg", "x.html", "x.php.png.exe"):
            self.assertIsNone(media.sanitize(bad), bad)

    def test_every_listed_name_is_usable_as_a_project_asset(self):
        for r in media.listing(MEDIA):
            self.assertTrue(ASSET_RE.match(r["name"]), r["name"])


class MediaRouteTests(WS):
    def test_media_json_requires_login_and_filters(self):
        anon = self.app.test_client()
        r = anon.get("/console/studio/media.json")
        self.assertEqual(r.status_code, 401)
        d = self.client.get("/console/studio/media.json?kind=image").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual([i["name"] for i in d["items"]], ["logo.png"])
        self.assertEqual(d["counts"], {"video": 2, "image": 1, "audio": 1})
        self.assertEqual([i["name"] for i in self.client.get("/console/studio/media.json?q=loop").get_json()["items"]], ["loop.mp4"])

    def test_media_json_reports_an_unreadable_library(self):
        self.payloads["media_list"] = RuntimeError("bridge down")
        r = self.client.get("/console/studio/media.json")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])

    def test_thumb_rejects_bad_names_and_needs_login(self):
        self.assertEqual(self.app.test_client().get("/console/studio/media/x.png/thumb").status_code, 401)
        self.assertEqual(self.client.get("/console/studio/media/..%2Fx/thumb").status_code, 404)
        self.assertEqual(self.client.get("/console/studio/media/missing.mp4/thumb").status_code, 404)

    def test_upload_accepts_video_image_and_audio_through_the_existing_import(self):
        for fname, data, safe in (("My Clip.MP4", b"x" * 100, "my-clip.mp4"), ("Logo.PNG", b"\x89PNG" + b"x" * 50, "logo.png"),
                                  ("a b.mp3", b"ID3" + b"x" * 10, "a-b.mp3"), ("s.webp", b"RIFFxxxx", "s.webp")):
            r = self.client.post("/console/studio/media/upload", data={"media": (__import__("io").BytesIO(data), fname)},
                                 headers=self.hdr(), content_type="multipart/form-data")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            d = r.get_json()
            self.assertTrue(d["ok"])
            self.assertEqual(d["name"], safe)
            self.assertIn(("media_import_staged", safe), self.engine.mutation_calls)
            self.assertTrue(os.path.exists(os.path.join(self.upload, safe)))   # staged for the engine to take over

    def test_upload_rejections_never_reach_the_engine(self):
        import io
        for fname, data in (("evil.exe", b"MZ"), ("../../etc/passwd.png", b"x"), ("empty.png", b""), ("doc.svg", b"<svg/>")):
            r = self.client.post("/console/studio/media/upload", data={"media": (io.BytesIO(data), fname)}, headers=self.hdr(),
                                 content_type="multipart/form-data")
            self.assertEqual(r.status_code, 400, fname)
            self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertEqual(os.listdir(self.upload) if os.path.isdir(self.upload) else [], [])

    def test_upload_without_a_file_csrf_or_login_is_refused(self):
        r = self.client.post("/console/studio/media/upload", data={}, headers=self.hdr(), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/console/studio/media/upload", data={}).status_code, 400)   # no CSRF token
        self.assertEqual(self.app.test_client().post("/console/studio/media/upload", data={}).status_code, 400)

    def test_oversized_image_is_refused_with_a_clear_message(self):
        import io
        with mock.patch.object(media, "MAX_IMAGE_BYTES", 100):
            r = self.client.post("/console/studio/media/upload", data={"media": (io.BytesIO(b"x" * 500), "big.png")},
                                 headers=self.hdr(), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)
        self.assertIn("limit", r.get_json()["error"])
        self.assertFalse(os.path.exists(os.path.join(self.upload, "big.png")))
        self.assertEqual(self.engine.mutation_calls, [])

    def test_engine_failure_is_reported_and_the_staged_file_removed(self):
        import io
        from webapp.engine import EngineError
        self.engine.mutation_payloads[("media_import_staged",)] = EngineError("exists", code="MEDIA_EXISTS")
        r = self.client.post("/console/studio/media/upload", data={"media": (io.BytesIO(b"x" * 10), "dup.png")},
                             headers=self.hdr(), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already exists", r.get_json()["error"])
        self.assertFalse(os.path.exists(os.path.join(self.upload, "dup.png")))


class SaveAndApplyTests(WS):
    def test_save_json_round_trip_with_label_profile_and_layers(self):
        self.project()
        body = {"project": {"source_ref": "loop.mp4", "profile": {"width": 854, "height": 480, "fps": 25, "video_kbps": 1500, "audio_kbps": 96,
                                                                   "preset": "ultrafast", "scaler": "lanczos"},
                            "overlays": [dict(Overlay("text", text="Hello", x=0.1, y=0.1, w=0.4, h=0.1, label="Title").__dict__),
                                         dict(Overlay("image", asset="logo.png", x=0.5, y=0.1, w=0.2, h=0.2, crop_w=0.5).__dict__)]}}
        r = self.jpost("/console/studio/ws/save.json", body, "/console/studio")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(d["ok"] and d["unpublished"])
        self.assertEqual(d["missing"], [])
        p = store.load_draft("ws")
        self.assertEqual((p.profile.width, p.profile.scaler, p.profile.preset, p.profile.audio_kbps), (854, "lanczos", "ultrafast", 96))
        self.assertEqual([(o.kind, o.label) for o in p.overlays], [("text", "Title"), ("image", "")])
        self.assertEqual(p.overlays[1].crop_w, 0.5)

    def test_save_reports_missing_media_but_still_saves_the_draft(self):
        self.project()
        body = {"project": {"overlays": [dict(Overlay("image", asset="gone.png", w=0.2, h=0.2).__dict__)]}}
        d = self.jpost("/console/studio/ws/save.json", body).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["missing"][0]["name"], "gone.png")

    def test_strict_validation_rejects_bad_input_and_keeps_the_saved_draft(self):
        self.project(overlays=[Overlay("text", text="keep")])
        bad = [{"overlays": [{"kind": "nope"}]}, {"overlays": [dict(Overlay("text").__dict__, evil=1)]},
               {"overlays": [dict(Overlay("text").__dict__, x="0.5")]}, {"overlays": [dict(Overlay("text").__dict__, w=1.0, x=0.5)]},
               {"overlays": [dict(Overlay("text").__dict__, color="red")]}, {"overlays": [dict(Overlay("text").__dict__, label="<b>")]},
               {"overlays": "x"}, {"profile": {"width": 999, "height": 999}}, {"profile": {"scaler": "nearest"}},
               {"profile": {"bogus": 1}}, {"source_kind": "ftp"}, {"mystery": 1}, {"source_ref": ""},
               {"overlays": [dict(Overlay("text").__dict__)] * 30}, {"slide_seconds": "9"}]
        for proj in bad:
            r = self.jpost("/console/studio/ws/save.json", {"project": proj})
            self.assertEqual(r.status_code, 400, proj)
            self.assertFalse(r.get_json()["ok"])
        self.assertEqual([o.text for o in store.load_draft("ws").overlays], ["keep"])

    def test_save_requires_login_csrf_and_a_sane_body(self):
        self.project()
        url = "/console/studio/ws/save.json"
        self.assertEqual(self.app.test_client().post(url, json={}).status_code, 400)           # CSRF first (no session token)
        self.assertEqual(self.client.post(url, json={"project": {}}).status_code, 400)           # no CSRF header
        r = self.client.post(url, data="not json", headers=dict(self.hdr(), **{"Content-Type": "application/json"}))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.jpost("/console/studio/nope/save.json", {"project": {}}).status_code, 404)
        self.assertEqual(self.jpost("/console/studio/..%2Fx/save.json", {"project": {}}).status_code, 404)

    def test_an_output_that_cannot_fit_this_server_is_refused_but_existing_profiles_are_not_locked(self):
        self.project()
        with mock.patch.object(capabilities, "cpu_count", return_value=2):
            r = self.jpost("/console/studio/ws/save.json", {"project": {"profile": {"width": 1920, "height": 1080, "fps": 60}}})
            self.assertEqual(r.status_code, 400)
            self.assertIn("CPU", r.get_json()["error"])
            self.assertEqual(store.load_draft("ws").profile.width, 1280)
            ok = self.jpost("/console/studio/ws/save.json", {"project": {"overlays": []}})      # unchanged profile saves fine
            self.assertEqual(ok.status_code, 200)

    def test_save_never_touches_the_live_revision(self):
        self.project(overlays=[Overlay("text", text="live")])
        store.publish("ws")
        self.jpost("/console/studio/ws/save.json", {"project": {"overlays": [dict(Overlay("text", text="draft").__dict__)]}})
        self.assertEqual(store.load_published("ws").overlays[0].text, "live")
        self.assertTrue(store.has_unpublished_changes("ws"))

    def test_preview_text_renders_live_layers_without_ffmpeg(self):
        self.project()
        layers = [dict(Overlay("clock").__dict__), dict(Overlay("text", text="x").__dict__), dict(Overlay("countdown", target="2099-01-01T00:00:00").__dict__)]
        d = self.jpost("/console/studio/ws/preview-text.json", {"layers": layers}).get_json()
        self.assertTrue(d["ok"])
        self.assertRegex(d["texts"][0], r"^\d\d:\d\d:\d\d$")
        self.assertRegex(d["texts"][2], r"d \d\d:\d\d:\d\d$")

    def test_publish_json_blocks_missing_media_and_needs_restart_confirmation(self):
        self.project(overlays=[Overlay("image", asset="gone.png", w=0.2, h=0.2)])
        r = self.jpost("/console/studio/ws/publish.json", {})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["missing"][0]["name"], "gone.png")
        self.assertFalse(os.path.exists(os.path.join(store.studio_dir(), "ws.json")))
        self.project(overlays=[Overlay("image", asset="logo.png", w=0.2, h=0.2)])
        self.assertEqual(self.jpost("/console/studio/ws/publish.json", {}).get_json()["revision"], 1)
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "yes", "health": "HEALTHY"}]
        self.assertEqual(self.jpost("/console/studio/ws/publish.json", {}).status_code, 409)     # running: must confirm
        self.assertEqual(self.engine.mutation_calls, [])
        d = self.jpost("/console/studio/ws/publish.json", {"restart": True}).get_json()
        self.assertTrue(d["restarted"])
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "ws")])

    def test_assess_endpoint(self):
        with mock.patch.object(capabilities, "cpu_count", return_value=4):
            d = self.jpost("/console/studio/assess.json", {"profile": {"width": 1920, "height": 1080, "fps": 60}}).get_json()
            self.assertEqual(d["capacity"]["level"], "block")
            d = self.jpost("/console/studio/assess.json", {"profile": {"width": 854, "height": 480, "fps": 30}}).get_json()
            self.assertEqual(d["capacity"]["level"], "ok")
        self.assertEqual(self.jpost("/console/studio/assess.json", {"profile": {"fps": 61}}).status_code, 400)


class StreamControlTests(WS):
    def test_create_start_stop_use_the_existing_relay_operations(self):
        self.project(overlays=[Overlay("text", text="t")])
        url = "/console/studio/ws/stream.json"
        self.assertEqual(self.jpost(url, {"action": "start"}).status_code, 409)                  # no stream yet
        d = self.jpost(url, {"action": "create"}).get_json()
        self.assertTrue(d["ok"])
        self.assertIn(("relay_create_studio", "ws", "ws"), self.engine.mutation_calls)
        self.assertEqual(store.load_published("ws").revision, 1)                                  # first stream: draft applied once
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "no", "health": "STOPPED"}]
        self.assertTrue(self.jpost(url, {"action": "start"}).get_json()["ok"])
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "yes", "health": "HEALTHY"}]
        self.assertEqual(self.jpost(url, {"action": "start"}).status_code, 409)                  # already running
        self.assertTrue(self.jpost(url, {"action": "stop"}).get_json()["ok"])
        ops = [c[0] for c in self.engine.mutation_calls]
        self.assertEqual(ops, ["relay_create_studio", "relay_start", "relay_stop"])

    def test_a_stream_of_another_type_with_the_same_name_is_never_controlled(self):
        self.project()
        self.payloads["relay_list"] = [{"name": "ws", "type": "url", "active": "yes", "health": "HEALTHY"}]
        for act in ("start", "stop", "create"):
            self.assertEqual(self.jpost("/console/studio/ws/stream.json", {"action": act}).status_code, 409)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_unknown_action_and_unauthenticated_requests(self):
        self.project()
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "no"}]
        self.assertEqual(self.jpost("/console/studio/ws/stream.json", {"action": "format-disk"}).status_code, 400)
        self.assertEqual(self.app.test_client().post("/console/studio/ws/stream.json", json={"action": "stop"}).status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_status_reports_stream_publishers_and_capacity(self):
        self.project()
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "yes", "health": "HEALTHY", "health_detail": "fresh"}]
        self.engine.read = lambda op, *v: {"studio_health": {"state": "HEALTHY", "speed": "1.0", "fps": "30"},
                                           "relay_publisher_status": [{"id": "yt1", "display_name": "YT", "state": "LIVE", "detail": "ok"}]}[op]
        d = self.client.get("/console/studio/ws/status.json").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual((d["stream"]["exists"], d["stream"]["active"], d["stream"]["state"]), (True, True, "HEALTHY"))
        self.assertEqual(d["stream"]["publishers"][0]["state"], "LIVE")
        self.assertIn(d["capacity"]["level"], ("ok", "warn", "block"))
        self.assertNotIn("key", json.dumps(d).lower().replace("keyframe", ""))


class DestinationTests(WS):
    DESTS = [{"name": "yt1", "display_name": "YouTube", "platform": "youtube", "server_url": "rtmps://a.rtmps.youtube.com/live2/<redacted>",
              "enabled": "yes", "has_stream_key": "yes", "attached_count": "0"},
             {"name": "fb", "display_name": "Facebook", "platform": "facebook", "server_url": "", "enabled": "no", "has_stream_key": "no"}]

    def setUp(self):
        super().setUp()
        self.project()
        self.payloads["destination_list"] = self.DESTS
        self.payloads["relay_list"] = [{"name": "ws", "type": "studio", "active": "no"}]
        self.payloads[("relay_destinations_get", "ws")] = ["yt1"]

    def test_list_shows_platform_and_key_presence_but_never_a_key(self):
        d = self.client.get("/console/studio/ws/destinations.json").get_json()
        self.assertEqual(d["attached"], ["yt1"])
        yt = d["destinations"][0]
        self.assertEqual((yt["platform"], yt["has_stream_key"], yt["enabled"]), ("youtube", True, True))
        self.assertNotIn('"stream_key"', json.dumps(d))

    def test_attach_validates_ids_against_the_existing_destinations(self):
        r = self.jpost("/console/studio/ws/destinations.json", {"ids": ["yt1"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.engine.mutation_calls, [("relay_destinations_set", "ws", ("yt1",))])
        for bad in ({"ids": ["ghost"]}, {"ids": ["../x"]}, {"ids": "yt1"}, {"ids": [1]}, {}):
            self.assertEqual(self.jpost("/console/studio/ws/destinations.json", bad).status_code, 400, bad)
        self.assertEqual(len(self.engine.mutation_calls), 1)

    def test_attach_needs_the_stream_to_exist(self):
        self.payloads["relay_list"] = []
        self.assertEqual(self.jpost("/console/studio/ws/destinations.json", {"ids": ["yt1"]}).status_code, 409)


class SettingsTests(WS):
    def test_defaults_round_trip_and_persist(self):
        d = self.client.get("/console/studio/settings.json").get_json()
        self.assertEqual(d["settings"], settings.DEFAULTS)
        new = json.loads(json.dumps(settings.DEFAULTS))
        new["general"].update(theme="light", confirm_stop=False, snap_sensitivity=20, snap_center=False)
        new["hotkeys"]["save"] = "Ctrl+Shift+S"
        r = self.jpost("/console/studio/settings.json", {"settings": new})
        self.assertEqual(r.status_code, 200)
        again = self.client.get("/console/studio/settings.json").get_json()["settings"]
        self.assertEqual(again["general"]["theme"], "light")
        self.assertEqual(again["hotkeys"]["save"], "Ctrl+Shift+S")
        self.assertTrue(os.path.exists(os.path.join(store.studio_dir(), ".ui-settings.json")))
        self.assertEqual(store.list_projects(), [])                      # the settings file is not mistaken for a project

    def test_server_side_validation(self):
        base = json.loads(json.dumps(settings.DEFAULTS))
        def with_(sec, key, val):
            d = json.loads(json.dumps(base)); d[sec][key] = val; return d
        bad = [with_("general", "theme", "neon"), with_("general", "snap_sensitivity", 0), with_("general", "snap_sensitivity", 99),
               with_("general", "confirm_stop", "yes"), with_("advanced", "status_poll_s", 1), with_("advanced", "text_preview_s", 61),
               with_("hotkeys", "save", "Ctrl+"), with_("hotkeys", "save", "rm -rf"), with_("hotkeys", "apply", "Ctrl+S"),
               {"general": {"bogus": 1}}, {"unknown": {}}, {"general": []}]
        for doc in bad:
            self.assertEqual(self.jpost("/console/studio/settings.json", {"settings": doc}).status_code, 400, doc)
        self.assertEqual(self.client.get("/console/studio/settings.json").get_json()["settings"], settings.DEFAULTS)

    def test_a_damaged_file_falls_back_to_defaults(self):
        os.makedirs(store.studio_dir(), exist_ok=True)
        with open(os.path.join(store.studio_dir(), ".ui-settings.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(settings.load(), settings.DEFAULTS)


class CapacityAndPipelineTests(unittest.TestCase):
    def test_assessment_levels(self):
        p = lambda w, h, f, preset="veryfast": OutputProfile(w, h, f, preset=preset)  # noqa: E731
        self.assertEqual(capabilities.assess(p(1280, 720, 30), 8)["level"], "ok")
        self.assertEqual(capabilities.assess(p(1280, 720, 30), 2)["level"], "warn")
        self.assertEqual(capabilities.assess(p(1920, 1080, 60), 4)["level"], "block")
        self.assertEqual(capabilities.assess(p(1920, 1080, 30, "ultrafast"), 4)["level"], "warn")

    def test_nvenc_is_never_offered_as_selectable(self):
        with mock.patch.object(capabilities, "_ffmpeg_encoders", return_value={"libx264", "h264_nvenc"}), \
                mock.patch.object(capabilities, "gpu_present", return_value=True):
            enc = {e["id"]: e for e in capabilities.encoders()}
        self.assertTrue(enc["libx264"]["selectable"])
        self.assertFalse(enc["h264_nvenc"]["selectable"])
        self.assertIn("does not use it yet", enc["h264_nvenc"]["why"])
        with mock.patch.object(capabilities, "_ffmpeg_encoders", return_value={"libx264"}), \
                mock.patch.object(capabilities, "gpu_present", return_value=False):
            self.assertIn("No NVIDIA GPU", {e["id"]: e for e in capabilities.encoders()}["h264_nvenc"]["why"])

    def test_default_scaler_keeps_existing_filter_graphs_identical_and_lanczos_is_applied(self):
        def graph(scaler):
            p = Project("g", "media", "a.mp4", [Overlay("image", asset="logo.png", w=0.2, h=0.2)], OutputProfile(scaler=scaler)).validate()
            a = ffmpeg.build_live_args(p, "/media/a.mp4", "rtmp://127.0.0.1:1935/studio/g", "/run/studio-g.work", "/media")
            return a[a.index("-filter_complex") + 1]
        self.assertNotIn("flags=", graph("bicubic"))
        self.assertEqual(graph("lanczos").count("flags=lanczos"), 2)       # background fit + the image overlay

    def test_480p_and_labels(self):
        Project("p", "media", "a.mp4", [Overlay("text", label="Lower third")], OutputProfile(854, 480, 30)).validate()
        for bad in (Overlay("text", label="x" * 41), Overlay("text", label="a\nb"), Overlay("text", label="<script>")):
            with self.assertRaises(ValidationError):
                bad.validate()

    def test_layer_names_do_not_change_the_render_cache_identity(self):
        a = Project("p", "media", "a.mp4", [Overlay("text", text="t")]).overlay_fingerprint()
        b = Project("p", "media", "a.mp4", [Overlay("text", text="t", label="Name")]).overlay_fingerprint()
        self.assertEqual(a, b)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed on this machine (media tests are UNTESTED here, not passed)")
class RealMediaThumbnailTests(unittest.TestCase):
    def test_video_and_image_thumbnails_are_real_pngs(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_MEDIA_DIR": d}):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=2",
                            os.path.join(d, "v.mp4")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x32", "-frames:v", "1",
                            os.path.join(d, "i.png")], check=True)
            for name in ("v.mp4", "i.png"):
                png = preview.thumb_png(name)
                self.assertTrue(png and png[:8] == b"\x89PNG\r\n\x1a\n", name)
            self.assertIsNone(preview.thumb_png("missing.mp4"))
            self.assertIsNone(preview.thumb_png("notes.txt"))


if __name__ == "__main__":
    unittest.main()
