"""Security tests for Studio: SSRF guard (connect-time), data overlays, injection."""
import unittest
from unittest import mock

from webapp.studio import ffmpeg as sff
from webapp.studio import netguard, widgets
from webapp.studio.model import Overlay, Project, ValidationError


def addrinfo(*ips):
    return [(2, 1, 6, "", (ip, 80)) for ip in ips]


class IpClassificationTests(unittest.TestCase):
    def test_non_public_addresses_blocked(self):
        for ip in ("127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
                   "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1",
                   "224.0.0.1", "not-an-ip"):
            self.assertFalse(netguard.ip_is_public(ip), ip)

    def test_public_addresses_allowed(self):
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(netguard.ip_is_public(ip), ip)


class UrlValidationTests(unittest.TestCase):
    def test_bad_urls(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/", "http://u:p@example.com/",
                    "http://example.com:8080/", "http://example.com:22/", "http:///nohost", "//example.com"):
            with self.assertRaises(netguard.BlockedError, msg=url):
                netguard.validate_url(url)

    def test_good_urls(self):
        self.assertEqual(netguard.validate_url("https://example.com/a?b=1")[1], 443)
        self.assertEqual(netguard.validate_url("http://example.com/a")[1], 80)


class ResolveTests(unittest.TestCase):
    def test_any_private_answer_blocks_the_host(self):
        # mixed public + private answers (classic rebinding set-up) must be refused
        with mock.patch("socket.getaddrinfo", return_value=addrinfo("8.8.8.8", "127.0.0.1")):
            with self.assertRaises(netguard.BlockedError):
                netguard.resolve_public("evil.example", 80)

    def test_decimal_and_octal_loopback_forms_blocked_after_resolution(self):
        # the OS resolver maps these forms to 127.0.0.1; the post-resolution check catches them
        with mock.patch("socket.getaddrinfo", return_value=addrinfo("127.0.0.1")):
            for host in ("2130706433", "0x7f.1", "017700000001", "localhost"):
                with self.assertRaises(netguard.BlockedError, msg=host):
                    netguard.resolve_public(host, 80)

    def test_public_resolves(self):
        with mock.patch("socket.getaddrinfo", return_value=addrinfo("93.184.216.34")):
            self.assertEqual(netguard.resolve_public("example.com", 80), ["93.184.216.34"])


class FetchTests(unittest.TestCase):
    class Resp:
        def __init__(self, status=200, body=b"{}", loc=None):
            self.status, self._body, self._loc = status, body, loc

        def getheader(self, name):
            return self._loc

        def read(self, n):
            return self._body[:n]

    class Conn:
        sequence = []
        connected_to = []

        def __init__(self, host, ip, port, timeout):
            FetchTests.Conn.connected_to.append(ip)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return FetchTests.Conn.sequence.pop(0)

        def close(self):
            pass

    def run_fetch(self, url, responses, dns):
        self.Conn.sequence = list(responses)
        self.Conn.connected_to = []
        with mock.patch("socket.getaddrinfo", side_effect=dns), \
                mock.patch.object(netguard, "_PinnedHTTP", self.Conn), \
                mock.patch.object(netguard, "_PinnedHTTPS", self.Conn):
            return netguard.fetch(url)

    def test_connects_to_the_validated_ip_only(self):
        body = self.run_fetch("http://example.com/x", [self.Resp(body=b'{"a":1}')],
                              lambda *a, **k: addrinfo("93.184.216.34"))
        self.assertEqual(body, b'{"a":1}')
        self.assertEqual(self.Conn.connected_to, ["93.184.216.34"])

    def test_redirect_to_metadata_service_blocked(self):
        def dns(host, *a, **k):
            return addrinfo("169.254.169.254" if host == "metadata.internal" else "93.184.216.34")
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x",
                           [self.Resp(302, loc="http://metadata.internal/latest")], dns)

    def test_redirect_to_loopback_ip_literal_blocked(self):
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x", [self.Resp(302, loc="http://127.0.0.1/admin")],
                           lambda host, *a, **k: addrinfo("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"))

    def test_redirect_scheme_downgrade_to_file_blocked(self):
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x", [self.Resp(302, loc="file:///etc/passwd")],
                           lambda *a, **k: addrinfo("93.184.216.34"))

    def test_redirect_loop_and_oversize_and_status(self):
        loop = [self.Resp(302, loc="http://example.com/y")] * 10
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x", loop, lambda *a, **k: addrinfo("93.184.216.34"))
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x", [self.Resp(body=b"x" * (netguard.MAX_BYTES + 5))],
                           lambda *a, **k: addrinfo("93.184.216.34"))
        with self.assertRaises(netguard.BlockedError):
            self.run_fetch("http://example.com/x", [self.Resp(500)], lambda *a, **k: addrinfo("93.184.216.34"))


class ExtractTests(unittest.TestCase):
    def test_paths(self):
        doc = {"items": [{"count": 12345}], "name": "x", "nested": {"a": {"b": True}}}
        self.assertEqual(netguard.extract(doc, "items.0.count"), "12345")
        self.assertEqual(netguard.extract(doc, "nested.a.b"), "True")
        for bad in ("items", "nested", "missing", "items.9.count"):
            with self.assertRaises((KeyError, IndexError)):
                netguard.extract(doc, bad)


