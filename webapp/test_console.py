"""Focused tests for the GUI-1A.2 local read-only Flask console.

Run from the repository root::

    python -m unittest webapp.test_console -v

Uses Flask's test client and temporary state only. Never touches /etc/bluestream,
real relays/playlists, or any production configuration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webapp import app as app_module
from webapp import security
from webapp.engine import ALLOWED_OPERATIONS, EngineClient, EngineError
from flask import url_for
import webapp.engine as engine_module

ADMIN_PASSWORD = "correct-horse-battery-staple"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeEngine:
    """Controllable stand-in for the engine bridge client."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def call(self, operation):
        self.calls.append(operation)
        if operation not in self.payloads:
            raise EngineError("unsupported in fake: %s" % operation)
        value = self.payloads[operation]
        if isinstance(value, Exception):
            raise value
        return value


def make_state(tmpdir, password=ADMIN_PASSWORD):
    state = Path(tmpdir) / "state"
    if not (state / "admin.json").exists():
        security.init_admin(state, password)
    return state


def make_app(tmpdir, config=None, engine=None, password=ADMIN_PASSWORD):
    state = make_state(tmpdir, password=password)
    return app_module.create_app(state_dir=state, engine=engine, config=config), state


def get_csrf(client, path="/console/login"):
    rv = client.get(path)
    assert rv.status_code == 200, "login GET failed: %s" % rv.status_code
    m = CSRF_RE.search(rv.get_data(as_text=True))
    assert m, "csrf token not found on %s" % path
    return m.group(1)


def login(client, password=ADMIN_PASSWORD):
    token = get_csrf(client)
    return client.post(
        "/console/login",
        data={"username": "admin", "password": password, "csrf_token": token},
    )


def write_fake_webctl(tmpdir, name, body):
    path = Path(tmpdir) / name
    path.write_text(body, encoding="utf-8")
    return path


VALID_SNAPSHOT = {
    "version": "0.1.0",
    "hostname": "testhost",
    "uptime": "up 1 day",
    "domain": "example.com",
    "https_configured": "no",
    "nginx_active": "no",
    "ffmpeg_available": "yes",
    "ffprobe_available": "yes",
    "relay_count": "1",
    "playlist_count": "0",
}

DEFAULT_PAYLOADS = {
    "snapshot": VALID_SNAPSHOT,
    "relay_list": [
        {
            "name": "news",
            "type": "remote-hls",
            "active": "yes",
            "enabled": "no",
            "health": "HEALTHY",
        }
    ],
    "playlist_list": [],
}


