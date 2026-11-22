"""Browser source: gate, validation, egress policy, worker behaviour, and (when a Chromium
binary is provided via BLUESTREAM_BROWSER_BIN, as in the CI browser job) a REAL capture.

The real-browser tests run Chromium with --no-sandbox because CI containers cannot give it
user namespaces. That is test-only (`allow_no_sandbox`); production refuses to run without
the sandbox, and the sandbox-enabled path plus the systemd isolation are verified on staging,
NOT here (see docs/bluestream/RELEASE_CHECKLIST.md)."""
import io
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from webapp.studio import browsercap as bc
from webapp.studio import cli, netguard, store
from webapp.studio.model import Overlay, Project, ValidationError

PUB = lambda host, port: ["93.184.216.34"]  # noqa: E731  fake public resolver
BIN = os.environ.get("BLUESTREAM_BROWSER_BIN", "")
REAL = bool(BIN) and os.path.isfile(BIN) and sys.platform.startswith("linux")


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


class SpecValidationTests(unittest.TestCase):
    def v(self, url="https://example.com/page", sel="#box", w=1280, h=720, interval=60, extra=""):
        return bc.validate_spec(url, sel, w, h, interval, extra)

    def test_accepts_a_normal_spec(self):
        self.assertEqual(self.v(), ["example.com"])
        self.assertEqual(self.v(extra="cdn.example.com, fonts.example.org"),
                         ["example.com", "cdn.example.com", "fonts.example.org"])

    def test_rejects_unsafe_urls(self):
        for url in ("http://example.com/", "file:///etc/passwd", "ftp://example.com/", "https://u:p@example.com/",
                    "https://127.0.0.1/", "https://169.254.169.254/latest", "https://10.0.0.5/", "https://[::1]/",
                    "https://localhost/", "https://intranet/", "https://printer.local/", "https://x.internal/",
                    "https://example.com:8443/", "https:///nohost", "javascript:alert(1)"):
            with self.assertRaises(ValidationError, msg=url):
                self.v(url=url)

    def test_rejects_bad_selector_viewport_interval_hosts(self):
        for sel in ("a;b", "{}", "div{color:red}", "x" * 201, "<script>"):
            with self.assertRaises(ValidationError, msg=sel):
                self.v(sel=sel)
        for w, h in ((100, 100), (4000, 700), (1280, 5000)):
            with self.assertRaises(ValidationError):
                self.v(w=w, h=h)
        for interval in (1, 14, 3601):
            with self.assertRaises(ValidationError):
                self.v(interval=interval)
        for extra in ("evil host", "a/b", "example.com", ",".join("h%d.example.com" % i for i in range(9))):
            with self.assertRaises(ValidationError, msg=extra):
                self.v(extra=extra)

    def test_overlay_model_uses_the_same_validation(self):
        ok = Overlay("browser", url="https://example.com/", refresh_s=60)
        Project("b", "media", "a.mp4", [ok]).validate()
        for bad in (Overlay("browser", url="http://example.com/", refresh_s=60),
                    Overlay("browser", url="https://example.com/", refresh_s=5),
                    Overlay("browser", url="https://example.com/", selector="a;b", refresh_s=60)):
            with self.assertRaises(ValidationError):
                Project("b", "media", "a.mp4", [bad]).validate()


class EgressPolicyTests(unittest.TestCase):
    def test_request_allowed_only_listed_https_hosts(self):
        hosts = {"example.com", "cdn.example.com"}
        for url in ("https://example.com/a.png", "https://cdn.example.com/x.js", "data:image/png;base64,AAAA",
                    "about:blank", "blob:https://example.com/1"):
            self.assertTrue(bc.request_allowed(url, hosts), url)
        for url in ("http://example.com/", "https://evil.com/", "https://example.com.evil.com/",
                    "http://169.254.169.254/latest/meta-data", "https://127.0.0.1/", "http://[::1]:8080/",
                    "file:///etc/passwd", "ftp://example.com/", "ws://example.com/", "https://u:p@example.com/",
                    "https://localhost/", "not a url"):
            self.assertFalse(bc.request_allowed(url, hosts), url)

    def test_resolver_rules_pin_validated_ips_and_deny_everything_else(self):
        rules = bc.resolver_rules(["example.com", "cdn.example.com"], PUB)
        self.assertEqual(rules, "MAP example.com 93.184.216.34, MAP cdn.example.com 93.184.216.34, MAP * ~NOTFOUND")

    def test_private_resolution_is_refused_before_chromium_starts(self):
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(netguard.BlockedError):
                bc.resolver_rules(["rebind.example.com"])
        with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(netguard.BlockedError):                 # mixed answers (rebinding set-up)
                bc.resolver_rules(["mixed.example.com"])

    def test_chromium_is_never_started_without_its_sandbox_in_production(self):
        args = bc.chromium_args("/usr/bin/chromium", "/tmp/p", ["example.com"], 1280, 720, PUB)
        self.assertNotIn("--no-sandbox", args)
        self.assertIn("--remote-debugging-pipe", args)
        self.assertTrue(any(a.startswith("--host-resolver-rules=") for a in args))
        self.assertIn("--proxy-server=direct://", args)
        self.assertIn("--no-sandbox", bc.chromium_args("/usr/bin/chromium", "/tmp/p", ["example.com"], 1280, 720, PUB,
                                                       allow_no_sandbox=True))


