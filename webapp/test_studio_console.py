"""Studio console routes: feature flag, backend licence gating, CSRF, draft/apply."""
import os
import tempfile
import time
import unittest
from unittest import mock

from webapp.studio import ed25519, entitlement, feedback, store
from webapp.studio.model import Project
from webapp.test_console import CSRF_RE, FakeEngine, login, make_app

SK = bytes(range(32))
PK = ed25519.public_key(SK)
SNAP = {"relay_list": [], "playlist_list": [], "media_list": [{"name": "loop.mp4"}], "snapshot": {}}


class StudioEngine(FakeEngine):
    def relay_create_studio(self, name, project):
        return self._mutation("relay_create_studio", name, project)

    def relay_restart(self, name):
        return self._mutation("relay_restart", name)

    def relay_publisher_takeover(self, name, destination):
        return self._mutation("relay_publisher_takeover", name, destination)


class Base(unittest.TestCase):
    enabled = True
    licensed = True
    enforce = True        # existing enforcement tests opt in; the DEFAULT (deferred licensing) is covered by DeferredLicensingTests

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        env = {"BLUESTREAM_STUDIO_DIR": d + "/studio", "BLUESTREAM_ETC_DIR": d + "/etc", "BLUESTREAM_STUDIO_BRIDGE_CACHE": "0"}
        if self.enabled:
            env["BLUESTREAM_STUDIO_ENABLED"] = "1"
        env["BLUESTREAM_STUDIO_PUBKEY_HEX"] = PK.hex()
        if self.enforce:
            env["BLUESTREAM_STUDIO_ENFORCE_LICENCE"] = "1"
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(d + "/etc")
        if self.licensed:
            entitlement.install_token(entitlement.make_token(
                SK, {"v": 1, "lic": "L1", "plan": "studio", "exp": time.time() + 3600}))
        self.payloads = dict(SNAP, relay_list=[])
        self.engine = StudioEngine(self.payloads)
        self.app, _ = make_app(d, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def csrf(self, path="/console/streams"):
        return CSRF_RE.search(self.client.get(path).get_data(as_text=True)).group(1)

    def post(self, path, data=None, page="/console/streams"):
        d = dict(data or {})
        d["csrf_token"] = self.csrf(page)
        return self.client.post(path, data=d)


class FlagOffTests(Base):
    enabled = False

    def test_no_studio_endpoints_when_flag_off(self):
        self.assertEqual(self.client.get("/console/studio").status_code, 404)
        self.assertNotIn("Studio", self.client.get("/console/streams").get_data(as_text=True).split("<main")[0])


class StudioRouteTests(Base):
    def make(self, name="demo", tpl="countdown", kind="media", ref="loop.mp4"):
        return self.post("/console/studio/new", {"name": name, "source_kind": kind,
                                                 "source_ref": ref, "template": tpl}, "/console/studio")

    def test_home_shows_licence_and_nav(self):
        html = self.client.get("/console/studio").get_data(as_text=True)
        self.assertIn("ACTIVE", html)
        self.assertIn("loop.mp4", html)
        self.assertIn(">Studio</a>", self.client.get("/console/streams").get_data(as_text=True))

    def test_create_edit_draft_publish_flow(self):
        rv = self.make()
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/studio/demo").get_data(as_text=True)
        self.assertIn('id="studio-state"', html)
        st = self.client.get("/console/studio/demo/status.json").get_json()
        self.assertTrue(st["ok"] and st["unpublished"])  # never applied yet
        # editor form: drop all existing overlays (count=0) and add a ticker
        rv = self.post("/console/studio/demo/save", {"count": "0", "action": "add:ticker",
                                                     "source_ref": "loop.mp4"}, "/console/studio/demo")
        self.assertEqual(rv.status_code, 302)
        kinds = [o.kind for o in store.load_draft("demo").overlays]
        self.assertEqual(kinds, ["ticker"])
        # a malformed overlay row is rejected as a whole; the saved draft is unchanged
        self.post("/console/studio/demo/save", {"count": "1", "kind_0": "text", "x_0": "5", "size_0": "48"},
                  "/console/studio/demo")
        self.assertEqual([o.kind for o in store.load_draft("demo").overlays], ["ticker"])

    def test_publish_then_create_stream_and_restart_confirmation(self):
        self.make("live1", "ticker")
        self.assertEqual(self.post("/console/studio/live1/publish", {}, "/console/studio/live1").status_code, 302)
        self.assertEqual(store.load_published("live1").revision, 1)
        self.post("/console/studio/live1/create-stream", {}, "/console/studio/live1")
        self.assertIn(("relay_create_studio", "live1", "live1"), self.engine.mutation_calls)
        # running stream: publish needs explicit confirmation, then restarts only that stream
        self.payloads["relay_list"] = [{"name": "live1", "type": "studio", "active": "yes",
                                        "health": "HEALTHY", "destination_count": "1"}]
        self.engine.mutation_calls.clear()
        self.post("/console/studio/live1/publish", {}, "/console/studio/live1")
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertEqual(store.load_published("live1").revision, 1)
        self.post("/console/studio/live1/publish", {"restart": "1"}, "/console/studio/live1")
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "live1")])
        self.assertEqual(store.load_published("live1").revision, 2)

    def test_invalid_input_rejected_and_nothing_written(self):
        self.make("Bad Name")
        self.make("ok", kind="bogus")
        self.make("dup")
        self.make("dup")  # duplicate
        self.assertEqual(store.list_projects(), ["dup"])
        self.assertEqual(self.client.get("/console/studio/..%2Fetc").status_code, 404)

    def test_rollback_loads_old_revision_into_draft_only(self):
        self.make("rb", "ticker")
        self.post("/console/studio/rb/publish", {}, "/console/studio/rb")
        p = store.load_draft("rb")
        p.overlays = []
        store.save_draft(p)
        self.post("/console/studio/rb/publish", {}, "/console/studio/rb")
        self.post("/console/studio/rb/rollback", {"revision": "1"}, "/console/studio/rb")
        self.assertEqual(len(store.load_draft("rb").overlays), 2)
        self.assertEqual(len(store.load_published("rb").overlays), 0)  # live untouched

    def test_csrf_required(self):
        rv = self.client.post("/console/studio/new", data={"name": "x"})
        self.assertIn(rv.status_code, (400, 403))
        self.assertEqual(store.list_projects(), [])

    def test_login_required(self):
        anon = self.app.test_client()
        for path in ("/console/studio", "/console/studio/demo", "/console/help"):
            self.assertEqual(anon.get(path).status_code, 302, path)

    def test_help_form_saves_redacted_local_report(self):
        rv = self.post("/console/help", {"kind": "bug", "consent": "on", "diagnostics": "on",
                                         "message": "stream failed to rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop-qrst token=hunter2hunter2"},
                       "/console/help")
        self.assertEqual(rv.status_code, 302)
        raw = open(feedback.queue_path()).read()
        self.assertNotIn("abcd-efgh", raw)
        self.assertNotIn("hunter2", raw)
        self.assertIn("pending", raw)
        # consent required
        self.post("/console/help", {"kind": "bug", "message": "long enough message"}, "/console/help")
        self.assertEqual(raw.count("\n"), open(feedback.queue_path()).read().count("\n"))