class ConsoleAuthTests(unittest.TestCase):
    """Flask route behavior: auth flow, CSRF, sessions, dashboard."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app, self.state = make_app(self._tmp.name, engine=FakeEngine(DEFAULT_PAYLOADS))
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_01_import_and_create_app(self):
        # setUp() already created the app successfully; also sanity-check modules.
        self.assertIsNotNone(self.app)
        self.assertTrue(callable(app_module.create_app))
        self.assertTrue(callable(engine_module.EngineClient))

    def test_02_anonymous_dashboard_redirects_to_login(self):
        rv = self.client.get("/console/", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])

    def test_03_login_get_works(self):
        rv = self.client.get("/console/login")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Sign in", rv.get_data(as_text=True))

    def test_04_missing_csrf_on_login_rejected(self):
        rv = self.client.post(
            "/console/login", data={"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(rv.status_code, 400)

    def test_05_bad_password_rejected_generically(self):
        token = get_csrf(self.client)
        rv = self.client.post(
            "/console/login",
            data={"username": "admin", "password": "wrong-password", "csrf_token": token},
        )
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("Invalid username or password.", html)
        # no session established
        self.client.get("/console/", follow_redirects=False)

    def test_06_valid_password_login_succeeds(self):
        rv = login(self.client)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])

    def test_07_authenticated_dashboard_renders(self):
        login(self.client)
        rv = self.client.get("/console/")
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("Dashboard", html)
        self.assertIn("Relays", html)
        self.assertIn("news", html)
        self.assertIn("example.com", html)

    def test_08_logout_requires_csrf(self):
        login(self.client)
        rv = self.client.post("/console/logout")
        self.assertEqual(rv.status_code, 400)

    def test_09_valid_logout_clears_session(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        rv = self.client.post("/console/logout", data={"csrf_token": token})
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        # session is cleared: dashboard now requires login again
        rv = self.client.get("/console/", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])

    def test_10_dashboard_after_logout_requires_login(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        self.client.post("/console/logout", data={"csrf_token": token})
        rv = self.client.get("/console/", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])

    def test_console_root_redirects_to_slash(self):
        rv = self.client.get("/console", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])

    def test_static_urls_are_under_console_prefix(self):
        with self.app.test_request_context():
            static_url = url_for("static", filename="css/console.css")
        self.assertTrue(static_url.startswith("/console/static/"), static_url)

    def test_authenticated_user_visiting_login_redirects_to_dashboard(self):
        login(self.client)
        rv = self.client.get("/console/login", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])

    def test_security_headers_present(self):
        rv = self.client.get("/console/login")
        self.assertEqual(rv.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(rv.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(rv.headers.get("Referrer-Policy"), "same-origin")

    def test_session_cookie_flags(self):
        login(self.client)
        cookie = self.client.get_cookie("session", path="/console")
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie.key, "session")
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Lax")
        self.assertFalse(cookie.secure)  # local HTTP phase; Secure=True later
        self.assertEqual(cookie.path, "/console")  # scoped to the console prefix

    def test_engine_unavailable_shows_clean_message(self):
        broken = FakeEngine(
            {
                "snapshot": EngineError("secret-detail"),
                "relay_list": EngineError("secret-detail"),
                "playlist_list": EngineError("secret-detail"),
            }
        )
        app = app_module.create_app(state_dir=self.state, engine=broken)
        client = app.test_client()
        login(client)
        rv = client.get("/console/")
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("BlueStream engine information is temporarily unavailable.", html)
        self.assertNotIn("secret-detail", html)


class ConsoleRateLimitAndSafetyTests(unittest.TestCase):
    """Rate limiting, HTML escaping, and route-surface safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = make_state(self._tmp.name)
        self.engine = FakeEngine(DEFAULT_PAYLOADS)
        self.app = app_module.create_app(state_dir=self.state, engine=self.engine)

    def tearDown(self):
        self._tmp.cleanup()

    def test_11_rate_limiter_triggers_lockout(self):
        app = app_module.create_app(
            state_dir=self.state,
            engine=self.engine,
            config={
                "RATE_LIMIT_MAX_ATTEMPTS": 2,
                "RATE_LIMIT_WINDOW_SECONDS": 300,
                "RATE_LIMIT_LOCKOUT_SECONDS": 600,
            },
        )
        client = app.test_client()
        # two failed logins -> lockout
        for _ in range(2):
            token = get_csrf(client)
            client.post(
                "/console/login",
                data={"username": "admin", "password": "wrong", "csrf_token": token},
            )
        # even the correct password is now rejected generically
        token = get_csrf(client)
        rv = client.post(
            "/console/login",
            data={"username": "admin", "password": ADMIN_PASSWORD, "csrf_token": token},
        )
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Invalid username or password.", rv.get_data(as_text=True))

    def test_12_rate_limiter_state_remains_bounded(self):
        limiter = security.LoginRateLimiter(
            max_attempts=3, window_seconds=300, lockout_seconds=300, max_keys=20
        )
        for i in range(100):
            limiter.record_failure("ip-%d" % i)
        limiter.prune()
        self.assertLessEqual(limiter.size(), 20)

        # per-key entries are bounded and success clears state
        limiter2 = security.LoginRateLimiter(max_attempts=2, lockout_seconds=600)
        for _ in range(5):
            limiter2.record_failure("same-ip")
        self.assertTrue(limiter2.is_locked("same-ip"))
        self.assertLessEqual(limiter2.failure_count("same-ip"), 2)
        limiter2.record_success("same-ip")
        self.assertFalse(limiter2.is_locked("same-ip"))
        self.assertEqual(limiter2.failure_count("same-ip"), 0)

    def test_20_engine_values_with_html_chars_are_escaped(self):
        payloads = {
            "snapshot": {
                "version": "0.1.0",
                "hostname": "<b>bold-host</b>",
                "uptime": "up",
                "domain": "example.com",
                "https_configured": "no",
                "nginx_active": "no",
                "ffmpeg_available": "no",
                "ffprobe_available": "no",
                "relay_count": "1",
                "playlist_count": "0",
            },
            "relay_list": [
                {
                    "name": '<script>alert(1)</script>',
                    "type": "remote-hls",
                    "active": "yes",
                    "enabled": "no",
                    "health": "HEALTHY",
                }
            ],
            "playlist_list": [],
        }
        app = app_module.create_app(state_dir=self.state, engine=FakeEngine(payloads))
        client = app.test_client()
        login(client)
        html = client.get("/console/").get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;b&gt;bold-host&lt;/b&gt;", html)
        self.assertNotIn("<b>bold-host</b>", html)

    def test_21_no_mutation_routes_exist(self):
        forbidden = (
            "start", "stop", "restart", "enable", "disable",
            "create", "edit", "delete", "upload", "restore",
            "nginx", "ssl", "firewall",
        )
        endpoints = set()
        for rule in self.app.url_map.iter_rules():
            endpoints.add(rule.endpoint)
            lower = rule.endpoint.lower()
            for word in forbidden:
                self.assertNotIn(word, lower)
        # the complete console surface is exactly: index, login(+post),
        # logout, dashboard (+ static)
        console = {e for e in endpoints if e.startswith("console.")}
        self.assertEqual(
            console,
            {
                "console.index",
                "console.login",
                "console.login_post",
                "console.logout",
                "console.dashboard",
            },
        )
        # only the expected HTTP methods
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("console."):
                allowed = {"GET", "POST", "HEAD", "OPTIONS"}
                self.assertTrue(rule.methods.issubset(allowed), rule)