class GateTests(unittest.TestCase):
    def test_off_by_default_and_requires_binary_and_sandbox(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLUESTREAM_STUDIO_BROWSER", None)
            ok, why = bc.usable()
            self.assertFalse(ok)
            self.assertIn("disabled", why)
        with mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_BROWSER": "1", "BLUESTREAM_BROWSER_BIN": "/nope"}):
            self.assertEqual(bc.usable()[0], False)
        bc._usable_cache.update(at=0.0, val=None)
        with mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_BROWSER": "1"}), \
                mock.patch.object(bc, "find_browser", return_value="/usr/bin/chromium"), \
                mock.patch.object(bc, "sandbox_works", return_value=False):
            ok, why = bc.usable()
            self.assertFalse(ok)
            self.assertIn("sandbox", why)
        bc._usable_cache.update(at=0.0, val=None)
        with mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_BROWSER": "1"}), \
                mock.patch.object(bc, "find_browser", return_value="/usr/bin/chromium"), \
                mock.patch.object(bc, "sandbox_works", return_value=True):
            self.assertEqual(bc.usable(), (True, "ready"))
        bc._usable_cache.update(at=0.0, val=None)

    def test_runtime_refuses_a_crafted_project_with_a_browser_overlay_when_gate_is_off(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "BLUESTREAM_STUDIO_DIR": d + "/s", "BLUESTREAM_ETC_DIR": d + "/e", "BLUESTREAM_RUN_DIR": d}):
            os.environ.pop("BLUESTREAM_STUDIO_BROWSER", None)
            from webapp.studio import ed25519, entitlement
            sk = bytes(range(32))
            os.makedirs(d + "/e")
            os.environ["BLUESTREAM_STUDIO_PUBKEY_HEX"] = ed25519.public_key(sk).hex()
            entitlement.install_token(entitlement.make_token(sk, {"v": 1, "lic": "x", "plan": "studio", "exp": 4102444800}))
            store.save_draft(Project("bp", "media", "a.mp4", [Overlay("browser", url="https://example.com/", refresh_s=60)]).validate())
            store.publish("bp")
            with mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(cli.cmd_argv("bp", "bp"), 2)
            self.assertIn("browser source unavailable", err.getvalue())


class WorkerTests(unittest.TestCase):
    def overlay(self, **kw):
        return Overlay("browser", url="https://example.com/", refresh_s=30, **kw)

    def run_worker(self, behaviour, ticks):
        calls, clock = [], [0.0]
        count = [0]

        def fake_capture(url, sel, w, h, out, hosts, interval=0):
            calls.append(clock[0])
            behaviour(len(calls), out)

        def stop():
            return count[0] >= ticks

        def sleep(_):
            count[0] += 1
            clock[0] += 10.0

        with tempfile.TemporaryDirectory() as d:
            bc.worker("r", [(0, self.overlay())], d, stop, sleep=sleep, now=lambda: clock[0], do_capture=fake_capture)
        return calls

    def test_cadence_and_exponential_backoff_on_failure(self):
        def always_fail(n, out):
            raise bc.CaptureError("boom")
        calls = self.run_worker(always_fail, 60)           # 600 s of fake time
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertGreater(len(calls), 3)
        self.assertEqual(gaps, sorted(gaps))                # never speeds up
        self.assertLessEqual(max(gaps), 600 + 10)            # capped
        self.assertGreater(gaps[-1], gaps[0])

    def test_success_keeps_the_configured_cadence(self):
        calls = self.run_worker(lambda n, out: None, 12)    # 120 s, interval 30 s
        self.assertEqual(len(calls), 4)

    def test_store_if_changed_caches_identical_captures_and_is_atomic(self):
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 200
        with tempfile.TemporaryDirectory() as d:
            path = d + "/c.png"
            self.assertTrue(bc._store_if_changed(path, png))
            m1 = os.stat(path).st_mtime_ns
            time.sleep(0.01)
            self.assertFalse(bc._store_if_changed(path, png))        # unchanged -> untouched
            self.assertEqual(os.stat(path).st_mtime_ns, m1)
            self.assertTrue(bc._store_if_changed(path, png + b"y"))
            self.assertEqual([f for f in os.listdir(d)], ["c.png"])   # no temp leftovers

    def test_last_valid_image_survives_failures(self):
        with tempfile.TemporaryDirectory() as d:
            path = bc.png_path(d, "r", 0)
            open(path, "wb").write(b"\x89PNG\r\n\x1a\nGOOD")

            def fail(*a, **k):
                raise bc.CaptureError("down")
            clock = [0.0]
            ticks = [0]

            def sleep(_):
                ticks[0] += 1
                clock[0] += 31

            bc.worker("r", [(0, self.overlay())], d, lambda: ticks[0] >= 5, sleep=sleep,
                      now=lambda: clock[0], do_capture=fail)
            self.assertEqual(open(path, "rb").read(), b"\x89PNG\r\n\x1a\nGOOD")