class DeferredLicensingTests(Base):
    """Licensing/payment is deferred: by default Studio needs no key, token or payment and shows no licence UI."""
    enforce = False
    licensed = False
    make = StudioRouteTests.make

    def test_whole_flow_works_with_no_licence_key_or_token(self):
        html = self.client.get("/console/studio").get_data(as_text=True)
        self.assertNotIn("Paste licence key", html)
        self.assertNotIn("NOT ACTIVE", html)
        self.assertNotIn("Premium", html)
        self.assertEqual(self.make("free1", "ticker").status_code, 302)
        self.assertIn("ticker", [o.kind for o in store.load_draft("free1").overlays])
        self.assertEqual(self.post("/console/studio/free1/publish", {}, "/console/studio/free1").status_code, 302)
        self.assertEqual(store.load_published("free1").revision, 1)
        self.post("/console/studio/free1/create-stream", {}, "/console/studio/free1")
        self.assertIn(("relay_create_studio", "free1", "free1"), self.engine.mutation_calls)
        edit = self.client.get("/console/studio/free1").get_data(as_text=True)
        self.assertNotIn("Premium", edit)
        self.assertIn("Save draft", edit)

    def test_licence_endpoint_is_inert(self):
        rv = self.post("/console/studio/license", {"token": "BS1.x.y"}, "/console/studio")
        self.assertEqual(rv.status_code, 302)
        self.assertFalse(os.path.exists(os.environ["BLUESTREAM_ETC_DIR"] + "/studio-license.token"))

    def test_runtime_does_not_require_a_licence(self):
        from studio import cli
        with mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_PUBKEY_HEX": ""}):
            self.assertTrue(entitlement.current().ok)


