"""Studio console routes: feature flag, backend licence gating, CSRF, draft/apply."""
import os
import tempfile
import time
import unittest
from unittest import mock

from webapp.studio import ed25519, entitlement, feedback, store
from webapp.test_console import CSRF_RE, FakeEngine, login, make_app

SK = bytes(range(32))
PK = ed25519.public_key(SK)
SNAP = {"relay_list": [], "playlist_list": [], "media_list": [{"name": "loop.mp4"}], "snapshot": {}}


class StudioEngine(FakeEngine):
    def relay_create_studio(self, name, project):
        return self._mutation("relay_create_studio", name, project)

    def relay_restart(self, name):
        return self._mutation("relay_restart", name)


class Base(unittest.TestCase):
    enabled = True
    licensed = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = self._tmp.name
        env = {"BLUESTREAM_STUDIO_DIR": d + "/studio", "BLUESTREAM_ETC_DIR": d + "/etc"}
        if self.enabled:
            env["BLUESTREAM_STUDIO_ENABLED"] = "1"
        env["BLUESTREAM_STUDIO_PUBKEY_HEX"] = PK.hex()
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
        self.assertIn("UNPUBLISHED CHANGES", html)
        self.assertIn("Live composition", html)  # countdown is dynamic
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