@unittest.skipUnless(sys.platform.startswith("linux"), "process groups need Linux")
class OrphanCleanupTests(unittest.TestCase):
    def test_kill_tree_removes_children_too(self):
        proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60 & wait"], start_new_session=True)
        time.sleep(0.5)
        kids = subprocess.run(["pgrep", "-g", str(proc.pid)], capture_output=True, text=True).stdout.split()
        self.assertGreaterEqual(len(kids), 3)
        bc._kill_tree(proc)
        time.sleep(0.3)
        left = subprocess.run(["pgrep", "-g", str(proc.pid)], capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [])


# ---------------------------------------------------------------------------
# REAL browser (CI browser job provides BLUESTREAM_BROWSER_BIN)
# ---------------------------------------------------------------------------
class _Page(BaseHTTPRequestHandler):
    hits = {"internal": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/hang"):
            time.sleep(30)
            return
        body = ("<html><body style='margin:0;background:#fff'>"
                "<div id='target' style='position:absolute;left:40px;top:30px;width:200px;height:100px;background:#e00'></div>"
                "<img src='http://127.0.0.2:%d/secret.png'>"
                "</body></html>" % self.server.internal_port).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Internal(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _Page.hits["internal"] += 1
        self.send_response(404)
        self.end_headers()


def serve(handler, host="127.0.0.1"):
    srv = HTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(REAL, "set BLUESTREAM_BROWSER_BIN to a Chromium binary on Linux")
class RealChromiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Page.hits["internal"] = 0
        try:
            cls.internal = serve(_Internal, "127.0.0.2")
        except OSError:
            raise unittest.SkipTest("127.0.0.2 not bindable")
        cls.page = serve(_Page)
        cls.page.internal_port = cls.internal.server_address[1]
        cls.url = "http://127.0.0.1:%d/" % cls.page.server_address[1]
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.page.shutdown()
        cls.internal.shutdown()
        cls.tmp.cleanup()

    def cap(self, selector, **kw):
        out = os.path.join(self.tmp.name, "o%d.png" % time.monotonic_ns())
        changed = bc.capture(self.url, selector, kw.pop("w", 800), kw.pop("h", 600), out,
                             resolve=lambda h, p: ["127.0.0.1"], browser=BIN, allow_no_sandbox=True,
                             test_mode=True, timeout=kw.pop("timeout", 40), settle=0.3)
        return out, changed

    def test_element_capture_has_the_elements_size(self):
        out, changed = self.cap("#target")
        self.assertTrue(changed)
        self.assertEqual(png_size(out), (200, 100))

    def test_viewport_capture_has_the_viewport_size(self):
        out, _ = self.cap("")
        self.assertEqual(png_size(out), (800, 600))

    def test_missing_selector_fails_cleanly(self):
        with self.assertRaises(bc.CaptureError):
            self.cap("#does-not-exist")

    def test_page_cannot_reach_internal_addresses_through_the_browser(self):
        before = _Page.hits["internal"]
        self.cap("#target")
        self.assertEqual(_Page.hits["internal"], before, "browser contacted an unlisted internal address")

    def test_identical_second_capture_is_not_rewritten(self):
        out = os.path.join(self.tmp.name, "same.png")
        kw = dict(resolve=lambda h, p: ["127.0.0.1"], browser=BIN, allow_no_sandbox=True, test_mode=True, settle=0.3)
        self.assertTrue(bc.capture(self.url, "#target", 800, 600, out, **kw))
        self.assertFalse(bc.capture(self.url, "#target", 800, 600, out, **kw))

    def test_hanging_page_times_out_and_leaves_no_browser_processes(self):
        out = os.path.join(self.tmp.name, "hang.png")
        started = time.monotonic()
        with self.assertRaises(bc.CaptureError):
            bc.capture(self.url + "hang", "", 800, 600, out, resolve=lambda h, p: ["127.0.0.1"], browser=BIN,
                       allow_no_sandbox=True, test_mode=True, timeout=6, settle=0.1)
        self.assertLess(time.monotonic() - started, 20)
        time.sleep(1.0)
        leftovers = []
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                cmd = open("/proc/%s/cmdline" % pid, "rb").read()
            except OSError:
                continue
            if b"bsbrowser-" in cmd:
                leftovers.append(pid)
        self.assertEqual(leftovers, [], "orphaned browser processes remain")
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