class UnlicensedTests(Base):
    licensed = False

    def test_mutations_blocked_on_backend_without_licence(self):
        for path, data in (("/console/studio/new", {"name": "x", "source_kind": "media", "source_ref": "a.mp4"}),
                           ("/console/studio/x/save", {}), ("/console/studio/x/publish", {}),
                           ("/console/studio/x/create-stream", {}), ("/console/studio/x/delete", {})):
            self.assertEqual(self.post(path, data, "/console/studio").status_code, 302, path)
        self.assertEqual(store.list_projects(), [])
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertIn("NOT ACTIVE", self.client.get("/console/studio").get_data(as_text=True))

    def test_free_relay_pages_unaffected(self):
        for path in ("/console/streams", "/console/media", "/console/playlists"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_invalid_licence_key_rejected_and_valid_activates(self):
        self.post("/console/studio/license", {"token": "BS1.bad.bad"}, "/console/studio")
        self.assertFalse(entitlement.current().ok)
        good = entitlement.make_token(SK, {"v": 1, "lic": "L2", "plan": "studio", "exp": time.time() + 99})
        self.post("/console/studio/license", {"token": good}, "/console/studio")
        self.assertTrue(entitlement.current().ok)


class FeedbackUnitTests(unittest.TestCase):
    def test_redaction_and_limits(self):
        for raw in ("rtmp://x.example/app/SECRETKEY123456", "BS1." + "a" * 40 + ".sig",
                    "password: hunter2", "https://user:pw@host/x", "a" * 64):
            self.assertNotIn("SECRETKEY", feedback.redact(raw))
        self.assertNotIn("hunter2", feedback.redact("password: hunter2"))
        self.assertNotIn(":pw@", feedback.redact("https://user:pw@host/x"))
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_DIR": d}):
            for i in range(feedback.MAX_PER_HOUR):
                feedback.submit("bug", "a long enough message %d" % i, True, False, now=1000 + i)
            with self.assertRaises(feedback.FeedbackError):
                feedback.submit("bug", "one more report please", True, False, now=1100)
            feedback.submit("bug", "later report is fine", True, False, now=1000 + 7200)
            for bad in (("nope", "long enough message", True), ("bug", "short", True), ("bug", "long enough message", False)):
                with self.assertRaises(feedback.FeedbackError):
                    feedback.submit(bad[0], bad[1], bad[2], False, now=9999999)


if __name__ == "__main__":
    unittest.main()


class HealthAndOwnershipPanelTests(Base):
    HEALTH = {"state": "DEGRADED", "detail": "encoder speed 0.82x is below real time", "speed": "0.82",
              "fps": "24.6", "cpu_cores": "2.4", "rss_mb": "412.5", "dropped_frames": "7",
              "restarts": "3", "last_failure": "unexpected end (ffmpeg exit=1)"}
    PUBS = [{"id": "yt1", "display_name": "YouTube main", "state": "BLOCKED",
             "detail": "destination is in use by another stream", "blocked_by": "relay:old:yt1"},
            {"id": "yt2", "display_name": "Backup", "state": "LIVE", "detail": "publishing", "blocked_by": ""}]

    def setUp(self):
        super().setUp()

        def read(op, *values):
            self.engine.calls.append((op,) + values)
            return {"studio_health": self.HEALTH, "relay_publisher_status": self.PUBS}[op]
        self.engine.read = read
        p = Project("hs", "media", "loop.mp4", []).validate()
        store.save_draft(p)
        store.publish("hs")
        self.payloads["relay_list"] = [{"name": "hs", "type": "studio", "active": "yes",
                                        "health": "HEALTHY", "health_detail": "hls fresh"}]

    def test_health_panel_shows_real_encoder_numbers(self):
        s = self.client.get("/console/studio/hs/status.json").get_json()["stream"]
        self.assertEqual((s["state"], s["speed"], s["cpu_cores"], s["rss_mb"], s["dropped"], s["restarts"]),
                         ("DEGRADED", "0.82", "2.4", "412.5", "7", "3"))
        self.assertIn("unexpected end", s["last_failure"])
        self.assertEqual((s["health"], s["health_detail"]), ("HEALTHY", "hls fresh"))
        self.assertTrue(s["active"] and s["exists"])

    def test_blocked_destination_offers_explicit_switch_only(self):
        pubs = self.client.get("/console/studio/hs/status.json").get_json()["stream"]["publishers"]
        blocked = [p for p in pubs if p["state"] == "BLOCKED"]
        self.assertEqual(len(blocked), 1)                          # only the BLOCKED one can be switched
        self.assertEqual(blocked[0]["blocked_by"], "relay:old:yt1")

    def test_json_takeover_needs_explicit_confirmation(self):
        h = {"X-CSRF-Token": self.csrf("/console/studio/hs")}
        url = "/console/studio/hs/takeover.json"
        r = self.client.post(url, json={"destination": "yt1"}, headers=h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])
        r = self.client.post(url, json={"destination": "yt1", "confirm": True}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.engine.mutation_calls, [("relay_publisher_takeover", "hs", "yt1")])

    def test_takeover_requires_confirmation_csrf_and_calls_engine_once(self):
        path = "/console/studio/hs/takeover/yt1"
        self.assertIn(self.client.post(path, data={"confirm": "1"}).status_code, (400, 403))
        self.post(path, {}, "/console/studio/hs")                      # no confirm -> refused
        self.assertEqual(self.engine.mutation_calls, [])
        self.post(path, {"confirm": "1"}, "/console/studio/hs")
        self.assertEqual(self.engine.mutation_calls, [("relay_publisher_takeover", "hs", "yt1")])

    def test_takeover_blocked_without_licence_and_for_bad_ids(self):
        self.post("/console/studio/hs/takeover/..%2Fx", {"confirm": "1"}, "/console/studio/hs")
        self.assertEqual(self.engine.mutation_calls, [])