class DataOverlayTests(unittest.TestCase):
    def test_validation(self):
        ok = Overlay("data", text="Subs: {value}", url="https://example.com/a.json", json_path="a.b")
        Project("p", "media", "a.mp4", [ok]).validate()
        for bad in (Overlay("data", url="ftp://x/y", json_path="a"), Overlay("data", url="https://x/y", json_path="a b;"),
                    Overlay("data", url="https://x/y", json_path="a", refresh_s=1)):
            with self.assertRaises(ValidationError):
                Project("p", "media", "a.mp4", [bad]).validate()

    def test_render_uses_last_good_value_and_placeholder(self):
        from datetime import datetime, timezone
        o = Overlay("data", text="Subs: {value}", url="https://x/y", json_path="a")
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(widgets.render_text(o, now, data_value="1200"), "Subs: 1200")
        self.assertEqual(widgets.render_text(o, now), "Subs: --")


class InjectionTests(unittest.TestCase):
    PAYLOADS = ["$(id)", "`id`", "'; rm -rf / ;'", "%{pts}", "\\", ":", "a:b=c,d", "\n-i /etc/passwd"]

    def test_user_text_cannot_reach_argv_or_filtergraph(self):
        def argv_for(text):
            p = Project("p", "media", "a.mp4", [Overlay("text", text=text)]).validate()
            return sff.build_live_args(p, "rtmp://127.0.0.1:1935/bluestream-relay/src",
                                       "rtmp://127.0.0.1:1935/bluestream-relay/out",
                                       "/run/w", "/var/lib/media")
        baseline = argv_for("x")
        # Text is delivered through a reloaded file, so NO payload may change the argv at all.
        for payload in self.PAYLOADS:
            self.assertEqual(argv_for(payload), baseline, payload)

    def test_font_name_and_asset_traversal_rejected(self):
        for kw in ({"font": "x:fontfile=/etc/passwd"}, {"font": "a'b"}):
            with self.assertRaises(ValidationError):
                Project("p", "media", "a.mp4", [Overlay("text", **kw)]).validate()
        for asset in ("../../etc/passwd", "/etc/passwd", "a/b.png", ".hidden", "a;b.png"):
            with self.assertRaises(ValidationError):
                Project("p", "media", "a.mp4", [Overlay("image", asset=asset)]).validate()

    def test_media_source_must_be_a_plain_filename(self):
        from webapp.studio import runtime
        for ref in ("../x.mp4", "/etc/passwd", "a/b.mp4"):
            p = Project("p", "media", ref)
            with self.assertRaises(ValueError):
                runtime.resolve_source(p)


if __name__ == "__main__":
    unittest.main()


class SourceHygieneTests(unittest.TestCase):
    """Guards against control characters/NUL bytes sneaking into shell sources
    (bash silently drops NUL, which once corrupted a printf format)."""

    def test_no_control_bytes_in_shell_sources(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        bad_re = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
        files = [root / "web-ctl", root / "install.sh", root / "uninstall.sh"]
        files += list((root / "lib").glob("*.sh")) + list((root / "config" / "systemd").glob("*.sh"))
        for f in files:
            self.assertIsNone(bad_re.search(f.read_bytes()), "control byte in %s" % f.name)


class StoreHardeningTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict("os.environ", {"BLUESTREAM_STUDIO_DIR": self.tmp.name + "/s"})
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(__import__("os").name == "nt", "symlinks need privileges on Windows")
    def test_symlinked_project_file_is_refused(self):
        import os
        from webapp.studio import store
        os.makedirs(self.tmp.name + "/s")
        secret = self.tmp.name + "/other-service.json"
        open(secret, "w").write("{}")
        os.symlink(secret, self.tmp.name + "/s/evil.json")
        with self.assertRaises(ValidationError):
            store.load_published("evil")

    def test_oversized_project_file_is_refused(self):
        import os
        from webapp.studio import store
        os.makedirs(self.tmp.name + "/s")
        open(self.tmp.name + "/s/big.json", "w").write(" " * (600 * 1024))
        with self.assertRaises(ValidationError):
            store.load_published("big")

    @unittest.skipIf(__import__("os").name == "nt", "needs flock (POSIX); Linux CI covers it")
    def test_concurrent_publishes_get_distinct_revisions(self):
        import threading
        from webapp.studio import store
        store.save_draft(Project("race", "media", "a.mp4").validate())
        results = []

        def go():
            results.append(store.publish("race"))
        threads = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 6)
        self.assertEqual(sorted(results), list(range(1, 7)))      # flock serialises the writers
        self.assertEqual(store.load_published("race").revision, max(results))


class AssetTypeTests(unittest.TestCase):
    def test_assets_must_have_the_right_kind_of_extension(self):
        bad = [Overlay("image", asset="notes.txt"), Overlay("image", asset="clip.mp4"), Overlay("video", asset="pic.png"),
               Overlay("video", asset="script.sh")]
        for o in bad:
            with self.assertRaises(ValidationError, msg=o.asset):
                Project("p", "media", "a.mp4", [o]).validate()
        for ref in ("a.txt", "a.mp4"):
            with self.assertRaises(ValidationError):
                Project("p", "audio", ref).validate()
        with self.assertRaises(ValidationError):
            Project("p", "slideshow", "a.png,b.txt").validate()
        Project("p", "media", "a.mp4", [Overlay("image", asset="logo.PNG"), Overlay("video", asset="x.webm")]).validate()