class EngineClientTests(unittest.TestCase):
    """web-ctl bridge client: allowlist, argv/shell=False, failure handling."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bash = engine_module.default_bash_path()

    def tearDown(self):
        self._tmp.cleanup()

    def test_15_engine_client_rejects_unsupported_operation(self):
        client = EngineClient(web_ctl=Path(self._tmp.name) / "web-ctl", bash="/bin/bash")
        for op in ("relay_start", "relay_stop", "delete", "nginx reload", "upload"):
            with self.assertRaises(EngineError):
                client.call(op)
        self.assertEqual(
            ALLOWED_OPERATIONS,
            frozenset({"version", "snapshot", "relay_list", "playlist_list"}),
        )

    def test_16_engine_client_uses_shell_false_and_argv(self):
        fake = write_fake_webctl(
            self._tmp.name,
            "web-ctl",
            "printf '%s\\n' '{\"ok\": true, \"data\": {\"version\": \"0.1.0\"}}'\n",
        )
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = b'{"ok": true, "data": {"version": "0.1.0"}}'
                stderr = b""

            return Result()

        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(web_ctl=fake, bash="/bin/bash")
            data = client.call("version")
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(data, {"version": "0.1.0"})
        self.assertIs(calls["kwargs"]["shell"], False)
        self.assertEqual(calls["cmd"], ["/bin/bash", str(fake), "version"])

    def test_17_malformed_webctl_json_handled_safely(self):
        fake = write_fake_webctl(
            self._tmp.name, "web-ctl", "printf '%s\\n' 'this is not json'\n"
        )
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError):
            client.call("snapshot")

    def test_18_nonzero_webctl_exit_handled_safely(self):
        fake = write_fake_webctl(self._tmp.name, "web-ctl", "exit 3\n")
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError):
            client.call("snapshot")

    @unittest.skipUnless(
        os.name == "posix", "real subprocess timeout needs a POSIX host"
    )
    def test_19_timeout_handled_safely(self):
        fake = write_fake_webctl(self._tmp.name, "web-ctl", "while true; do :; done\n")
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=1.0)
        with self.assertRaises(EngineError):
            client.call("snapshot")

    def test_timeout_expired_is_converted_to_engine_error(self):
        # Deterministic TimeoutExpired path (works on every platform).
        original = engine_module.subprocess.run

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(
                web_ctl=Path(self._tmp.name) / "web-ctl", bash="/bin/bash"
            )
            with self.assertRaises(EngineError):
                client.call("snapshot")
        finally:
            engine_module.subprocess.run = original

    def test_failure_envelope_even_with_exit_zero(self):
        fake = write_fake_webctl(
            self._tmp.name,
            "web-ctl",
            "printf '%s\\n' '{\"ok\": false, \"error\": \"boom\", \"code\": \"X\"}'\n",
        )
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError):
            client.call("snapshot")

    def test_missing_data_field_rejected(self):
        fake = write_fake_webctl(
            self._tmp.name, "web-ctl", "printf '%s\\n' '{\"ok\": true}'\n"
        )
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError):
            client.call("snapshot")

    def test_missing_web_ctl_file_rejected(self):
        client = EngineClient(web_ctl=Path(self._tmp.name) / "nope", bash=self.bash)
        with self.assertRaises(EngineError):
            client.call("version")


class SecurityTests(unittest.TestCase):
    """Password storage, secret key persistence, and the admin CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_13_password_file_contains_hash_and_salt_not_plaintext(self):
        state = Path(self._tmp.name) / "pwd"
        secret_password = "s3cret-P@ssw0rd-\U0001F986!"
        security.init_admin(state, secret_password)
        record = security.load_admin_record(state)
        self.assertEqual(record["username"], "admin")
        self.assertEqual(record["algorithm"], "scrypt")
        self.assertTrue(record["salt"])
        self.assertTrue(record["hash"])
        raw = (state / "admin.json").read_text(encoding="utf-8")
        self.assertNotIn(secret_password, raw)
        # verification works and rejects wrong/malformed records
        self.assertTrue(security.verify_password(secret_password, record))
        self.assertFalse(security.verify_password("wrong-password", record))
        self.assertFalse(security.verify_password(secret_password, None))
        self.assertFalse(security.verify_password(secret_password, {"algorithm": "nope"}))
        # never silently overwrites
        with self.assertRaises(FileExistsError):
            security.init_admin(state, "another-password")

    def test_14_secret_key_persists_between_instances(self):
        state = Path(self._tmp.name) / "secret"
        app1 = app_module.create_app(state_dir=state)
        key1 = app1.secret_key
        app2 = app_module.create_app(state_dir=state)
        self.assertEqual(key1, app2.secret_key)
        self.assertEqual(len(key1), 64)
        self.assertEqual(key1, app2.config["SECRET_KEY"])

    def test_cli_init_admin_reads_password_from_stdin_only(self):
        state = Path(self._tmp.name) / "cli"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "webapp.security",
                "init-admin",
                "--state-dir",
                str(state),
            ],
            input=b"cli-secret-password\n",
            capture_output=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record = security.load_admin_record(state)
        self.assertIsNotNone(record)
        raw = (state / "admin.json").read_text(encoding="utf-8")
        self.assertNotIn("cli-secret-password", raw)
        self.assertTrue(security.verify_password("cli-secret-password", record))


class IntegrationTest(unittest.TestCase):
    """End-to-end: Flask -> real EngineClient -> web-ctl -> JSON -> dashboard."""

    def test_real_engine_dashboard_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = make_state(tmp)
            app = app_module.create_app(state_dir=state)  # real engine client
            client = app.test_client()
            login(client)
            rv = client.get("/console/")
            self.assertEqual(rv.status_code, 200)
            html = rv.get_data(as_text=True)
            self.assertIn("Dashboard", html)
            self.assertIn("0.1.0", html)  # version from the real bridge


if __name__ == "__main__":
    unittest.main()