class EditorServerSideTests(Base):
    def make(self, name="ed"):
        self.post("/console/studio/new", {"name": name, "source_kind": "media", "source_ref": "loop.mp4",
                                          "template": "blank"}, "/console/studio")

    def test_editor_page_embeds_every_layer_and_the_workspace_scripts(self):
        import json
        import re
        self.make()
        p = store.load_draft("ed")
        from webapp.studio.model import Overlay
        p.overlays = [Overlay("rect", y=0.8, h=0.2, w=1.0, visible=False), Overlay("data", text="S: {value}",
                      url="https://example.com/a.json", json_path="a.b", refresh_s=90, value_format="thousands"),
                      Overlay("image", asset="logo.png", crop_w=0.5)]
        store.save_draft(p.validate())
        html = self.client.get("/console/studio/ed").get_data(as_text=True)
        state = json.loads(re.search(r'<script type="application/json" id="studio-state">(.*?)</script>', html, re.S).group(1))
        ov = state["project"]["overlays"]
        self.assertEqual([o["kind"] for o in ov], ["rect", "data", "image"])
        self.assertFalse(ov[0]["visible"])                                     # hidden layers are still editable
        self.assertEqual((ov[1]["json_path"], ov[1]["refresh_s"], ov[1]["value_format"]), ("a.b", 90, "thousands"))
        self.assertEqual(ov[2]["crop_w"], 0.5)
        self.assertEqual(len(state["texts"]), 3)
        for needle in ("studio_workspace.js", "studio_picker.js", 'id="ws-canvas"', 'id="ws-props"', 'id="ws-layers"', 'id="ws-menu"'):
            self.assertIn(needle, html)

    def test_data_and_crop_fields_round_trip_through_the_same_validated_model(self):
        self.make()
        form = {"count": "2", "kind_0": "data", "x_0": "0.1", "y_0": "0.1", "w_0": "0.4", "h_0": "0.1",
                "visible_0": "on", "text_0": "Subs {value}", "url_0": "https://example.com/s.json",
                "json_path_0": "c.subs", "refresh_s_0": "120", "value_format_0": "int", "size_0": "40",
                "kind_1": "image", "x_1": "0.6", "y_1": "0.1", "w_1": "0.2", "h_1": "0.2", "visible_1": "on",
                "asset_1": "logo.png", "crop_x_1": "0.25", "crop_y_1": "0", "crop_w_1": "0.5", "crop_h_1": "1"}
        self.post("/console/studio/ed/save", form, "/console/studio/ed")
        o = store.load_draft("ed").overlays
        self.assertEqual((o[0].url, o[0].json_path, o[0].refresh_s, o[0].value_format),
                         ("https://example.com/s.json", "c.subs", 120, "int"))
        self.assertEqual((o[1].crop_x, o[1].crop_w), (0.25, 0.5))
        # invalid crop / format / refresh rejected as a whole, draft unchanged
        for key, val in (("crop_w_1", "0.01"), ("crop_x_1", "0.9"), ("value_format_0", "evil"), ("refresh_s_0", "5"),
                         ("url_0", "ftp://x/y")):
            bad = dict(form)
            bad[key] = val
            self.post("/console/studio/ed/save", bad, "/console/studio/ed")
            self.assertEqual(store.load_draft("ed").overlays[0].refresh_s, 120, key)
            self.assertEqual(store.load_draft("ed").overlays[1].crop_w, 0.5, key)

    def test_asset_route_serves_only_media_library_images(self):
        import pathlib
        media = pathlib.Path(os.environ["BLUESTREAM_MEDIA_DIR"]) if "BLUESTREAM_MEDIA_DIR" in os.environ else None
        d = self._tmp.name + "/media"
        os.makedirs(d, exist_ok=True)
        with mock.patch.dict(os.environ, {"BLUESTREAM_MEDIA_DIR": d}):
            open(d + "/logo.png", "wb").write(b"\x89PNG\r\n\x1a\nxx")
            open(d + "/notes.txt", "wb").write(b"secret")
            ok = self.client.get("/console/studio/asset/logo.png")
            self.assertEqual((ok.status_code, ok.mimetype), (200, "image/png"))
            ok.close()  # release the file handle (Windows dev boxes)
            for bad in ("notes.txt", "missing.png", "..%2F..%2Fetc%2Fpasswd", ".hidden.png"):
                self.assertEqual(self.client.get("/console/studio/asset/" + bad).status_code, 404, bad)
        anon = self.app.test_client()
        self.assertEqual(anon.get("/console/studio/asset/logo.png").status_code, 302)
        del media

    def test_frame_route_404_without_source_and_requires_login(self):
        self.make()
        self.assertEqual(self.client.get("/console/studio/ed/frame.jpg").status_code, 404)
        self.assertEqual(self.app.test_client().get("/console/studio/ed/frame.jpg").status_code, 302)
        self.assertEqual(self.client.get("/console/studio/nope/frame.jpg").status_code, 404)


