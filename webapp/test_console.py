"""Focused tests for the GUI-1A.2 local read-only Flask console.

Run from the repository root::

    python -m unittest webapp.test_console -v

Uses Flask's test client and temporary state only. Never touches /etc/bluestream,
real relays/playlists, or any production configuration.
"""

from __future__ import annotations

import json
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
from webapp.engine import (
    ALLOWED_MUTATION_OPERATIONS,
    ALLOWED_OPERATIONS,
    EngineClient,
    EngineError,
    valid_target_name,
)
from flask import request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
import webapp.engine as engine_module

ADMIN_PASSWORD = "correct-horse-battery-staple"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeEngine:
    """Controllable stand-in for the engine bridge client."""

    def __init__(self, payloads, mutation_payloads=None):
        self.payloads = payloads
        self.mutation_payloads = mutation_payloads or {}
        self.calls = []
        self.mutation_calls = []

    def call(self, operation):
        self.calls.append(operation)
        if operation not in self.payloads:
            raise EngineError("unsupported in fake: %s" % operation)
        value = self.payloads[operation]
        if isinstance(value, Exception):
            raise value
        return value

    def _mutation(self, operation, name):
        self.mutation_calls.append((operation, name))
        value = self.mutation_payloads.get(
            (operation, name), {"operation": operation, "name": name}
        )
        if isinstance(value, Exception):
            raise value
        return value

    def relay_start(self, name):
        return self._mutation("relay_start", name)

    def relay_stop(self, name):
        return self._mutation("relay_stop", name)

    def relay_restart(self, name):
        return self._mutation("relay_restart", name)

    def playlist_start(self, name):
        return self._mutation("playlist_start", name)

    def playlist_stop(self, name):
        return self._mutation("playlist_stop", name)

    def playlist_restart(self, name):
        return self._mutation("playlist_restart", name)


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

    def test_21_route_surface_only_allowed_endpoints(self):
        # GUI-1B.1 adds exactly the six lifecycle endpoints; everything else
        # that mutates (create/edit/delete/upload/restore/...) stays forbidden.
        forbidden = (
            "enable", "disable",
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
        # logout, dashboard, the six GUI-1B.1 lifecycle endpoints (+ static)
        console = {e for e in endpoints if e.startswith("console.")}
        self.assertEqual(
            console,
            {
                "console.index",
                "console.login",
                "console.login_post",
                "console.logout",
                "console.dashboard",
                "console.relay_start",
                "console.relay_stop",
                "console.relay_restart",
                "console.playlist_start",
                "console.playlist_stop",
                "console.playlist_restart",
            },
        )
        # only the expected HTTP methods
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint.startswith("console."):
                allowed = {"GET", "POST", "HEAD", "OPTIONS"}
                self.assertTrue(rule.methods.issubset(allowed), rule)


class LifecycleActionTests(unittest.TestCase):
    """GUI-1B.1 authenticated lifecycle routes: POST, CSRF, Post/Redirect/Get."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(DEFAULT_PAYLOADS)
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=self.engine
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_01_anonymous_lifecycle_post_redirects_to_login(self):
        token = get_csrf(self.client)  # anonymous session still has a token
        rv = self.client.post(
            "/console/relays/news/start", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [])

    def test_02_lifecycle_routes_reject_get(self):
        login(self.client)
        for path in (
            "/console/relays/news/start",
            "/console/relays/news/stop",
            "/console/relays/news/restart",
            "/console/playlists/pl/start",
            "/console/playlists/pl/stop",
            "/console/playlists/pl/restart",
        ):
            rv = self.client.get(path)
            self.assertEqual(rv.status_code, 405, path)

    def test_03_missing_csrf_executes_nothing(self):
        login(self.client)
        rv = self.client.post("/console/relays/news/start")
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_04_invalid_csrf_executes_nothing(self):
        login(self.client)
        rv = self.client.post(
            "/console/relays/news/start", data={"csrf_token": "wrong-token"}
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_05_route_fixes_operation_not_form_data(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        rv = self.client.post(
            "/console/relays/news/restart",
            data={"csrf_token": token, "operation": "relay_delete", "name": "other"},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])
        # the fixed route operation wins; form data is ignored
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "news")])

    def test_06_invalid_target_name_rejected_server_side(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        for name in ("--help", "a%20b", "UPPER", "..%2Fevil", "x%3Brm", "x" * 49):
            rv = self.client.post(
                "/console/relays/%s/start" % name, data={"csrf_token": token}
            )
            # routed-but-invalid names redirect; unrouteable names 404 - the
            # invariant is that nothing reaches the engine either way
            self.assertIn(rv.status_code, (302, 404), name)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_07_successful_action_post_redirect_get(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        rv = self.client.post(
            "/console/relays/news/start", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [("relay_start", "news")])
        # follow the redirect: dashboard re-renders fresh state with the flash
        rv = self.client.get("/console/")
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("Dashboard", html)
        self.assertIn("Relay &#39;news&#39; started successfully.", html)

    def test_08_engine_failure_redirects_without_traceback(self):
        engine = FakeEngine(
            DEFAULT_PAYLOADS,
            mutation_payloads={("relay_restart", "news"): EngineError("secret-detail")},
        )
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=engine
        )
        client = app.test_client()
        login(client)
        token = get_csrf(client, path="/console/")
        rv = client.post("/console/relays/news/restart", data={"csrf_token": token})
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/", rv.headers["Location"])
        self.assertEqual(engine.mutation_calls, [("relay_restart", "news")])
        rv = client.get("/console/")
        html = rv.get_data(as_text=True)
        self.assertIn("Relay &#39;news&#39; restart failed.", html)
        self.assertNotIn("secret-detail", html)
        self.assertNotIn("Traceback", html)

    def test_09_all_six_operations_map_to_exact_methods(self):
        login(self.client)
        token = get_csrf(self.client, path="/console/")
        for path in (
            "/console/relays/news/start",
            "/console/relays/news/stop",
            "/console/relays/news/restart",
            "/console/playlists/pl/start",
            "/console/playlists/pl/stop",
            "/console/playlists/pl/restart",
        ):
            rv = self.client.post(path, data={"csrf_token": token})
            self.assertEqual(rv.status_code, 302, path)
        self.assertEqual(
            sorted(self.engine.mutation_calls),
            [
                ("playlist_restart", "pl"),
                ("playlist_start", "pl"),
                ("playlist_stop", "pl"),
                ("relay_restart", "news"),
                ("relay_start", "news"),
                ("relay_stop", "news"),
            ],
        )

    def test_10_dashboard_shows_controls_with_csrf(self):
        login(self.client)
        html = self.client.get("/console/").get_data(as_text=True)
        self.assertIn("/console/relays/news/start", html)
        self.assertIn("/console/relays/news/stop", html)
        self.assertIn("/console/relays/news/restart", html)
        # every action form carries its own CSRF token (3 actions + logout)
        self.assertGreaterEqual(html.count('name="csrf_token"'), 4)
        # no create/edit/delete controls yet
        self.assertNotIn("Delete", html)
        self.assertNotIn("Edit", html)
        self.assertNotIn("Add", html)


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

    # ------------------------------------------------------------------
    # GUI-1B.1: lifecycle mutations
    # ------------------------------------------------------------------
    def test_20_mutation_methods_are_exactly_six_and_fixed(self):
        client = EngineClient(
            web_ctl=Path(self._tmp.name) / "web-ctl", bash="/bin/bash"
        )
        for name in (
            "relay_start",
            "relay_stop",
            "relay_restart",
            "playlist_start",
            "playlist_stop",
            "playlist_restart",
        ):
            self.assertTrue(callable(getattr(client, name, None)), name)
        for forbidden in (
            "relay_create", "relay_edit", "relay_delete",
            "playlist_create", "playlist_edit", "playlist_upload",
        ):
            self.assertFalse(hasattr(client, forbidden), forbidden)
        self.assertEqual(
            ALLOWED_MUTATION_OPERATIONS,
            frozenset(
                {
                    "relay_start",
                    "relay_stop",
                    "relay_restart",
                    "playlist_start",
                    "playlist_stop",
                    "playlist_restart",
                }
            ),
        )
        # the read-only call() surface still rejects lifecycle operations
        for op in ("relay_start", "playlist_stop", "create", "delete"):
            with self.assertRaises(EngineError):
                client.call(op)

    def test_21_mutation_uses_shell_false_argv_and_validated_name(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = (
                    b'{"ok": true, "data": {"operation": "relay_start",'
                    b' "name": "news"}}'
                )
                stderr = b""

            return Result()

        fake = Path(self._tmp.name) / "web-ctl"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(web_ctl=fake, bash="/bin/bash")
            data = client.relay_start("news")
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(data, {"operation": "relay_start", "name": "news"})
        self.assertEqual(
            calls["cmd"], ["/bin/bash", str(fake), "relay_start", "news"]
        )
        self.assertIs(calls["kwargs"]["shell"], False)

    def test_22_mutation_invalid_name_rejected_before_subprocess(self):
        called = []

        def fake_run(cmd, **kwargs):
            called.append(cmd)
            raise AssertionError("subprocess must not be executed")

        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(
                web_ctl=Path(self._tmp.name) / "web-ctl", bash="/bin/bash"
            )
            for name in ("--help", "../evil", "x;rm", "a b", "UPPER", "", "x" * 49):
                for method in (client.relay_start, client.playlist_restart):
                    with self.assertRaises(EngineError):
                        method(name)
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(called, [])

    def test_23_mutation_nonzero_exit_with_json_envelope_reports_error(self):
        fake = write_fake_webctl(
            self._tmp.name,
            "web-ctl",
            "printf '%s\\n' '{\"ok\": false, \"error\": \"relay not found\","
            " \"code\": \"NOT_FOUND\"}'\nexit 1\n",
        )
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError) as ctx:
            client.relay_start("nope")
        self.assertIn("relay not found", str(ctx.exception))

    def test_24_mutation_invalid_json_handled_safely(self):
        fake = write_fake_webctl(
            self._tmp.name, "web-ctl", "printf '%s\\n' 'not json'\n"
        )
        client = EngineClient(web_ctl=fake, bash=self.bash, timeout=5)
        with self.assertRaises(EngineError):
            client.relay_start("news")

    def test_25_mutation_timeout_handled_safely(self):
        original = engine_module.subprocess.run

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

        fake = Path(self._tmp.name) / "web-ctl"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(
                web_ctl=fake,
                bash="/bin/bash",
                timeout=1.0,
            )
            with self.assertRaises(EngineError):
                client.relay_start("news")
        finally:
            engine_module.subprocess.run = original

    def test_26_production_mutation_argv_fixed_sudo_webctl(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = (
                    b'{"ok": true, "data": {"operation": "playlist_restart",'
                    b' "name": "pl"}}'
                )
                stderr = b""

            return Result()

        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(production=True)
            data = client.playlist_restart("pl")
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(
            calls["cmd"],
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/local/lib/bluestream/web-ctl",
                "playlist_restart",
                "pl",
            ],
        )
        self.assertIs(calls["kwargs"]["shell"], False)

    def test_27_valid_target_name_matches_engine_rule(self):
        for good in ("test1080", "a", "a-b_c9", "x" * 48):
            self.assertTrue(valid_target_name(good), good)
        for bad in (
            "--help",
            "../x",
            "x;rm",
            "a b",
            "A",
            "",
            "x" * 49,
            None,
            "a/b",
            "a@b",
        ):
            self.assertFalse(valid_target_name(bad), bad)


class WebCtlArgvBoundaryTests(unittest.TestCase):
    """GUI-1B.1: lifecycle operations accept EXACTLY two argv items.

    Executes the real web-ctl bridge through a trusted bash. Every cardinality
    case is fail-closed and non-destructive: invalid targets never reach a real
    relay/playlist, and the single valid-name case only reaches the existence
    check (which fails in a dev checkout - never a real start/stop).
    """

    OPS = (
        "relay_start",
        "relay_stop",
        "relay_restart",
        "playlist_start",
        "playlist_stop",
        "playlist_restart",
    )

    def _run(self, *args):
        bash = engine_module.default_bash_path()
        proc = subprocess.run(
            [bash, str(REPO_ROOT / "web-ctl"), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            cwd=str(REPO_ROOT),
        )
        doc = json.loads(proc.stdout.decode("utf-8"))
        return proc.returncode, doc

    def test_01_valid_two_argv_reaches_existence_handling(self):
        rc, doc = self._run("relay_start", "zz-argcount-probe")
        self.assertEqual(rc, 1)  # fails closed (no such relay in a dev checkout)
        self.assertIs(doc["ok"], False)
        # NOT a cardinality/validation rejection: the exact-2 form progressed
        # past dispatch and name validation to the existence check.
        self.assertEqual(doc["code"], "NOT_FOUND")
        self.assertNotIn(
            doc["code"],
            ("MISSING_TARGET", "TOO_MANY_ARGUMENTS", "INVALID_NAME",
             "UNKNOWN_OPERATION"),
        )

    def test_02_missing_target_all_six_ops(self):
        for op in self.OPS:
            rc, doc = self._run(op)
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "MISSING_TARGET", op)

    def test_03_one_extra_arg_rejected(self):
        for op in ("relay_start", "playlist_restart"):
            rc, doc = self._run(op, "goodname", "extra")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS", op)

    def test_04_empty_third_argv_item_rejected(self):
        for op in ("relay_start", "playlist_stop"):
            rc, doc = self._run(op, "goodname", "")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS", op)

    def test_05_fourth_arg_not_hidden_by_empty_third(self):
        for op in ("relay_start", "playlist_start"):
            rc, doc = self._run(op, "goodname", "", "extra4")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS", op)

    def test_06_many_extra_args_rejected(self):
        for op in ("relay_start", "playlist_restart"):
            rc, doc = self._run(op, "goodname", "a", "b", "c")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS", op)

    def test_07_invalid_names_still_invalid(self):
        for bad in ("--help", "../x", "x;id"):
            rc, doc = self._run("relay_start", bad)
            self.assertEqual(rc, 1, bad)
            self.assertEqual(doc["code"], "INVALID_NAME", bad)

    def test_08_unknown_operation_still_unknown(self):
        rc, doc = self._run("relay_bogus")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "UNKNOWN_OPERATION")


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


class ProductionModeTests(unittest.TestCase):
    """GUI-1A.3A production runtime and privilege-separation plumbing."""

    FORBIDDEN = (
        # GUI-1B.1 allows exactly the six lifecycle operations (start/stop/
        # restart on relays/playlists); every other mutation word stays banned.
        "enable", "disable",
        "create", "edit", "delete", "upload", "restore",
        "nginx", "ssl", "firewall",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = make_state(self._tmp.name)
        self.repo_root = Path(__file__).resolve().parent.parent

    def tearDown(self):
        self._tmp.cleanup()

    def _prod_app(self, **kwargs):
        kwargs.setdefault("engine", FakeEngine(DEFAULT_PAYLOADS))
        return app_module.create_app(
            state_dir=self.state, production=True, **kwargs
        )

    def test_production_requires_explicit_state_dir(self):
        with self.assertRaises(ValueError):
            app_module.create_app(production=True)

    def test_proxyfix_enabled_only_in_production(self):
        local_app = app_module.create_app(state_dir=self.state)
        self.assertNotIsInstance(local_app.wsgi_app, ProxyFix)
        prod_app = self._prod_app()
        self.assertIsInstance(prod_app.wsgi_app, ProxyFix)

    def test_local_mode_ignores_spoofed_x_forwarded_for(self):
        local_app = app_module.create_app(state_dir=self.state)
        with local_app.test_request_context(
            "/console/login",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Forwarded-For": "203.0.113.7"},
        ):
            self.assertEqual(request.remote_addr, "127.0.0.1")
        # end-to-end: failed logins with different spoofed XFF still hit the
        # same real client key (no rate-limit bypass).
        client = local_app.test_client()
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            token = get_csrf(client)
            client.post(
                "/console/login",
                data={"username": "admin", "password": "wrong", "csrf_token": token},
                headers={"X-Forwarded-For": ip},
            )
        limiter = local_app.extensions["bluestream_limiter"]
        self.assertEqual(limiter.failure_count("127.0.0.1"), 3)

    def test_production_rate_limiter_uses_corrected_client_ip(self):
        # Requirement #8: with one trusted hop the rate limiter keys on the
        # proxy-corrected client IP (observed through the real WSGI stack).
        prod_app = self._prod_app()
        client = prod_app.test_client()
        token = get_csrf(client)
        client.post(
            "/console/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        limiter = prod_app.extensions["bluestream_limiter"]
        self.assertEqual(limiter.failure_count("203.0.113.7"), 1)
        self.assertEqual(limiter.failure_count("127.0.0.1"), 0)

    def test_production_does_not_trust_more_than_one_hop(self):
        prod_app = self._prod_app()
        client = prod_app.test_client()
        token = get_csrf(client)
        client.post(
            "/console/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
            headers={"X-Forwarded-For": "198.51.100.9, 203.0.113.7, 10.0.0.1"},
        )
        limiter = prod_app.extensions["bluestream_limiter"]
        # x_for=1 trusts exactly one hop: the LAST XFF entry (the value nginx
        # appended). Earlier client-spoofed values are ignored entirely.
        self.assertEqual(limiter.failure_count("10.0.0.1"), 1)
        self.assertEqual(limiter.failure_count("203.0.113.7"), 0)
        self.assertEqual(limiter.failure_count("198.51.100.9"), 0)

    def test_production_nginx_appended_xff_yields_real_client(self):
        # Mirrors nginx `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
        # plus ProxyFix(x_for=1): the client sends a spoofed leading value and
        # nginx appends the real peer. The last (nginx-appended) value wins.
        prod_app = self._prod_app()
        client = prod_app.test_client()
        token = get_csrf(client)
        client.post(
            "/console/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
            headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.7"},
        )
        limiter = prod_app.extensions["bluestream_limiter"]
        self.assertEqual(limiter.failure_count("198.51.100.7"), 1)
        self.assertEqual(limiter.failure_count("203.0.113.99"), 0)

    def test_production_secure_cookie_reads_web_conf(self):
        # Installer-written web.conf (derived from BlueStream SSL state) must
        # be honored; a missing/absent file stays fail-safe (Secure on).
        state = Path(self._tmp.name) / "webconf-state"
        security.init_admin(state, "pw")
        (state / "web.conf").write_text("secure_cookie=no\n", encoding="utf-8")
        prod_app = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertFalse(prod_app.config["SESSION_COOKIE_SECURE"])
        # garbage config stays fail-safe
        (state / "web.conf").write_text("secure_cookie=maybe\n", encoding="utf-8")
        prod_app2 = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertTrue(prod_app2.config["SESSION_COOKIE_SECURE"])
        # explicit call-site config still wins
        prod_app3 = app_module.create_app(
            state_dir=state,
            production=True,
            engine=FakeEngine(DEFAULT_PAYLOADS),
            config={"SESSION_COOKIE_SECURE": True},
        )
        self.assertTrue(prod_app3.config["SESSION_COOKIE_SECURE"])

    def test_local_cookie_secure_false_and_path_console(self):
        local_app = app_module.create_app(
            state_dir=self.state, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        client = local_app.test_client()
        login(client)
        cookie = client.get_cookie("session", path="/console")
        self.assertIsNotNone(cookie)
        self.assertFalse(cookie.secure)
        self.assertEqual(cookie.path, "/console")

    def test_production_secure_cookie_defaults_on_and_can_be_overridden(self):
        prod_app = self._prod_app()
        client = prod_app.test_client()
        login(client)
        cookie = client.get_cookie("session", path="/console")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.path, "/console")
        # explicit HTTP-only override must be supported for local deployment tests
        http_app = self._prod_app(config={"SESSION_COOKIE_SECURE": False})
        client2 = http_app.test_client()
        login(client2)
        self.assertFalse(client2.get_cookie("session", path="/console").secure)

    def test_production_dashboard_renders(self):
        prod_app = self._prod_app()
        client = prod_app.test_client()
        login(client)
        rv = client.get("/console/")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Dashboard", rv.get_data(as_text=True))

    def test_production_has_no_mutation_routes(self):
        prod_app = self._prod_app()
        for rule in prod_app.url_map.iter_rules():
            lower = rule.endpoint.lower()
            for word in self.FORBIDDEN:
                self.assertNotIn(word, lower)

    def test_production_engine_argv_and_shell_false(self):
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
            client = EngineClient(production=True)
            data = client.call("version")
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(data, {"version": "0.1.0"})
        self.assertEqual(
            calls["cmd"],
            ["/usr/bin/sudo", "-n", "/usr/local/lib/bluestream/web-ctl", "version"],
        )
        self.assertIs(calls["kwargs"]["shell"], False)

    def test_production_unsupported_ops_rejected_before_subprocess(self):
        called = []

        def fake_run(cmd, **kwargs):
            called.append(cmd)
            raise AssertionError("subprocess must not be executed")

        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(production=True)
            for op in ("relay_start", "relay_stop", "delete", "nginx reload"):
                with self.assertRaises(EngineError):
                    client.call(op)
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(called, [])

    @unittest.skipUnless(
        os.name == "posix", "fake-sudo subprocess execution needs POSIX"
    )
    def test_production_real_subprocess_via_fake_sudo(self):
        fake_webctl = write_fake_webctl(
            self._tmp.name,
            "web-ctl",
            "printf '%s\\n' '{\"ok\": true, \"data\": {\"version\": \"0.1.0\"}}'\n",
        )
        fake_sudo = Path(self._tmp.name) / "sudo"
        fake_sudo.write_text("#!/usr/bin/env bash\nexec \"$@\"\n", encoding="utf-8")
        fake_sudo.chmod(0o700)
        orig_sudo = engine_module.SUDO_PATH
        orig_wctl = engine_module.INSTALLED_WEB_CTL
        try:
            engine_module.SUDO_PATH = str(fake_sudo)
            engine_module.INSTALLED_WEB_CTL = str(fake_webctl)
            client = EngineClient(production=True, timeout=5)
            data = client.call("version")
        finally:
            engine_module.SUDO_PATH = orig_sudo
            engine_module.INSTALLED_WEB_CTL = orig_wctl
        self.assertEqual(data, {"version": "0.1.0"})

    def test_gunicorn_template_binds_loopback_only(self):
        text = (self.repo_root / "config" / "systemd" / "bluestream-web.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("127.0.0.1:8080", text)
        self.assertNotIn("0.0.0.0", text)
        self.assertIn("--workers 1", text)

    def test_gunicorn_template_runs_as_bluestream_web(self):
        text = (self.repo_root / "config" / "systemd" / "bluestream-web.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("User=bluestream-web", text)
        self.assertIn("Group=bluestream-web", text)

    def test_service_template_does_not_break_sudo(self):
        text = (self.repo_root / "config" / "systemd" / "bluestream-web.service").read_text(
            encoding="utf-8"
        )
        # Inspect active directives only (comments may legitimately discuss
        # the directives that are deliberately NOT set).
        active = "\n".join(
            ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        )
        # NoNewPrivileges=true would block sudo from gaining root.
        self.assertNotIn("NoNewPrivileges=true", active)
        # These can also break the sudo privilege transition; verify on Ubuntu
        # before ever adding them (GUI-1A.3B).
        self.assertNotIn("CapabilityBoundingSet=", active)
        self.assertNotIn("RestrictSUIDSGID=", active)
        # The compatible hardening directives remain present.
        for directive in (
            "ProtectSystem=strict",
            "ReadWritePaths=/run/sudo",
            "ProtectHome=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "RestrictRealtime=true",
            "LockPersonality=true",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=PYTHONNOUSERSITE=1",
        ):
            self.assertIn(directive, active)

    def test_sudoers_grants_only_web_ctl(self):
        text = (self.repo_root / "config" / "sudoers" / "bluestream-web").read_text(
            encoding="utf-8"
        )
        grant_lines = [
            ln.strip() for ln in text.splitlines() if "NOPASSWD:" in ln and ln.strip()
        ]
        self.assertTrue(grant_lines, "no NOPASSWD grant found")
        for line in grant_lines:
            self.assertIn("/usr/local/lib/bluestream/web-ctl", line)
            for dangerous in (
                "systemctl", "journalctl", "python", "bluestream-manager",
                "bash", "sh ", "vim", "nano", "/usr/bin/", "/bin/",
            ):
                self.assertNotIn(dangerous, line)

    def test_sudoers_enforces_env_reset_and_secure_path(self):
        text = (self.repo_root / "config" / "sudoers" / "bluestream-web").read_text(
            encoding="utf-8"
        )
        self.assertIn("env_reset", text)
        self.assertIn("secure_path", text)

    def test_sudoers_does_not_allow_setenv(self):
        text = (self.repo_root / "config" / "sudoers" / "bluestream-web").read_text(
            encoding="utf-8"
        )
        # Comments may mention SETENV; only active policy lines matter.
        active = "\n".join(
            ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        )
        self.assertNotIn("SETENV", active)


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