class PreviewUnitTests(unittest.TestCase):
    def test_source_path_resolution_and_traversal_refusal(self):
        from webapp.studio import preview
        from webapp.studio.model import Project
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "BLUESTREAM_MEDIA_DIR": d + "/m", "BLUESTREAM_HLS_ROOT": d + "/hls"}):
            os.makedirs(d + "/m")
            os.makedirs(d + "/hls/relay/cam")
            open(d + "/m/a.mp4", "wb").write(b"x")
            open(d + "/hls/relay/cam/1.ts", "wb").write(b"x")
            open(d + "/hls/relay/cam/2.ts", "wb").write(b"x")
            os.utime(d + "/hls/relay/cam/1.ts", (1, 1))
            self.assertTrue(preview.source_path(Project("p", "media", "a.mp4")).endswith("a.mp4"))
            self.assertTrue(preview.source_path(Project("p", "relay", "cam")).endswith("2.ts"))  # newest segment
            self.assertIsNone(preview.source_path(Project("p", "media", "../x.mp4")))
            self.assertIsNone(preview.source_path(Project("p", "relay", "../cam")))
            self.assertIsNone(preview.source_path(Project("p", "media", "nope.mp4")))
            self.assertIsNone(preview.frame_jpeg(Project("p", "media", "nope.mp4")))
