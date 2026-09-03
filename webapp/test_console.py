"""Focused tests for the GUI-1A.2 local read-only Flask console.

Run from the repository root::

    python -m unittest webapp.test_console -v

Uses Flask's test client and temporary state only. Never touches /etc/bluestream,
real relays/playlists, or any production configuration.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webapp import app as app_module
from webapp import metrics as metrics_module
from webapp import security
from webapp.engine import (
    ALLOWED_MUTATION_OPERATIONS,
    ALLOWED_OPERATIONS,
    EngineClient,
    EngineError,
    valid_dest_display,
    valid_dest_platform,
    valid_dest_stream_key,
    valid_dest_url,
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

    def _mutation(self, operation, *values):
        self.mutation_calls.append((operation,) + tuple(values))
        value = self.mutation_payloads.get((operation,) + tuple(values))
        if value is None:
            value = self.mutation_payloads.get((operation,))
        if value is None:
            value = {"operation": operation, "values": values}
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

    def relay_create_url(self, name, url):
        return self._mutation("relay_create_url", name, url)

    def relay_set_source(self, name, url):
        return self._mutation("relay_set_source", name, url)

    def relay_create_media(self, name, media):
        return self._mutation("relay_create_media", name, media)

    def media_import_staged(self, staging):
        return self._mutation("media_import_staged", staging)

    def playlist_create(self, name, items):
        return self._mutation("playlist_create", name, *items)

    def playlist_cache_status(self):
        return self.call("playlist_cache_status")

    def playlist_cache_clear_unused(self):
        return self._mutation("playlist_cache_clear_unused")

    def destination_list(self):
        return self.call("destination_list")

    def destination_create(self, name, display, platform, url, key, enabled):
        return self._mutation(
            "destination_create", name, display, platform, url, key, enabled
        )

    def destination_enable(self, name):
        return self._mutation("destination_enable", name)

    def destination_disable(self, name):
        return self._mutation("destination_disable", name)

    def destination_delete(self, name):
        return self._mutation("destination_delete", name)

    # GUI-4 Phase 1B: destination attachment reads/writes.
    def relay_destinations_get(self, name):
        return self.call(("relay_destinations_get", name))

    def playlist_destinations_get(self, name):
        return self.call(("playlist_destinations_get", name))

    def relay_destinations_set(self, name, ids):
        return self._mutation("relay_destinations_set", name, tuple(ids))

    def playlist_destinations_set(self, name, ids):
        return self._mutation("playlist_destinations_set", name, tuple(ids))

    # GUI-5A: safe delete.
    def relay_delete(self, name):
        return self._mutation("relay_delete", name)

    def playlist_delete(self, name):
        return self._mutation("playlist_delete", name)

    def media_delete(self, name):
        return self._mutation("media_delete", name)

    # GUI-8A: one-time playlist start schedule.
    def playlist_schedule_get(self, name):
        return self.call(("playlist_schedule_get", name))

    def playlist_schedule_set(self, name, epoch, iso):
        return self._mutation("playlist_schedule_set", name, str(epoch), iso)

    def playlist_schedule_clear(self, name):
        return self._mutation("playlist_schedule_clear", name)


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
    "ytdlp_available": "yes",
    "relay_count": "1",
    "playlist_count": "0",
}

DEFAULT_PAYLOADS = {
    "snapshot": VALID_SNAPSHOT,
    "relay_list": [
        {
            "name": "news",
            "type": "remote-hls",
            "source": "https://source.example.com/live/index.m3u8",
            "active": "yes",
            "enabled": "no",
            "health": "HEALTHY",
        }
    ],
    "playlist_list": [],
    "media_list": [],
}


class _DiskUsage:
    """Minimal stand-in for the shutil.disk_usage named tuple (total/used/free)."""

    def __init__(self, total=0, used=0, free=0):
        self.total = total
        self.used = used
        self.free = free


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
        # GUI-1B.1/GUI-1C.1 add the lifecycle, create-stream and media endpoints.
        # GUI-4 Phase 1A/1B add the fixed destination enable/disable/delete and
        # attachment routes; GUI-5A adds the three fixed safe-delete routes;
        # GUI-6A adds the fixed edit-source route. Every one of those is a
        # FIXED, validated, single-purpose operation - there is still no generic
        # editor/remover. The exact-set assertion below is the real guard.
        forbidden = (
            "edit", "delete", "remove", "restore",
            "enable", "disable",
            "nginx", "ssl", "firewall",
        )
        allowed_action_endpoints = {
            "console.destinations_enable",
            "console.destinations_disable",
            "console.destinations_delete",
            # GUI-5A: fixed safe-delete of one stopped/unreferenced target
            "console.relay_delete_post",
            "console.playlist_delete_post",
            "console.media_delete_post",
            # GUI-6A: fixed edit of one stopped, URL-backed stream's source URL
            "console.relay_edit_source",
            "console.relay_edit_source_post",
        }
        endpoints = set()
        for rule in self.app.url_map.iter_rules():
            endpoints.add(rule.endpoint)
            if rule.endpoint in allowed_action_endpoints:
                continue
            lower = rule.endpoint.lower()
            for word in forbidden:
                self.assertNotIn(word, lower)
        # the complete console surface
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
                "console.streams",
                "console.streams_create",
                "console.streams_create_post",
                "console.media",
                "console.media_upload",
                "console.media_upload_post",
                "console.media_create_stream",
                "console.media_create_stream_post",
                "console.playlists",
                "console.playlists_create",
                "console.playlists_create_post",
                "console.playlists_cache_clear",
                "console.system_metrics",
                "console.destinations",
                "console.destinations_create",
                "console.destinations_create_post",
                "console.destinations_enable",
                "console.destinations_disable",
                "console.destinations_delete",
                # GUI-4 Phase 1B: destination attachment pages
                "console.relay_destinations",
                "console.relay_destinations_post",
                "console.playlist_destinations",
                "console.playlist_destinations_post",
                # GUI-5A: safe delete
                "console.relay_delete_post",
                "console.playlist_delete_post",
                "console.media_delete_post",
                # GUI-6A: edit stream source URL
                "console.relay_edit_source",
                "console.relay_edit_source_post",
                # GUI-8A: one-time playlist start schedule
                "console.playlist_schedule",
                "console.playlist_schedule_post",
                "console.playlist_schedule_cancel",
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

    def test_10_streams_page_shows_controls_with_csrf(self):
        login(self.client)
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("/console/relays/news/start", html)
        self.assertIn("/console/relays/news/stop", html)
        self.assertIn("/console/relays/news/restart", html)
        # every action form carries its own CSRF token (start/stop/restart +
        # delete + logout = at least 5)
        self.assertGreaterEqual(html.count('name="csrf_token"'), 5)
        # GUI-5A/6A: fixed, per-stream Delete and Edit Source controls exist
        # and post to their dedicated single-purpose routes.
        self.assertIn("/console/streams/news/delete", html)
        self.assertIn("/console/streams/news/edit-source", html)
        self.assertIn("btn-danger", html)


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
            frozenset(
                {
                    "version",
                    "snapshot",
                    "relay_list",
                    "playlist_list",
                    "media_list",
                    "playlist_cache_status",
                    "destination_list",
                }
            ),
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
    def test_20_mutation_methods_are_exactly_fixed(self):
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
            "relay_create_url",
            "relay_create_media",
            "media_import_staged",
            # GUI-1D.1: fixed-argv playlist creation
            "playlist_create",
            # GUI-4 Phase 1A: destination management foundation
            "destination_list",
            "destination_create",
            "destination_enable",
            "destination_disable",
            "destination_delete",
            # GUI-4 Phase 1B: destination attachments (IDs only)
            "relay_destinations_get",
            "relay_destinations_set",
            "playlist_destinations_get",
            "playlist_destinations_set",
            # GUI-5A: fixed safe-delete methods (one validated name each)
            "relay_delete",
            "playlist_delete",
            "media_delete",
            # GUI-6A: edit source URL of a stopped, URL-backed stream
            "relay_set_source",
            # GUI-8A: one-time playlist start schedule
            "playlist_schedule_get",
            "playlist_schedule_set",
            "playlist_schedule_clear",
        ):
            self.assertTrue(callable(getattr(client, name, None)), name)
        for forbidden in (
            # No generic edit / arbitrary-path / exec operations may exist.
            "relay_edit", "playlist_edit",
            "media_remove", "media_delete_path", "media_remove_path",
            "relay_delete_path", "playlist_delete_path",
            "config_edit", "config_write", "exec", "run", "shell",
            # GUI-4: no generic destination operations may exist
            "destination_edit", "destination_exec", "destination_execute",
            "destination_write", "destination_write_file",
            "destination_delete_path", "destination_remove_path",
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
                    "relay_create_url",
                    "relay_create_media",
                    "media_import_staged",
                    "playlist_create",
                    "playlist_cache_clear_unused",
                    "destination_create",
                    "destination_enable",
                    "destination_disable",
                    "destination_delete",
                    # GUI-4 Phase 1B: destination attachment setters (IDs only;
                    # no systemd unit touched, no outgoing RTMP push started).
                    "relay_destinations_set",
                    "playlist_destinations_set",
                    # GUI-5A: safe delete (one stopped/unreferenced target each).
                    "relay_delete",
                    "playlist_delete",
                    "media_delete",
                    # GUI-6A: edit the source URL of a stopped, URL-backed
                    # stream (URL treated strictly as data; never starts it).
                    "relay_set_source",
                    # GUI-8A: one-time playlist start schedule (set/replace +
                    # cancel). A root-generated timer targets a FIXED oneshot
                    # service - never a browser-supplied command.
                    "playlist_schedule_set",
                    "playlist_schedule_clear",
                }
            ),
        )
        # the read-only call() surface still rejects lifecycle operations
        for op in ("relay_start", "playlist_stop", "relay_create_url", "delete"):
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

    # ------------------------------------------------------------------
    # GUI-1D.1: playlist_create fixed argv + validation.
    # ------------------------------------------------------------------
    def test_28_playlist_create_uses_shell_false_argv_preserving_order(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = (
                    b'{"ok": true, "data": {"operation": "playlist_create",'
                    b' "name": "evening-promo-loop", "items": "3"}}'
                )
                stderr = b""

            return Result()

        fake = Path(self._tmp.name) / "web-ctl"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(web_ctl=fake, bash="/bin/bash")
            data = client.playlist_create(
                "evening-promo-loop", ["intro.mp4", "advert-01.mp4", "closing.mp4"]
            )
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(data["operation"], "playlist_create")
        # each media basename is its own argv element (no shell string), order kept
        self.assertEqual(
            calls["cmd"],
            [
                "/bin/bash",
                str(fake),
                "playlist_create",
                "evening-promo-loop",
                "intro.mp4",
                "advert-01.mp4",
                "closing.mp4",
            ],
        )
        self.assertIs(calls["kwargs"]["shell"], False)

    def test_29_playlist_create_invalid_inputs_rejected_before_subprocess(self):
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
            with self.assertRaises(EngineError):
                client.playlist_create("--help", ["a.mp4", "b.mp4"])
            with self.assertRaises(EngineError):
                client.playlist_create("ok", ["../evil.mp4", "b.mp4"])
            with self.assertRaises(EngineError):
                client.playlist_create("ok", ["a.mp4", "a.mp4"])
            with self.assertRaises(EngineError):
                client.playlist_create("ok", ["a.mp4"])
            with self.assertRaises(EngineError):
                client.playlist_create("ok", ["C:\\x.mp4", "b.mp4"])
            with self.assertRaises(EngineError):
                client.playlist_create("ok", ["x.mp4"] * 65)
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(called, [])

    def test_30_production_playlist_create_argv_fixed_sudo_webctl(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs

            class Result:
                returncode = 0
                stdout = b'{"ok": true, "data": {"operation": "playlist_create"}}'
                stderr = b""

            return Result()

        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(production=True)
            client.playlist_create("evening-promo-loop", ["intro.mp4", "closing.mp4"])
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(
            calls["cmd"],
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/local/lib/bluestream/web-ctl",
                "playlist_create",
                "evening-promo-loop",
                "intro.mp4",
                "closing.mp4",
            ],
        )
        self.assertIs(calls["kwargs"]["shell"], False)


    # ------------------------------------------------------------------
    # GUI-4 Phase 1A security follow-up: the destination stream key must
    # NEVER appear in process argv - it travels through stdin only.
    # ------------------------------------------------------------------
    def test_31_destination_create_local_argv_excludes_key_and_uses_stdin(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["stdin"] = kwargs.get("input")
            calls["shell"] = kwargs.get("shell")

            class Result:
                returncode = 0
                stdout = b'{"ok": true, "data": {"operation": "destination_create"}}\n'
                stderr = b""

            return Result()

        fake = Path(self._tmp.name) / "web-ctl"
        fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        key = "secret key with spaces-123"
        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(web_ctl=fake, bash="/bin/bash")
            client.destination_create(
                "main-youtube", "Main YouTube", "youtube",
                "rtmps://a.rtmp.youtube.com/live2", key, "yes",
            )
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(
            calls["cmd"],
            [
                "/bin/bash", str(fake), "destination_create",
                "main-youtube", "Main YouTube", "youtube",
                "rtmps://a.rtmp.youtube.com/live2", "yes",
            ],
        )
        self.assertEqual(calls["stdin"], key.encode("utf-8"))
        self.assertIs(calls["shell"], False)
        for token in calls["cmd"]:
            self.assertNotIn(key, token)
        self.assertNotIn(key, " ".join(calls["cmd"]))

    def test_32_destination_create_production_argv_excludes_key_and_uses_stdin(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["stdin"] = kwargs.get("input")
            calls["shell"] = kwargs.get("shell")

            class Result:
                returncode = 0
                stdout = b'{"ok": true, "data": {}}\n'
                stderr = b""

            return Result()

        key = "SUPER-TOP-SECRET-key"
        original = engine_module.subprocess.run
        engine_module.subprocess.run = fake_run
        try:
            client = EngineClient(production=True)
            client.destination_create(
                "twitch-main", "Main Twitch", "twitch", "rtmp://live.twitch.tv/app",
                key, "no",
            )
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(
            calls["cmd"],
            [
                "/usr/bin/sudo", "-n", "/usr/local/lib/bluestream/web-ctl",
                "destination_create", "twitch-main", "Main Twitch", "twitch",
                "rtmp://live.twitch.tv/app", "no",
            ],
        )
        self.assertEqual(calls["stdin"], key.encode("utf-8"))
        self.assertIs(calls["shell"], False)
        self.assertNotIn(key, " ".join(calls["cmd"]))

    def test_33_destination_create_invalid_key_rejected_before_subprocess(self):
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
            for bad_key in ("", "a\nb", "k" * 257, "ctl\x1b"):
                with self.assertRaises(EngineError, msg=repr(bad_key)):
                    client.destination_create(
                        "main-youtube", "Main YouTube", "youtube",
                        "rtmps://x", bad_key, "yes",
                    )
        finally:
            engine_module.subprocess.run = original
        self.assertEqual(called, [])


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

    def _run_stdin(self, data, *args):
        """Run web-ctl with ``data`` written to its stdin (utf-8)."""
        bash = engine_module.default_bash_path()
        proc = subprocess.run(
            [bash, str(REPO_ROOT / "web-ctl"), *args],
            input=data.encode("utf-8"),
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

    # ------------------------------------------------------------------
    # GUI-1C.1: exact argv + fail-closed validation for create/import ops.
    # ------------------------------------------------------------------
    def test_20_relay_create_url_missing_argument_fails(self):
        rc, doc = self._run("relay_create_url", "news24")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_ARGUMENT")

    def test_21_relay_create_url_extra_argument_fails(self):
        rc, doc = self._run("relay_create_url", "news24", "https://x/y.m3u8", "extra")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")

    def test_22_relay_create_url_invalid_name_fails(self):
        rc, doc = self._run("relay_create_url", "--help", "https://x/y.m3u8")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_NAME")

    def test_23_relay_create_url_unsupported_scheme_fails(self):
        rc, doc = self._run("relay_create_url", "news24", "file:///etc/passwd")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_URL")

    def test_24_relay_create_media_invalid_media_name_fails(self):
        rc, doc = self._run("relay_create_media", "news24", "../evil.mp4")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_MEDIA")

    def test_25_media_import_staged_missing_fails(self):
        rc, doc = self._run("media_import_staged")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_TARGET")

    def test_26_media_import_staged_extra_fails(self):
        rc, doc = self._run("media_import_staged", "a.mp4", "extra")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")

    def test_27_media_import_staged_invalid_staging_fails(self):
        rc, doc = self._run("media_import_staged", "../evil.mp4")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_STAGING")

    def test_28_media_list_rejects_extra_args(self):
        rc, doc = self._run("media_list", "extra")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")

    def test_29_media_list_read_only_succeeds(self):
        rc, doc = self._run("media_list")
        self.assertEqual(rc, 0)
        self.assertIs(doc["ok"], True)
        self.assertIsInstance(doc.get("data"), list)

    # ------------------------------------------------------------------
    # GUI-1D.1: playlist_create fixed argv + fail-closed validation.  Every
    # case below stops at dispatch/name/media validation - nothing is ever
    # written in a dev checkout.
    # ------------------------------------------------------------------
    def test_30_playlist_create_missing_arguments_fail(self):
        rc, doc = self._run("playlist_create")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_ARGUMENT")
        rc, doc = self._run("playlist_create", "mypl")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_ARGUMENT")

    def test_31_playlist_create_too_few_items_fails(self):
        rc, doc = self._run("playlist_create", "mypl", "a.mp4")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_FEW_ITEMS")

    def test_32_playlist_create_too_many_args_rejected(self):
        items = ["item%02d.mp4" % i for i in range(65)]
        rc, doc = self._run("playlist_create", "mypl", *items)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")

    def test_33_playlist_create_unsafe_name_rejected(self):
        for bad in ("--help", "../x", "UPPER", "x;rm", "a b"):
            rc, doc = self._run("playlist_create", bad, "a.mp4", "b.mp4")
            self.assertEqual(rc, 1, bad)
            self.assertEqual(doc["code"], "INVALID_NAME", bad)

    def test_34_playlist_create_unsafe_media_rejected(self):
        for item in ("../evil.mp4", "/etc/passwd", "C:\\x.mp4", "a;b.mp4", "a b.mp4"):
            rc, doc = self._run("playlist_create", "mypl", item, "b.mp4")
            self.assertEqual(rc, 1, item)
            self.assertEqual(doc["code"], "INVALID_MEDIA", item)

    def test_35_playlist_create_duplicate_media_rejected(self):
        rc, doc = self._run("playlist_create", "mypl", "a.mp4", "a.mp4")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "DUPLICATE_ITEM")

    def test_36_playlist_create_valid_names_reach_media_check(self):
        # Valid name + valid media basenames progress past dispatch/validation
        # to the engine's authoritative media existence check (fails closed in
        # a dev checkout because the managed media directory does not exist).
        rc, doc = self._run("playlist_create", "zz-playlist-probe", "a.mp4", "b.mp4")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MEDIA_NOT_FOUND")

    # ------------------------------------------------------------------
    # GUI-4 Phase 1A: destination operation argv cardinality + allowlist.
    # ------------------------------------------------------------------
    def test_37_destination_state_argv_cardinality(self):
        for op in ("destination_enable", "destination_disable", "destination_delete"):
            rc, doc = self._run(op)
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "MISSING_TARGET", op)
        for op in ("destination_enable", "destination_delete"):
            rc, doc = self._run(op, "goodname", "extra")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS", op)

    def test_38_destination_state_valid_name_reaches_existence(self):
        # The exact-2 form progresses past dispatch + name validation to the
        # existence check, which fails closed in a dev checkout.
        for op in ("destination_enable", "destination_disable", "destination_delete"):
            rc, doc = self._run(op, "zz-destination-probe")
            self.assertEqual(rc, 1, op)
            self.assertEqual(doc["code"], "NOT_FOUND", op)
            self.assertNotIn(
                doc["code"],
                ("MISSING_TARGET", "TOO_MANY_ARGUMENTS", "INVALID_NAME",
                 "UNKNOWN_OPERATION"),
                op,
            )

    def test_39_destination_create_argc_and_platform_allowlist(self):
        # Too few fixed values -> MISSING_ARGUMENT; extra -> TOO_MANY_ARGUMENTS.
        # The argv form carries only NON-SECRET values; a secret-looking extra
        # argv element is rejected, never read as the key.
        rc, doc = self._run("destination_create", "only-name")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_ARGUMENT")
        rc, doc = self._run(
            "destination_create", "a", "A", "youtube", "rtmps://x",
            "yes", "KEY-IN-ARGV",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")
        # Unknown platform rejected before any write, never executed.
        rc, doc = self._run(
            "destination_create", "a", "A", "not-a-platform", "rtmps://x", "no"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "UNKNOWN_PLATFORM")

    def test_40_destination_list_readonly_and_no_extra_args(self):
        rc, doc = self._run("destination_list")
        self.assertEqual(rc, 0, doc)
        self.assertIs(doc["ok"], True)
        self.assertEqual(doc["data"], [])  # empty in a dev checkout
        rc, doc = self._run("destination_list", "extra")
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "TOO_MANY_ARGUMENTS")

    def test_41_destination_create_reads_secret_from_stdin(self):
        # The non-secret argv is accepted; the secret must come from stdin.
        args = ("destination_create", "a", "A", "youtube", "rtmps://x", "no")
        # Missing stdin (empty) -> MISSING_STREAM_KEY.
        rc, doc = self._run_stdin("", *args)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_STREAM_KEY")
        # Blank line -> MISSING_STREAM_KEY.
        rc, doc = self._run_stdin("\n", *args)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "MISSING_STREAM_KEY")

    def test_42_destination_create_stdin_secret_validation(self):
        args = ("destination_create", "a", "A", "youtube", "rtmps://x", "no")
        # Control character (CR) is invalid.
        rc, doc = self._run_stdin("ab\r", *args)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_STREAM_KEY")
        # Oversized key is invalid.
        rc, doc = self._run_stdin("k" * 257 + "\n", *args)
        self.assertEqual(rc, 1)
        self.assertEqual(doc["code"], "INVALID_STREAM_KEY")
        # A valid key with spaces is ACCEPTED (passes validation; in a dev
        # checkout it then fails closed at the root-required engine write).
        rc, doc = self._run_stdin("my secret  key-123\n", *args)
        self.assertEqual(rc, 1)
        self.assertNotIn(
            doc["code"],
            ("MISSING_STREAM_KEY", "INVALID_STREAM_KEY", "TOO_MANY_ARGUMENTS"),
            doc,
        )


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
        # GUI-1B.1/GUI-1C.1 allow the lifecycle + create-stream + upload
        # operations; every other mutation word stays banned.
        "enable", "disable",
        "edit", "delete", "remove", "restore",
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

    def test_production_max_upload_reads_web_conf(self):
        # GUI-2C: production honors the authoritative MAX_MEDIA_UPLOAD_MB value
        # rendered into web.conf, keeping Flask and nginx in sync.
        state = Path(self._tmp.name) / "webconf-upload"
        security.init_admin(state, "pw")
        (state / "web.conf").write_text(
            "secure_cookie=no\nmax_media_upload_mb=5120\n", encoding="utf-8"
        )
        prod_app = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertEqual(prod_app.config["MAX_UPLOAD_SIZE"], 5120 * 1024 * 1024)
        self.assertEqual(
            prod_app.config["MAX_CONTENT_LENGTH"], 5120 * 1024 * 1024
        )
        # invalid value falls back to the 10 GiB default (documented safe rule)
        (state / "web.conf").write_text(
            "secure_cookie=no\nmax_media_upload_mb=not-a-number\n", encoding="utf-8"
        )
        prod_app2 = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertEqual(
            prod_app2.config["MAX_UPLOAD_SIZE"], app_module.DEFAULT_MAX_UPLOAD_SIZE
        )
        # existing installation without the setting gets the default
        (state / "web.conf").write_text("secure_cookie=no\n", encoding="utf-8")
        prod_app3 = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertEqual(
            prod_app3.config["MAX_UPLOAD_SIZE"], app_module.DEFAULT_MAX_UPLOAD_SIZE
        )
        # oversized configured value (> 512 GiB ceiling) falls back to default
        (state / "web.conf").write_text(
            "secure_cookie=no\nmax_media_upload_mb=999999999999\n", encoding="utf-8"
        )
        prod_app4 = app_module.create_app(
            state_dir=state, production=True, engine=FakeEngine(DEFAULT_PAYLOADS)
        )
        self.assertEqual(
            prod_app4.config["MAX_UPLOAD_SIZE"], app_module.DEFAULT_MAX_UPLOAD_SIZE
        )

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
        # GUI-4 Phase 1A: exactly these fixed destination action endpoints are
        # the only allowlisted mutation-word routes (enable/disable/delete of a
        # strictly-validated destination). Every other mutation word stays
        # forbidden.
        allowed_action_endpoints = {
            "console.destinations_enable",
            "console.destinations_disable",
            "console.destinations_delete",
            # GUI-5A safe delete + GUI-6A edit-source: fixed single-purpose
            # routes, each validated end to end. Still no generic editor.
            "console.relay_delete_post",
            "console.playlist_delete_post",
            "console.media_delete_post",
            "console.relay_edit_source",
            "console.relay_edit_source_post",
        }
        for rule in prod_app.url_map.iter_rules():
            if rule.endpoint in allowed_action_endpoints:
                continue
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


class Gui1cWorkflowTests(unittest.TestCase):
    """GUI-1C.1 routes: streams create, media library, upload, create-stream.

    Uses a FakeEngine so no privileged bridge call is ever made; auth, CSRF,
    GET-vs-POST and PRG behaviour are exercised end to end.
    """

    RELAYS_PAYLOADS = {
        "snapshot": VALID_SNAPSHOT,
        "relay_list": [
            {
                "name": "news",
                "type": "remote-hls",
                "source": "https://source.example.com/live/index.m3u8",
                "active": "no",
                "enabled": "yes",
                "health": "STOPPED",
            }
        ],
        "playlist_list": [],
        "media_list": [{"name": "promo.mp4", "size": "12345", "extension": "mp4"}],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.RELAYS_PAYLOADS)
        self.upload_dir = Path(self._tmp.name) / "upload"
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
            config={"WEB_UPLOAD_DIR": str(self.upload_dir)},
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def _login(self):
        login(self.client)

    def _csrf(self):
        return get_csrf(self.client, path="/console/streams")

    def test_01_streams_page_lists_relays_professionally(self):
        self._login()
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("news", html)
        self.assertIn("Remote HLS", html)
        self.assertIn("https://source.example.com/live/index.m3u8", html)
        self.assertIn("STOPPED", html)
        self.assertIn("/console/relays/news/start", html)
        self.assertIn("/console/relays/news/stop", html)
        self.assertIn("/console/relays/news/restart", html)

    def test_02_create_stream_get_form_has_csrf(self):
        self._login()
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn('name="csrf_token"', html)
        self.assertIn("Source URL", html)

    def test_03_unauthenticated_create_stream_cannot_mutate(self):
        token = get_csrf(self.client)
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "news24", "url": "https://x/live/index.m3u8", "csrf_token": token},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [])

    def test_04_create_stream_requires_csrf(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "news24", "url": "https://x/live/index.m3u8"},
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_05_create_stream_invalid_name_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "!!!", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn("safe stream name", html)

    def test_06_create_stream_traversal_name_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "../evil", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_07_create_stream_unsupported_scheme_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "news24", "url": "file:///etc/passwd", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn("Unsupported or malformed source URL", html)

    def test_08_create_stream_success_prg(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "news24", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_create_url", "news24", "https://x/live/index.m3u8")],
        )
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("Stream &#39;news24&#39; created", html)

    def test_09_create_stream_duplicate_controlled_error(self):
        self._login()
        self.engine.mutation_payloads[("relay_create_url", "news24", "https://x/live/index.m3u8")] = EngineError(
            "stream already exists", code="ALREADY_EXISTS"
        )
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "news24", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn("already exists", html)
        self.assertNotIn("Traceback", html)

    def test_10_media_page_lists_items(self):
        self._login()
        html = self.client.get("/console/media").get_data(as_text=True)
        self.assertIn("promo.mp4", html)
        self.assertIn("mp4", html)
        self.assertIn("Upload Media", html)

    def test_11_upload_get_form_has_csrf_and_size_hint(self):
        self._login()
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn('name="csrf_token"', html)
        self.assertIn("Upload limit", html)

    def test_12_upload_path_traversal_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"data"), "../../promo.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        # traversal anywhere in the submitted name is rejected: no engine call,
        # nothing written inside OR outside the upload dir.
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertFalse(list(self.upload_dir.iterdir()) if self.upload_dir.exists() else [])
        self.assertFalse((self.upload_dir.parent / "promo.mp4").exists())

    def test_12b_upload_dotdot_basename_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"data"), "a..b.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertFalse(list(self.upload_dir.iterdir()) if self.upload_dir.exists() else [])

    def test_13_upload_unsupported_extension_rejected(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"data"), "evil.exe"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_14_upload_oversized_rejected(self):
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
            config={"WEB_UPLOAD_DIR": str(self.upload_dir), "MAX_UPLOAD_SIZE": 10},
        )
        client = app.test_client()
        login(client)
        token = get_csrf(client, path="/console/media/upload")
        rv = client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"x" * 100), "big.mp4"), "csrf_token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("upload size limit", html)

    def test_15_upload_success_stages_and_imports(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"video-data"), "promo.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/media", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [("media_import_staged", "promo.mp4")])
        self.assertTrue((self.upload_dir / "promo.mp4").exists())

    def test_15b_upload_not_regular_controlled_error(self):
        self._login()
        self.engine.mutation_payloads[("media_import_staged", "promo.mp4")] = EngineError(
            "staged upload is not a regular file", code="NOT_REGULAR"
        )
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"video-data"), "promo.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("not a regular file", html)
        self.assertNotIn("Traceback", html)

    def test_16_media_create_stream_get_form(self):
        self._login()
        html = self.client.get("/console/media/promo.mp4/create-stream").get_data(as_text=True)
        self.assertIn("promo.mp4", html)
        self.assertIn('name="csrf_token"', html)

    def test_17_media_create_stream_success_prg(self):
        self._login()
        rv = self.client.post(
            "/console/media/promo.mp4/create-stream",
            data={"name": "promo-loop", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_create_media", "promo-loop", "promo.mp4")],
        )

    def test_18_media_create_stream_nonexistent_media(self):
        self._login()
        rv = self.client.get("/console/media/missing.mp4/create-stream")
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/media", rv.headers["Location"])

    def test_19_media_create_stream_invalid_name(self):
        self._login()
        rv = self.client.post(
            "/console/media/promo.mp4/create-stream",
            data={"name": "!!!", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_20_playlists_page_renders(self):
        self._login()
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Playlists", html)

    def test_21_navigation_present_on_pages(self):
        self._login()
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("/console/media", html)
        self.assertIn("/console/playlists", html)
        self.assertIn("/console/", html)

    def test_22_create_stream_friendly_name_normalized(self):
        self._login()
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "My Promo Stream", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        # only the normalized safe internal ID crosses the privileged boundary
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_create_url", "my-promo-stream", "https://x/live/index.m3u8")],
        )
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("my-promo-stream", html)

    def test_23_create_stream_normalized_duplicate_message(self):
        self._login()
        self.engine.mutation_payloads[("relay_create_url", "my-promo-stream", "https://x/live/index.m3u8")] = EngineError(
            "stream already exists", code="ALREADY_EXISTS"
        )
        rv = self.client.post(
            "/console/streams/create",
            data={"name": "My Promo Stream", "url": "https://x/live/index.m3u8", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn("A stream named &#39;my-promo-stream&#39; already exists.", html)
        self.assertNotIn("Traceback", html)

    def test_24_upload_friendly_name_normalized(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"video-data"), "5 Minute Timer.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/media", rv.headers["Location"])
        # the engine receives only the normalized safe stored filename
        self.assertEqual(self.engine.mutation_calls, [("media_import_staged", "5-minute-timer.mp4")])
        self.assertTrue((self.upload_dir / "5-minute-timer.mp4").exists())
        self.assertFalse((self.upload_dir / "5 Minute Timer.mp4").exists())

    def test_25_upload_normalized_duplicate_message(self):
        self._login()
        self.engine.mutation_payloads[("media_import_staged", "my-video.mp4")] = EngineError(
            "a file with this name already exists in managed media", code="MEDIA_EXISTS"
        )
        rv = self.client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"video-data"), "My Video.mp4"), "csrf_token": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("A media file named &#39;my-video.mp4&#39; already exists.", html)
        self.assertNotIn("Traceback", html)

    def test_26_media_create_stream_friendly_name_normalized(self):
        self._login()
        rv = self.client.post(
            "/console/media/promo.mp4/create-stream",
            data={"name": "My Timer Stream", "csrf_token": self._csrf()},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_create_media", "my-timer-stream", "promo.mp4")],
        )

    # ------------------------------------------------------------------
    # GUI-2A: upload progress UI + XHR outcome format (same pipeline).
    # ------------------------------------------------------------------
    def test_27_upload_form_has_progress_ui(self):
        self._login()
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn('id="upload-progress"', html)
        self.assertIn('role="progressbar"', html)
        self.assertIn("aria-valuenow", html)
        self.assertIn("processing media", html)

    def test_28_upload_progress_uses_real_events_not_fake_timer(self):
        template = (
            Path(__file__).resolve().parent / "templates" / "media_upload.html"
        ).read_text(encoding="utf-8")
        self.assertIn("xhr.upload.onprogress", template)
        # the percentage must come from the browser upload event, never a timer
        self.assertNotIn("setInterval", template)
        self.assertNotIn("Math.random", template)

    def test_29_upload_progress_prevents_duplicate_submission(self):
        template = (
            Path(__file__).resolve().parent / "templates" / "media_upload.html"
        ).read_text(encoding="utf-8")
        self.assertIn("submitButton.disabled = true", template)
        self.assertIn("fileInput.disabled = true", template)
        self.assertIn("event.preventDefault", template)

    def test_30_upload_ajax_success_returns_json(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={
                "media": (io.BytesIO(b"video-data"), "promo.mp4"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 200)
        payload = json.loads(rv.get_data(as_text=True))
        self.assertEqual(
            payload,
            {"ok": True, "message": "Media 'promo.mp4' added to the library."},
        )
        # the same trusted pipeline ran unchanged
        self.assertEqual(
            self.engine.mutation_calls, [("media_import_staged", "promo.mp4")]
        )
        self.assertTrue((self.upload_dir / "promo.mp4").exists())

    def test_31_upload_ajax_error_returns_safe_message(self):
        self._login()
        self.engine.mutation_payloads[("media_import_staged", "promo.mp4")] = EngineError(
            "uploaded file is not recognized media", code="NOT_MEDIA"
        )
        rv = self.client.post(
            "/console/media/upload",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={
                "media": (io.BytesIO(b"garbage"), "promo.mp4"),
                "csrf_token": self._csrf(),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 200)
        payload = json.loads(rv.get_data(as_text=True))
        self.assertFalse(payload["ok"])
        self.assertIn("not recognized media", payload["message"])
        self.assertNotIn("Traceback", rv.get_data(as_text=True))

    def test_32_upload_ajax_oversized_rejected_with_safe_message(self):
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
            config={"WEB_UPLOAD_DIR": str(self.upload_dir), "MAX_UPLOAD_SIZE": 10},
        )
        client = app.test_client()
        login(client)
        token = get_csrf(client, path="/console/media/upload")
        rv = client.post(
            "/console/media/upload",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"media": (io.BytesIO(b"x" * 100), "big.mp4"), "csrf_token": token},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 200)
        payload = json.loads(rv.get_data(as_text=True))
        self.assertFalse(payload["ok"])
        self.assertIn("upload size limit", payload["message"])
        self.assertEqual(self.engine.mutation_calls, [])

    def test_33_upload_ajax_still_requires_csrf(self):
        self._login()
        rv = self.client.post(
            "/console/media/upload",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"media": (io.BytesIO(b"video-data"), "promo.mp4")},
            content_type="multipart/form-data",
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    # ------------------------------------------------------------------
    # GUI-2C: configurable large uploads + disk safety (same pipeline).
    # ------------------------------------------------------------------
    def test_34_default_upload_max_is_10_gib(self):
        self.assertEqual(
            app_module.DEFAULT_MAX_UPLOAD_SIZE, 10 * 1024 * 1024 * 1024
        )
        # the upload page derives the displayed limit from the SAME config used
        # for enforcement - no hard-coded presentation constant.
        self._login()
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("10.0 GiB", html)

    def test_35_upload_above_max_rejected_when_content_length_missing(self):
        # A client that omits/misreports Content-Length (chunked/lying) must not
        # bypass the cap: the ACTUAL staged size is enforced after save.
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
            config={"WEB_UPLOAD_DIR": str(self.upload_dir), "MAX_UPLOAD_SIZE": 10},
        )
        client = app.test_client()
        login(client)
        token = get_csrf(client, path="/console/media/upload")
        rv = client.post(
            "/console/media/upload",
            data={"media": (io.BytesIO(b"x" * 100), "big.mp4"), "csrf_token": token},
            content_type="multipart/form-data",
            content_length=0,  # misreported Content-Length
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        # no oversized staging artifact is left behind
        self.assertFalse(list(self.upload_dir.iterdir()) if self.upload_dir.exists() else [])
        html = client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("upload size limit", html)

    def test_36_upload_rejected_when_free_space_insufficient(self):
        self._login()
        with mock.patch("webapp.app.shutil.disk_usage") as du:
            du.return_value = _DiskUsage(free=1 * 1024 * 1024)  # below staged+reserve
            rv = self.client.post(
                "/console/media/upload",
                data={
                    "media": (io.BytesIO(b"video-data"), "promo.mp4"),
                    "csrf_token": self._csrf(),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(rv.status_code, 302)
        # rejected BEFORE the privileged import; the staged file is cleaned up
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertFalse(list(self.upload_dir.iterdir()) if self.upload_dir.exists() else [])
        html = self.client.get("/console/media/upload").get_data(as_text=True)
        self.assertIn("Not enough free storage for this upload.", html)
        # no internal filesystem path is exposed
        self.assertNotIn(str(self.upload_dir), html)

    def test_37_upload_proceeds_when_free_space_sufficient(self):
        self._login()
        with mock.patch("webapp.app.shutil.disk_usage") as du:
            du.return_value = _DiskUsage(free=100 * 1024 * 1024 * 1024)
            rv = self.client.post(
                "/console/media/upload",
                data={
                    "media": (io.BytesIO(b"video-data"), "promo.mp4"),
                    "csrf_token": self._csrf(),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(
            self.engine.mutation_calls, [("media_import_staged", "promo.mp4")]
        )

    def test_38_upload_disk_error_returns_safe_json_message(self):
        # The Phase 1 XHR progress UI keeps working: disk rejections come back
        # as the same safe application message with no privileged detail.
        self._login()
        with mock.patch("webapp.app.shutil.disk_usage") as du:
            du.return_value = _DiskUsage(free=1 * 1024 * 1024)
            rv = self.client.post(
                "/console/media/upload",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "media": (io.BytesIO(b"video-data"), "promo.mp4"),
                    "csrf_token": self._csrf(),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(rv.status_code, 200)
        payload = json.loads(rv.get_data(as_text=True))
        self.assertFalse(payload["ok"])
        self.assertIn("Not enough free storage", payload["message"])
        self.assertNotIn(str(self.upload_dir), rv.get_data(as_text=True))
        self.assertEqual(self.engine.mutation_calls, [])

    def test_39_max_upload_mb_parser_unit(self):
        # Strict digits-only parsing; invalid/oversized values return None so
        # the documented safe default is applied (mirrors bs_valid_upload_mb).
        parse = app_module._max_upload_bytes_from_mb
        self.assertEqual(parse("10240"), 10 * 1024 * 1024 * 1024)
        self.assertEqual(parse("1"), 1 * 1024 * 1024)
        self.assertEqual(
            parse(str(app_module.MAX_UPLOAD_MB_CEILING)),
            app_module.MAX_UPLOAD_MB_CEILING * 1024 * 1024,
        )
        for bad in ("0", "-1", "abc", "10m", "10;", "1 0", "", "   ", "999999999999"):
            self.assertIsNone(parse(bad), bad)
        self.assertIsNone(
            parse(str(app_module.MAX_UPLOAD_MB_CEILING + 1))
        )


class Gui2NavigationTests(unittest.TestCase):
    """GUI-2B: active top-level navigation highlight (route-aware, child pages
    keep their parent section highlighted via the active_section context)."""

    ACTIVE_NAV_RE = re.compile(r'<a href="([^"]+)"[^>]*aria-current="page"[^>]*>')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=FakeEngine(DEFAULT_PAYLOADS),
        )
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self._tmp.cleanup()

    def _active_hrefs(self, path):
        html = self.client.get(path).get_data(as_text=True)
        return self.ACTIVE_NAV_RE.findall(html)

    def test_01_dashboard_is_active_on_dashboard(self):
        self.assertEqual(self._active_hrefs("/console/"), ["/console/"])

    def test_02_streams_is_active_on_stream_pages(self):
        self.assertEqual(self._active_hrefs("/console/streams"), ["/console/streams"])
        self.assertEqual(
            self._active_hrefs("/console/streams/create"), ["/console/streams"]
        )

    def test_03_media_is_active_on_media_pages(self):
        self.assertEqual(self._active_hrefs("/console/media"), ["/console/media"])
        self.assertEqual(
            self._active_hrefs("/console/media/upload"), ["/console/media"]
        )

    def test_04_playlists_is_active_on_playlist_pages(self):
        self.assertEqual(
            self._active_hrefs("/console/playlists"), ["/console/playlists"]
        )
        self.assertEqual(
            self._active_hrefs("/console/playlists/create"), ["/console/playlists"]
        )

    def test_05_exactly_one_nav_item_is_active(self):
        for path in (
            "/console/",
            "/console/streams",
            "/console/streams/create",
            "/console/media",
            "/console/media/upload",
            "/console/playlists",
            "/console/playlists/create",
        ):
            self.assertEqual(len(self._active_hrefs(path)), 1, path)


class Gui3PlaylistCacheTests(unittest.TestCase):
    """GUI-3A: playlist cache visibility + manual clear (auth/CSRF/PRG).

    Uses a FakeEngine so no privileged bridge call is ever made. The page-level
    cache card, POST-only clear route, auth/CSRF, safe success/nothing/failure
    messages, and status-failure resilience are exercised end to end.
    """

    CACHE_STATUS = {
        "total_bytes": "1468006400",  # 1.4 GiB
        "artifact_count": "6",
        "protected_bytes": "650117120",  # 620 MiB
        "protected_count": "3",
        "reclaimable_bytes": "817889280",  # 780 MiB
        "reclaimable_count": "3",
    }

    EMPTY_CACHE_STATUS = {
        "total_bytes": "0",
        "artifact_count": "0",
        "protected_bytes": "0",
        "protected_count": "0",
        "reclaimable_bytes": "0",
        "reclaimable_count": "0",
    }

    def _make_app(self, cache_status=None, cache_status_payload=True):
        payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [],
            "playlist_list": [],
            "media_list": [],
        }
        if cache_status_payload:
            payloads["playlist_cache_status"] = cache_status or self.CACHE_STATUS
        self.engine = FakeEngine(payloads)
        return app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = self._make_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def _login(self):
        login(self.client)

    def _csrf(self, path="/console/playlists"):
        return get_csrf(self.client, path=path)

    def _clear_post(self, **kwargs):
        data = kwargs.pop("data", {})
        data.setdefault("csrf_token", self._csrf())
        return self.client.post(
            "/console/playlists/cache/clear", data=data, **kwargs
        )

    def test_01_playlists_page_renders_cache_card(self):
        self._login()
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Playlist Cache", html)
        self.assertIn("1.4 GiB", html)
        self.assertIn("Artifacts", html)
        self.assertIn("Clear Unused Cache", html)
        self.assertIn("/console/playlists/cache/clear", html)
        # internal filesystem paths are never exposed to the browser
        self.assertNotIn("playlist-cache", html)

    def test_02_empty_cache_renders_clean_zero_state_and_disabled_button(self):
        self.app = self._make_app(cache_status=self.EMPTY_CACHE_STATUS)
        self.client = self.app.test_client()
        self._login()
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("0 B", html)
        self.assertIn("disabled", html)

    def test_03_get_cannot_trigger_clear(self):
        self._login()
        rv = self.client.get("/console/playlists/cache/clear")
        self.assertEqual(rv.status_code, 405)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_04_clear_requires_authentication(self):
        rv = self.client.post(
            "/console/playlists/cache/clear",
            data={"csrf_token": get_csrf(self.client)},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [])

    def test_05_clear_requires_csrf(self):
        self._login()
        rv = self.client.post(
            "/console/playlists/cache/clear",
            data={},
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_06_clear_success_reports_safe_message(self):
        self._login()
        self.engine.mutation_payloads[("playlist_cache_clear_unused",)] = {
            "operation": "playlist_cache_clear_unused",
            "freed_bytes": "817889280",
            "freed_count": "3",
        }
        rv = self._clear_post()
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/playlists", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls, [("playlist_cache_clear_unused",)]
        )
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Cleared 780.0 MiB from the playlist cache (3 files).", html)

    def test_07_clear_nothing_to_clear_message(self):
        self._login()
        # no mutation payload -> FakeEngine returns freed count 0
        rv = self._clear_post()
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(
            self.engine.mutation_calls, [("playlist_cache_clear_unused",)]
        )
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("No unused playlist cache files to clear.", html)

    def test_08_clear_engine_failure_uses_safe_message(self):
        self._login()
        self.engine.mutation_payloads[("playlist_cache_clear_unused",)] = EngineError(
            "privileged detail", code="CACHE_CLEAR_FAILED"
        )
        rv = self._clear_post()
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Playlist cache could not be cleared safely.", html)
        self.assertNotIn("privileged detail", html)
        self.assertNotIn("Traceback", html)

    def test_09_status_failure_does_not_break_page(self):
        # FakeEngine without a playlist_cache_status payload raises EngineError
        self.app = self._make_app(cache_status_payload=False)
        self.client = self.app.test_client()
        self._login()
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertEqual(html.count("Playlist Cache"), 1)
        self.assertIn("temporarily unavailable", html)
        self.assertIn("Create Playlist", html)

    def test_10_status_values_are_normalized_to_ints(self):
        self.assertEqual(
            app_module._normalize_cache_status(self.CACHE_STATUS),
            {
                "total_bytes": 1468006400,
                "artifact_count": 6,
                "protected_bytes": 650117120,
                "protected_count": 3,
                "reclaimable_bytes": 817889280,
                "reclaimable_count": 3,
            },
        )
        self.assertEqual(app_module._normalize_cache_status(None), {})
        self.assertEqual(
            app_module._normalize_cache_status({"total_bytes": "junk"}),
            {
                "total_bytes": 0,
                "artifact_count": 0,
                "protected_bytes": 0,
                "protected_count": 0,
                "reclaimable_bytes": 0,
                "reclaimable_count": 0,
            },
        )

    def test_11_clear_blocked_by_transition_shows_accurate_message(self):
        # The privileged operation deferred deletion because a playlist unit
        # was still settling; the web layer must say so instead of falsely
        # reporting "no unused playlist cache files to clear".
        self._login()
        self.engine.mutation_payloads[("playlist_cache_clear_unused",)] = {
            "operation": "playlist_cache_clear_unused",
            "freed_bytes": "0",
            "freed_count": "0",
            "reclaimable_bytes": "817889280",
            "reclaimable_count": "3",
            "blocked": "1",
        }
        rv = self._clear_post()
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Playlist cache is still in use. Try again shortly.", html)
        self.assertNotIn("No unused playlist cache files to clear.", html)

    # ------------------------------------------------------------------
    # Integration: drive the REAL EngineClient (production command + JSON
    # parsing) with an envelope produced by the real web-ctl json_helper,
    # then through the Flask clear route. This guards the exact subprocess
    # envelope shape (all values serialized as strings) that the FakeEngine
    # mocks cannot reproduce, so a parse/structure regression can never
    # silently show the wrong flash message.
    # ------------------------------------------------------------------
    def _real_clear_engine(self, fields):
        """Return a production EngineClient whose _run_argv yields a real
        web-ctl-style envelope for each operation the page needs, and the
        real json_helper envelope for playlist_cache_clear_unused. This lets
        the actual EngineClient._mutation JSON parsing drive the clear route
        while the surrounding page reads still render."""
        tokens = []
        for key, value in fields.items():
            tokens.extend([key, value])
        stream = ("\0".join(tokens) + "\0").encode("utf-8")
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "webapp" / "json_helper.py"), "object"],
            input=stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        clear_envelope = proc.stdout.decode("utf-8")
        # The read operations the console page renders (mirrors web-ctl output).
        status_data = {
            "total_bytes": "8516825",
            "artifact_count": "2",
            "protected_bytes": "0",
            "protected_count": "0",
            "reclaimable_bytes": "8516825",
            "reclaimable_count": "2",
        }
        envelopes = {
            "playlist_list": json.dumps({"ok": True, "data": []}),
            "relay_list": json.dumps(
                {"ok": True, "data": DEFAULT_PAYLOADS["relay_list"]}
            ),
            "media_list": json.dumps({"ok": True, "data": []}),
            "snapshot": json.dumps({"ok": True, "data": VALID_SNAPSHOT}),
            "playlist_cache_status": json.dumps({"ok": True, "data": status_data}),
            "playlist_cache_clear_unused": clear_envelope,
        }
        engine = EngineClient(production=True)
        captured = {}

        def fake_run_argv(argv, timeout=None, stdin_data=None):
            op = argv[-1]
            if op not in envelopes:
                raise EngineError("unexpected operation in real-engine test: %r" % op)
            if op == "playlist_cache_clear_unused":
                captured["clear_argv"] = list(argv)
            return 0, envelopes[op]

        engine._run_argv = fake_run_argv
        self._real_clear_argv = captured
        return engine

    def _real_clear(self, fields):
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self._real_clear_engine(fields),
        )
        self.client = self.app.test_client()
        self._login()
        rv = self._clear_post()
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/playlists").get_data(as_text=True)
        return html

    def test_12_real_envelope_clear_reports_success(self):
        # The exact live envelope: fully stopped/reclaimable cache, freed 2.
        html = self._real_clear(
            {
                "operation": "playlist_cache_clear_unused",
                "freed_bytes": "8516825",
                "freed_count": "2",
                "reclaimable_bytes": "8516825",
                "reclaimable_count": "2",
                "blocked": "0",
            }
        )
        self.assertEqual(
            self._real_clear_argv["clear_argv"],
            [
                engine_module.SUDO_PATH,
                "-n",
                engine_module.INSTALLED_WEB_CTL,
                "playlist_cache_clear_unused",
            ],
        )
        self.assertIn("Cleared 8.1 MiB from the playlist cache (2 files).", html)
        self.assertNotIn("No unused playlist cache files to clear.", html)

    def test_13_real_envelope_nothing_reclaimable_shows_no_unused(self):
        # freed_count=0, blocked=0: nothing was reclaimable. This is exactly
        # the branch reached when a live browser request's privileged clear
        # found every artifact protected (or an empty cache).
        html = self._real_clear(
            {
                "operation": "playlist_cache_clear_unused",
                "freed_bytes": "0",
                "freed_count": "0",
                "reclaimable_bytes": "0",
                "reclaimable_count": "0",
                "blocked": "0",
            }
        )
        self.assertIn("No unused playlist cache files to clear.", html)
        self.assertNotIn("Playlist cache is still in use.", html)

    def test_14_real_envelope_blocked_shows_still_in_use(self):
        # freed_count=0, blocked=1: deferred by a settling transition.
        html = self._real_clear(
            {
                "operation": "playlist_cache_clear_unused",
                "freed_bytes": "0",
                "freed_count": "0",
                "reclaimable_bytes": "8516825",
                "reclaimable_count": "2",
                "blocked": "1",
            }
        )
        self.assertIn("Playlist cache is still in use. Try again shortly.", html)
        self.assertNotIn("No unused playlist cache files to clear.", html)


class Gui1dPlaylistWorkflowTests(unittest.TestCase):
    """GUI-1D.1 playlist builder: friendly names, ordered media selection,
    fail-closed media validation, stopped-start state, HLS URL display, and
    the playlist lifecycle actions.  Uses a FakeEngine so no privileged bridge
    call is ever made; auth, CSRF, GET-vs-POST and PRG are exercised end to end.
    """

    PLAYLISTS_PAYLOADS = {
        "snapshot": {
            "version": "0.1.0",
            "hostname": "testhost",
            "uptime": "up 1 day",
            "domain": "example.com",
            "https_configured": "yes",
            "nginx_active": "no",
            "ffmpeg_available": "yes",
            "ffprobe_available": "yes",
            "relay_count": "0",
            "playlist_count": "1",
        },
        "relay_list": [],
        "playlist_list": [
            {
                "name": "evening-promo-loop",
                "active": "no",
                "enabled": "no",
                "items": "2",
                "health": "STOPPED",
            }
        ],
        "media_list": [
            {"name": "intro.mp4", "size": "1000", "extension": "mp4"},
            {"name": "advert-01.mp4", "size": "2000", "extension": "mp4"},
            {"name": "advert-02.mp4", "size": "3000", "extension": "mp4"},
            {"name": "closing.mp4", "size": "4000", "extension": "mp4"},
        ],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.PLAYLISTS_PAYLOADS)
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def _login(self):
        login(self.client)

    def _csrf(self, path="/console/playlists/create"):
        return get_csrf(self.client, path=path)

    def _post_create(self, data):
        return self.client.post(
            "/console/playlists/create",
            data=dict(data, csrf_token=self._csrf()),
        )

    # ------------------------------------------------------------------
    # Playlist name normalization (friendly -> strict internal ID).
    # ------------------------------------------------------------------
    def test_01_friendly_name_normalized_to_internal_id(self):
        self._login()
        rv = self._post_create(
            {
                "name": "Evening Promo Loop",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/playlists", rv.headers["Location"])
        # only the normalized safe internal ID crosses the privileged boundary
        self.assertEqual(
            self.engine.mutation_calls,
            [("playlist_create", "evening-promo-loop", "intro.mp4", "closing.mp4")],
        )

    def test_02_repeated_spaces_and_punctuation_normalize_safely(self):
        self._login()
        rv = self._post_create(
            {
                "name": "Evening!!!  Promo Loop!!",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(
            self.engine.mutation_calls,
            [("playlist_create", "evening-promo-loop", "intro.mp4", "closing.mp4")],
        )

    def test_03_empty_or_invalid_name_rejected(self):
        self._login()
        for bad in ("", "   ", "!!!", "..", "x" * 60):
            rv = self._post_create(
                {
                    "name": bad,
                    "items": ["intro.mp4", "closing.mp4"],
                    "order_intro.mp4": "1",
                    "order_closing.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302, repr(bad))
            self.assertEqual(self.engine.mutation_calls, [], repr(bad))
        html = self.client.get("/console/playlists/create").get_data(as_text=True)
        self.assertIn("We could not create a safe playlist name", html)
        self.assertNotIn("Traceback", html)

    # ------------------------------------------------------------------
    # Playlist creation (ordered items, stopped start state).
    # ------------------------------------------------------------------
    def test_05_valid_two_item_creation_prg(self):
        self._login()
        rv = self._post_create(
            {
                "name": "Evening Promo Loop",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/playlists", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("playlist_create", "evening-promo-loop", "intro.mp4", "closing.mp4")],
        )
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("evening-promo-loop", html)
        self.assertIn("It is stopped", html)

    def test_06_playback_order_preserved_exactly(self):
        self._login()
        # order numbers deliberately differ from the checkbox submission order
        rv = self._post_create(
            {
                "name": "Reversed",
                "items": ["intro.mp4", "advert-01.mp4", "closing.mp4"],
                "order_intro.mp4": "3",
                "order_advert-01.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(
            self.engine.mutation_calls,
            [
                (
                    "playlist_create",
                    "reversed",
                    "advert-01.mp4",
                    "closing.mp4",
                    "intro.mp4",
                )
            ],
        )

    def test_07_created_playlist_starts_stopped(self):
        self._login()
        self._post_create(
            {
                "name": "Loop",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("It is stopped", html)
        self.assertIn("STOPPED", html)

    def test_08_duplicate_playlist_rejected_without_overwrite(self):
        self._login()
        self.engine.mutation_payloads[
            ("playlist_create", "evening-promo-loop", "intro.mp4", "closing.mp4")
        ] = EngineError(
            "a playlist named 'evening-promo-loop' already exists",
            code="ALREADY_EXISTS",
        )
        rv = self._post_create(
            {
                "name": "Evening Promo Loop",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/playlists/create").get_data(as_text=True)
        self.assertIn(
            "A playlist named &#39;evening-promo-loop&#39; already exists.", html
        )
        self.assertNotIn("Traceback", html)

    def test_09_too_few_items_rejected(self):
        self._login()
        rv = self._post_create(
            {
                "name": "Solo",
                "items": ["intro.mp4"],
                "order_intro.mp4": "1",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = self.client.get("/console/playlists/create").get_data(as_text=True)
        self.assertIn("Select at least two media files.", html)

    # ------------------------------------------------------------------
    # Media selection security (never trust the browser).
    # ------------------------------------------------------------------
    def test_11_unsafe_media_identifier_rejected(self):
        self._login()
        for item in ("evil;rm -rf /tmp/x.mp4", "a b.mp4", "&x=1.mp4"):
            rv = self._post_create(
                {
                    "name": "bad",
                    "items": [item, "intro.mp4"],
                    "order_" + item: "1",
                    "order_intro.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302, item)
            self.assertEqual(self.engine.mutation_calls, [], item)

    def test_12_traversal_attempt_rejected(self):
        self._login()
        rv = self._post_create(
            {
                "name": "bad",
                "items": ["../evil.mp4", "intro.mp4"],
                "order_../evil.mp4": "1",
                "order_intro.mp4": "2",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_13_absolute_path_rejected(self):
        self._login()
        for item in ("/etc/passwd", "/var/lib/bluestream/media/x.mp4"):
            rv = self._post_create(
                {
                    "name": "bad",
                    "items": [item, "intro.mp4"],
                    "order_" + item: "1",
                    "order_intro.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302, item)
            self.assertEqual(self.engine.mutation_calls, [], item)

    def test_14_windows_style_path_rejected(self):
        self._login()
        for item in ("C:\\evil\\x.mp4", "..\\evil.mp4", ".\\evil.mp4"):
            rv = self._post_create(
                {
                    "name": "bad",
                    "items": [item, "intro.mp4"],
                    "order_" + item: "1",
                    "order_intro.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302, item)
            self.assertEqual(self.engine.mutation_calls, [], item)

    def test_15_no_arbitrary_privileged_path_reaches_engine(self):
        self._login()
        for item in ("../evil.mp4", "/etc/passwd", "C:\\x.mp4", "a;b.mp4"):
            rv = self._post_create(
                {
                    "name": "bad",
                    "items": [item, "intro.mp4"],
                    "order_" + item: "1",
                    "order_intro.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302)
        # duplicate order numbers are also rejected before any engine call
        rv = self._post_create(
            {
                "name": "dup-order",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "1",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    # ------------------------------------------------------------------
    # Order validation.
    # ------------------------------------------------------------------
    def test_16_non_sequential_order_rejected(self):
        self._login()
        rv = self._post_create(
            {
                "name": "gap",
                "items": ["intro.mp4", "closing.mp4"],
                "order_intro.mp4": "1",
                "order_closing.mp4": "3",
            }
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = self.client.get("/console/playlists/create").get_data(as_text=True)
        self.assertIn("Play order numbers must be 1 to 2 in sequence.", html)

    def test_17_non_numeric_or_zero_order_rejected(self):
        self._login()
        for order in ("", "abc", "0", "-1"):
            rv = self._post_create(
                {
                    "name": "badorder",
                    "items": ["intro.mp4", "closing.mp4"],
                    "order_intro.mp4": order,
                    "order_closing.mp4": "2",
                }
            )
            self.assertEqual(rv.status_code, 302, repr(order))
            self.assertEqual(self.engine.mutation_calls, [], repr(order))

    # ------------------------------------------------------------------
    # Playlist list page: columns, HLS URL, health, lifecycle actions.
    # ------------------------------------------------------------------
    def test_19_playlists_page_shows_columns_and_hls_url(self):
        self._login()
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("evening-promo-loop", html)
        self.assertIn(">2<", html)
        self.assertIn("STOPPED", html)
        self.assertIn(
            "https://example.com/hls/playlist/evening-promo-loop/index.m3u8", html
        )
        self.assertIn("/console/playlists/evening-promo-loop/start", html)
        self.assertIn("/console/playlists/evening-promo-loop/stop", html)
        self.assertIn("/console/playlists/evening-promo-loop/restart", html)
        # GUI-5A/1B: a fixed safe-delete control and a fixed destinations
        # assignment link, each posting to its own single-purpose route.
        self.assertIn("/console/playlists/evening-promo-loop/delete", html)
        self.assertIn("/console/playlists/evening-promo-loop/destinations", html)

    def test_20_create_page_lists_media_with_order_fields(self):
        self._login()
        html = self.client.get("/console/playlists/create").get_data(as_text=True)
        self.assertIn("Playlist Name", html)
        self.assertIn("name=\"items\"", html)
        self.assertIn("order_intro.mp4", html)
        self.assertIn("order_closing.mp4", html)
        self.assertIn('name="csrf_token"', html)

    def test_21_hls_url_helper_matches_expected_structure(self):
        url = app_module.playlist_hls_url(
            {"domain": "example.com", "https_configured": "yes"},
            "evening-promo-loop",
        )
        self.assertEqual(
            url, "https://example.com/hls/playlist/evening-promo-loop/index.m3u8"
        )
        # no authoritative domain -> no URL (never fabricated from Host header)
        self.assertIsNone(app_module.playlist_hls_url({}, "evening-promo-loop"))
        self.assertIsNone(app_module.playlist_hls_url(None, "evening-promo-loop"))

    def test_22_item_order_validator_unit(self):
        from werkzeug.datastructures import ImmutableMultiDict

        ok, ordered, error = app_module._validate_playlist_items(
            ImmutableMultiDict(
                [
                    ("items", "intro.mp4"),
                    ("items", "closing.mp4"),
                    ("order_intro.mp4", "2"),
                    ("order_closing.mp4", "1"),
                ]
            )
        )
        self.assertTrue(ok)
        self.assertEqual(ordered, ["closing.mp4", "intro.mp4"])
        ok, ordered, error = app_module._validate_playlist_items(
            ImmutableMultiDict(
                [("items", "intro.mp4"), ("order_intro.mp4", "1")]
            )
        )
        self.assertFalse(ok)
        self.assertIn("at least two", error)

    def test_23_lifecycle_actions_call_only_expected_operations(self):
        self._login()
        token = get_csrf(self.client, path="/console/playlists")
        for action in ("start", "stop", "restart"):
            rv = self.client.post(
                "/console/playlists/evening-promo-loop/%s" % action,
                data={"csrf_token": token},
            )
            self.assertEqual(rv.status_code, 302, action)
        self.assertEqual(
            sorted(self.engine.mutation_calls),
            [
                ("playlist_restart", "evening-promo-loop"),
                ("playlist_start", "evening-promo-loop"),
                ("playlist_stop", "evening-promo-loop"),
            ],
        )

    def test_24_intentional_stop_displays_stopped_not_failed(self):
        self._login()
        payloads = dict(self.PLAYLISTS_PAYLOADS)
        payloads["playlist_list"] = [
            {
                "name": "evening-promo-loop",
                "active": "no",
                "enabled": "yes",
                "items": "2",
                "health": "STOPPED",
            }
        ]
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=FakeEngine(payloads)
        )
        client = app.test_client()
        login(client)
        html = client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("STOPPED", html)
        self.assertNotIn("FAILED", html)


class GuiStreamPlaylistLinkTests(unittest.TestCase):
    """GUI-2C/1D: copyable public M3U8 + Web Player links for streams and
    playlists, derived from the engine's trusted server config (never the Host
    header).  No source/private URL copy control, no embed/multistreaming, no
    hard-coded production domain, and HTTPS follows existing SSL config.
    """

    SNAPSHOT_HTTPS = {
        "version": "0.1.0",
        "hostname": "testhost",
        "uptime": "up 1 day",
        "domain": "example.com",
        "https_configured": "yes",
        "nginx_active": "no",
        "ffmpeg_available": "yes",
        "ffprobe_available": "yes",
        "relay_count": "1",
        "playlist_count": "1",
    }
    PAYLOADS = {
        "snapshot": SNAPSHOT_HTTPS,
        "relay_list": [
            {
                "name": "news",
                "type": "remote-hls",
                "source": "https://source.example.com/live/index.m3u8",
                "active": "no",
                "enabled": "yes",
                "health": "STOPPED",
            }
        ],
        "playlist_list": [
            {
                "name": "evening-promo-loop",
                "active": "no",
                "enabled": "no",
                "items": "2",
                "health": "STOPPED",
            }
        ],
        "media_list": [{"name": "intro.mp4", "size": "1000", "extension": "mp4"}],
        "playlist_cache_status": {
            "total_bytes": "0",
            "artifact_count": "0",
            "protected_bytes": "0",
            "protected_count": "0",
            "reclaimable_bytes": "0",
            "reclaimable_count": "0",
        },
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(self.PAYLOADS)
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name),
            engine=self.engine,
        )
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self._tmp.cleanup()

    def _streams(self):
        return self.client.get("/console/streams").get_data(as_text=True)

    def _playlists(self):
        return self.client.get("/console/playlists").get_data(as_text=True)

    def test_01_stream_m3u8_url_rendered(self):
        self.assertIn("https://example.com/hls/relay/news/index.m3u8", self._streams())

    def test_02_stream_player_url_rendered(self):
        self.assertIn("https://example.com/player/?relay=news", self._streams())

    def test_03_playlist_m3u8_url_rendered(self):
        self.assertIn(
            "https://example.com/hls/playlist/evening-promo-loop/index.m3u8",
            self._playlists(),
        )

    def test_04_playlist_player_url_rendered(self):
        self.assertIn(
            "https://example.com/player/?playlist=evening-promo-loop", self._playlists()
        )

    def test_05_uses_configured_domain_not_hardcoded(self):
        for html in (self._streams(), self._playlists()):
            self.assertIn("example.com", html)
            self.assertNotIn("stream.therealworldboosts.com", html)

    def test_06_https_respected_from_config(self):
        self.assertIn("https://example.com/hls/relay/news/index.m3u8", self._streams())
        payloads = dict(self.PAYLOADS)
        payloads["snapshot"] = dict(self.SNAPSHOT_HTTPS, https_configured="no")
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=FakeEngine(payloads)
        )
        client = app.test_client()
        login(client)
        html = client.get("/console/streams").get_data(as_text=True)
        self.assertIn("http://example.com/hls/relay/news/index.m3u8", html)
        self.assertNotIn("https://example.com/hls/relay/news/index.m3u8", html)

    def test_07_stream_url_uses_relay_path(self):
        self.assertIn("/hls/relay/news/index.m3u8", self._streams())

    def test_08_playlist_url_uses_playlist_path(self):
        self.assertIn("/hls/playlist/evening-promo-loop/index.m3u8", self._playlists())

    def test_09_player_stream_query_is_relay(self):
        self.assertIn("?relay=news", self._streams())

    def test_10_player_playlist_query_is_playlist(self):
        self.assertIn("?playlist=evening-promo-loop", self._playlists())

    def test_11_stream_copy_controls_present(self):
        html = self._streams()
        self.assertEqual(html.count('class="btn btn-sm copy-btn"'), 2)
        self.assertIn("M3U8", html)
        self.assertIn("Web Player", html)
        self.assertIn('class="link-url"', html)

    def test_12_playlist_copy_controls_present(self):
        html = self._playlists()
        self.assertEqual(html.count('class="btn btn-sm copy-btn"'), 2)
        self.assertIn("M3U8", html)
        self.assertIn("Web Player", html)
        self.assertIn('class="link-url"', html)

    def test_13_stream_lifecycle_controls_render(self):
        html = self._streams()
        self.assertIn("/console/relays/news/start", html)
        self.assertIn("/console/relays/news/stop", html)
        self.assertIn("/console/relays/news/restart", html)

    def test_14_playlist_lifecycle_controls_render(self):
        html = self._playlists()
        self.assertIn("/console/playlists/evening-promo-loop/start", html)
        self.assertIn("/console/playlists/evening-promo-loop/stop", html)
        self.assertIn("/console/playlists/evening-promo-loop/restart", html)

    def test_15_playlist_cache_ui_still_renders(self):
        html = self._playlists()
        self.assertIn("Playlist Cache", html)
        self.assertIn("Clear Unused Cache", html)

    def test_16_no_source_private_url_copy_control(self):
        html = self._streams()
        # the private/authenticated source URL stays a plain read-only table
        # cell - never a copyable link input, never near a Copy button.
        self.assertIn("https://source.example.com/live/index.m3u8", html)
        self.assertIn('class="source"', html)
        self.assertNotIn('value="https://source.example.com/live/index.m3u8"', html)

    def test_17_url_values_are_escaped(self):
        payloads = dict(self.PAYLOADS)
        payloads["relay_list"] = [
            {
                "name": '"><script>alert(1)</script>',
                "type": "remote-hls",
                "source": "https://src.example.com/x",
                "active": "no",
                "enabled": "no",
                "health": "STOPPED",
            }
        ]
        app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=FakeEngine(payloads)
        )
        client = app.test_client()
        login(client)
        html = client.get("/console/streams").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn('value="https://example.com/hls/relay/">', html)

    def test_18_active_navigation_unchanged(self):
        streams_html = self._streams()
        playlists_html = self._playlists()
        self.assertRegex(
            streams_html, r'href="[^"]*streams[^"]*"[^>]*aria-current="page"'
        )
        self.assertRegex(
            playlists_html, r'href="[^"]*playlists[^"]*"[^>]*aria-current="page"'
        )

    def test_19_url_helper_units(self):
        snap = {"domain": "example.com", "https_configured": "yes"}
        self.assertEqual(
            app_module.relay_hls_url(snap, "news"),
            "https://example.com/hls/relay/news/index.m3u8",
        )
        self.assertEqual(
            app_module.relay_player_url(snap, "news"),
            "https://example.com/player/?relay=news",
        )
        self.assertEqual(
            app_module.playlist_player_url(snap, "evening-promo-loop"),
            "https://example.com/player/?playlist=evening-promo-loop",
        )
        self.assertEqual(
            app_module._public_base({"domain": "x.com", "https_configured": "no"}),
            "http://x.com",
        )
        self.assertIsNone(app_module._public_base({}))
        self.assertIsNone(app_module.relay_hls_url(None, "news"))


class DashboardUtilitiesTests(unittest.TestCase):
    """GUI-4: CPU / RAM / Storage dashboard gauges + /console/system-metrics.

    All metrics are mocked or use non-existent paths so the suite never
    depends on the local machine having Linux /proc.
    """

    MEMINFO = (
        "MemTotal:        8000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:    3000000 kB\n"
        "Buffers:          200000 kB\n"
        "Cached:          1500000 kB\n"
        "SwapTotal:       2000000 kB\n"
        "SwapFree:        2000000 kB\n"
    )
    STAT_SAMPLE_1 = (100, 50, 80, 1000, 100, 0, 0, 0)
    STAT_SAMPLE_2 = (120, 70, 100, 1180, 120, 0, 0, 0)

    FULL_METRICS = {
        "cpu_percent": 18.4,
        "cpu_count": 8,
        "ram_percent": 37.2,
        "ram_used_bytes": 3000000000,
        "ram_total_bytes": 8000000000,
        "storage_percent": 4.1,
        "storage_used_bytes": 3200000000,
        "storage_total_bytes": 96000000000,
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = make_state(self._tmp.name)
        self.engine = FakeEngine(DEFAULT_PAYLOADS)
        self.app = app_module.create_app(state_dir=self.state, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self._tmp.cleanup()

    def _dashboard_html(self):
        return self.client.get("/console/").get_data(as_text=True)

    # ------------------------------------------------------------------
    # Cards render
    # ------------------------------------------------------------------
    def test_01_dashboard_renders_cpu_card(self):
        html = self._dashboard_html()
        self.assertIn("Server Utilities", html)
        self.assertIn('data-metric="cpu"', html)
        self.assertIn("CPU", html)
        self.assertIn("Server CPU usage", html)

    def test_02_dashboard_renders_ram_card(self):
        html = self._dashboard_html()
        self.assertIn('data-metric="ram"', html)
        self.assertIn('<h3>RAM</h3>', html)
        self.assertIn('class="metric-detail"', html)

    def test_03_dashboard_renders_storage_card(self):
        html = self._dashboard_html()
        self.assertIn('data-metric="storage"', html)
        self.assertIn("Storage", html)

    # ------------------------------------------------------------------
    # CPU calculation units (mocked /proc/stat data)
    # ------------------------------------------------------------------
    def test_04_cpu_percent_calculation_from_mocked_stat(self):
        parsed = metrics_module.cpu_counters(
            "cpu  100 50 80 1000 100 0 0 0 0 0"
        )
        self.assertEqual(parsed, self.STAT_SAMPLE_1)
        # busy = 60, total delta = 260  ->  23.0769%
        pct = metrics_module.cpu_percent(self.STAT_SAMPLE_1, self.STAT_SAMPLE_2)
        self.assertAlmostEqual(pct, 60.0 / 260.0 * 100.0, places=5)
        # _read_stat_cpu parses the aggregate line from a fake /proc/stat file
        stat_file = Path(self._tmp.name) / "procstat"
        stat_file.write_text(
            "cpu  " + " ".join(str(v) for v in self.STAT_SAMPLE_1)
            + " 0 0\nintr 123\nctxt 456\n",
            encoding="utf-8",
        )
        self.assertEqual(metrics_module._read_stat_cpu(stat_file), self.STAT_SAMPLE_1)

    def test_05_cpu_calculation_cannot_divide_by_zero(self):
        # Identical samples (zero delta) must yield 0.0, never a ZeroDivisionError.
        self.assertEqual(
            metrics_module.cpu_percent(self.STAT_SAMPLE_1, self.STAT_SAMPLE_1), 0.0
        )
        self.assertEqual(
            metrics_module.cpu_percent((1, 2, 3, 4), (1, 2, 3, 4)), 0.0
        )
        self.assertIsNone(metrics_module.cpu_percent(None, None))
        self.assertIsNone(metrics_module.cpu_percent(self.STAT_SAMPLE_1, None))
        self.assertIsNone(metrics_module.cpu_counters("not a stat line"))
        self.assertIsNone(metrics_module.cpu_counters("cpu0 1 2 3 4 5"))

    def test_06_cpu_result_clamped_to_0_100(self):
        # idle shrank while total grew -> raw 300% clamps to 100.0
        a = (0, 0, 0, 1000, 0, 0, 0, 0)
        b = (600, 0, 0, 600, 0, 0, 0, 0)
        self.assertEqual(metrics_module.cpu_percent(a, b), 100.0)
        # idle grew more than total -> raw negative clamps to 0.0
        a2 = (100, 0, 0, 500, 0, 0, 0, 0)
        b2 = (50, 0, 0, 1200, 0, 0, 0, 0)
        self.assertEqual(metrics_module.cpu_percent(a2, b2), 0.0)
        # the shared clamp is a hard 0-100 wall for any input
        self.assertEqual(metrics_module._clamp_percent(150), 100.0)
        self.assertEqual(metrics_module._clamp_percent(-5), 0.0)
        self.assertEqual(metrics_module._clamp_percent("garbage"), 0.0)
        self.assertEqual(metrics_module._clamp_percent(42.5), 42.5)



    # ------------------------------------------------------------------
    # RAM calculation units (mocked /proc/meminfo data)
    # ------------------------------------------------------------------
    def test_07_ram_uses_memavailable(self):
        # used must be MemTotal - MemAvailable, NOT MemTotal - MemFree.
        # MemFree (1,000,000) + Buffers + Cached are present but must NOT be
        # treated as permanently used RAM. meminfo reports KiB, so the byte
        # fields must reflect the ×1024 conversion.
        metrics = metrics_module.ram_metrics(self.MEMINFO)
        self.assertIsNotNone(metrics)
        self.assertEqual(
            metrics["ram_used_bytes"], (8000000 - 3000000) * 1024
        )
        self.assertEqual(metrics["ram_total_bytes"], 8000000 * 1024)

    def test_08_ram_used_bytes_calculated_correctly(self):
        metrics = metrics_module.ram_metrics(self.MEMINFO)
        # MemTotal 8,000,000 KiB -> 8,192,000,000 bytes; used 5,000,000 KiB
        # -> 5,120,000,000 bytes. Fields ending in _bytes are true bytes.
        self.assertEqual(metrics["ram_used_bytes"], 5000000 * 1024)
        self.assertEqual(metrics["ram_total_bytes"], 8000000 * 1024)

    def test_09_ram_percentage_correct(self):
        metrics = metrics_module.ram_metrics(self.MEMINFO)
        self.assertAlmostEqual(metrics["ram_percent"], 62.5, places=6)

    def test_10_storage_used_total_percent_correct(self):
        result = metrics_module.storage_metrics(
            "/var/lib/bluestream/web/upload",
            disk_usage=lambda p: _DiskUsage(
                total=96 * 1024 ** 3, used=3 * 1024 ** 3, free=93 * 1024 ** 3
            ),
        )
        self.assertEqual(result["storage_total_bytes"], 96 * 1024 ** 3)
        self.assertEqual(result["storage_used_bytes"], 3 * 1024 ** 3)
        self.assertAlmostEqual(result["storage_percent"], 3.125, places=6)

    # ------------------------------------------------------------------
    # JSON endpoint: authentication + structured, bounded response
    # ------------------------------------------------------------------
    def test_11_metrics_endpoint_requires_auth(self):
        anon = app_module.create_app(state_dir=self.state, engine=self.engine)
        rv = anon.test_client().get("/console/system-metrics")
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertNotIn("cpu_percent", rv.get_data(as_text=True))
        self.assertNotIn("cpu_count", rv.get_data(as_text=True))

    def test_12_authenticated_metrics_endpoint_returns_structured_json(self):
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=self.FULL_METRICS
        ):
            rv = self.client.get("/console/system-metrics")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("application/json", rv.headers.get("Content-Type", ""))
        payload = rv.get_json()
        self.assertEqual(
            payload,
            {
                "cpu_percent": 18.4,
                "cpu_count": 8,
                "ram_percent": 37.2,
                "ram_used_bytes": 3000000000,
                "ram_total_bytes": 8000000000,
                "storage_percent": 4.1,
                "storage_used_bytes": 3200000000,
                "storage_total_bytes": 96000000000,
            },
        )
        # every percentage the endpoint can return stays in 0-100
        for key in ("cpu_percent", "ram_percent", "storage_percent"):
            self.assertGreaterEqual(payload[key], 0)
            self.assertLessEqual(payload[key], 100)

    def test_13_metrics_endpoint_does_not_expose_filesystem_paths(self):
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=self.FULL_METRICS
        ):
            body = self.client.get("/console/system-metrics").get_data(as_text=True)
        for leak in ("/var/lib", "/proc", "upload", "C:", "\\\\", "web.conf"):
            self.assertNotIn(leak, body)

    def test_14_metrics_endpoint_does_not_expose_shell_or_config(self):
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=self.FULL_METRICS
        ):
            body = self.client.get("/console/system-metrics").get_data(as_text=True)
        payload = json.loads(body)
        self.assertEqual(set(payload.keys()), set(metrics_module.METRIC_KEYS))
        for forbidden in (
            "hostname", "username", "password", "command", "shell",
            "sudo", "web-ctl", "config", "env", "token",
        ):
            self.assertNotIn(forbidden, body)

    # ------------------------------------------------------------------
    # Graceful failure behaviour (one bad source never breaks anything)
    # ------------------------------------------------------------------
    def test_15_missing_proc_stat_fails_gracefully(self):
        missing = Path(self._tmp.name) / "no" / "proc" / "stat"
        self.assertIsNone(metrics_module._read_stat_cpu(missing))
        collected = metrics_module.collect_metrics(
            stat_path=str(missing),
            meminfo_path=str(missing),
            storage_path=None,
            sample_interval=0,
        )
        self.assertIsNone(collected["cpu_percent"])
        self.assertIsNone(collected["ram_percent"])
        self.assertIsNone(collected["storage_percent"])


    def test_16_missing_or_malformed_meminfo_fails_gracefully(self):
        self.assertIsNone(metrics_module.ram_metrics(""))
        self.assertIsNone(metrics_module.ram_metrics("not meminfo at all"))
        self.assertIsNone(metrics_module.ram_metrics("MemTotal: abc kB\n"))
        self.assertIsNone(metrics_module.ram_metrics("MemTotal: 1000 kB\n"))
        self.assertIsNone(metrics_module.ram_metrics(None))
        missing = Path(self._tmp.name) / "no" / "meminfo"
        self.assertIsNone(metrics_module._collect_ram(missing))

    def test_17_storage_read_failure_fails_gracefully(self):
        def broken(path):
            raise OSError("no such file")

        self.assertIsNone(metrics_module.storage_metrics("/nope", disk_usage=broken))
        self.assertIsNone(
            metrics_module.storage_metrics(
                "/nope", disk_usage=lambda p: _DiskUsage(total=0, used=0, free=0)
            )
        )
        self.assertIsNone(
            metrics_module.storage_metrics("/nope", disk_usage=lambda p: None)
        )
        self.assertIsNone(metrics_module._collect_storage(None))

    def test_18_failure_of_one_metric_keeps_other_metrics(self):
        with mock.patch.object(
            metrics_module, "_collect_cpu", return_value=None
        ), mock.patch.object(
            metrics_module,
            "_collect_ram",
            return_value={
                "ram_percent": 10.0,
                "ram_used_bytes": 1000,
                "ram_total_bytes": 10000,
            },
        ), mock.patch.object(
            metrics_module,
            "_collect_storage",
            return_value={
                "storage_percent": 20.0,
                "storage_used_bytes": 2000,
                "storage_total_bytes": 10000,
            },
        ):
            rv = self.client.get("/console/system-metrics")
        payload = rv.get_json()
        self.assertIsNone(payload["cpu_percent"])
        self.assertEqual(payload["ram_percent"], 10.0)
        self.assertEqual(payload["ram_used_bytes"], 1000)
        self.assertEqual(payload["storage_percent"], 20.0)
        self.assertEqual(payload["storage_used_bytes"], 2000)

    def test_19_dashboard_still_renders_when_metrics_unavailable(self):
        unavailable = dict.fromkeys(metrics_module.METRIC_KEYS)
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=unavailable
        ):
            rv = self.client.get("/console/")
        self.assertEqual(rv.status_code, 200)
        html = rv.get_data(as_text=True)
        self.assertIn("Server Utilities", html)
        self.assertIn("&mdash;", html)
        # the rest of the dashboard is untouched
        self.assertIn("Quick actions", html)
        self.assertIn("Relays", html)
        self.assertIn("news", html)

    def test_20_percentages_cannot_exceed_100_or_below_0(self):
        # RAM: fully exhausted -> 100.0; impossible available > total -> 0.0
        full = metrics_module.ram_metrics("MemTotal: 8000000 kB\nMemAvailable: 0 kB\n")
        self.assertEqual(full["ram_percent"], 100.0)
        bogus = metrics_module.ram_metrics(
            "MemTotal: 8000000 kB\nMemAvailable: 9000000 kB\n"
        )
        self.assertEqual(bogus["ram_percent"], 0.0)
        self.assertEqual(bogus["ram_used_bytes"], 0)
        # Storage: used > total is normalized to 100.0 and used == total
        over = metrics_module.storage_metrics(
            "/x", disk_usage=lambda p: _DiskUsage(total=1000, used=5000, free=0)
        )
        self.assertEqual(over["storage_percent"], 100.0)
        self.assertEqual(over["storage_used_bytes"], 1000)
        empty = metrics_module.storage_metrics(
            "/x", disk_usage=lambda p: _DiskUsage(total=1000, used=0, free=1000)
        )
        self.assertEqual(empty["storage_percent"], 0.0)
        # collect_metrics() clamps every percentage into 0-100 before the
        # endpoint ever serializes it (even for raw out-of-range collector
        # outputs, which only exist for malformed/hostile data).
        with mock.patch.object(
            metrics_module, "_collect_cpu", return_value=150.0
        ), mock.patch.object(
            metrics_module,
            "_collect_ram",
            return_value={
                "ram_percent": -3.0,
                "ram_used_bytes": 1,
                "ram_total_bytes": 2,
            },
        ), mock.patch.object(
            metrics_module,
            "_collect_storage",
            return_value={
                "storage_percent": 200.0,
                "storage_used_bytes": 1,
                "storage_total_bytes": 2,
            },
        ):
            collected = metrics_module.collect_metrics(sample_interval=0)
        self.assertEqual(collected["cpu_percent"], 100.0)
        self.assertEqual(collected["ram_percent"], 0.0)
        self.assertEqual(collected["storage_percent"], 100.0)
        # and the endpoint only ever serializes the clamped values
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=collected
        ):
            body = self.client.get("/console/system-metrics").get_data(as_text=True)
        self.assertNotIn("150.0", body)
        self.assertNotIn("-3.0", body)
        self.assertNotIn("200.0", body)




    # ------------------------------------------------------------------
    # Frontend behaviour + regression guards
    # ------------------------------------------------------------------
    def test_21_auto_refresh_js_targets_metrics_endpoint(self):
        html = self._dashboard_html()
        self.assertIn("/console/system-metrics", html)
        self.assertIn("fetch(METRICS_ENDPOINT", html)
        self.assertIn("window.setInterval(refresh, METRICS_REFRESH_MS)", html)

    def test_22_refresh_interval_is_exactly_3000ms(self):
        html = self._dashboard_html()
        match = re.search(r"var METRICS_REFRESH_MS = (\d+);", html)
        self.assertIsNotNone(match, "refresh interval constant missing")
        interval = int(match.group(1))
        self.assertEqual(interval, 3000)

    def test_23_no_sudo_or_webctl_operation_added_for_metrics(self):
        # The engine bridge is untouched: no new privileged operation exists.
        self.assertNotIn("system_metrics", engine_module.ALLOWED_OPERATIONS)
        self.assertNotIn(
            "system_metrics", engine_module.ALLOWED_MUTATION_OPERATIONS
        )
        # metrics.py is stdlib-only: no subprocess, no shell execution. (The
        # docstring may SAY "no web-ctl/sudo"; what matters is nothing runs.)
        source = Path(__file__).resolve().parent.joinpath("metrics.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "import subprocess",
            "subprocess.",
            "os.system",
            "Popen",
            "shell=True",
            "check_output",
            "check_call",
        ):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(metrics_module, "subprocess"))

    def test_24_existing_dashboard_functionality_intact(self):
        html = self._dashboard_html()
        for expected in (
            "Dashboard",
            "Version",
            "Hostname",
            "Uptime",
            "Nginx",
            "FFmpeg",
            "Counts",
            "Quick actions",
            "Create Stream",
            "Upload Media",
            "Relays",
            "news",
            "example.com",
            "Playlists",
        ):
            self.assertIn(expected, html)
        self.assertIn("0.1.0", html)  # VERSION stays 0.1.0

    def test_25_existing_navigation_intact(self):
        html = self._dashboard_html()
        for label in ("Dashboard", "Streams", "Media Library", "Playlists"):
            self.assertIn(label, html)
        self.assertRegex(
            html, r'href="[^"]*console/"' r'[^>]*aria-current="page"'
        )

    def test_26_streams_playlists_link_functionality_untouched(self):
        streams_html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("M3U8", streams_html)
        self.assertIn("Web Player", streams_html)
        self.assertIn('class="btn btn-sm copy-btn"', streams_html)
        self.assertIn('class="link-url"', streams_html)
        self.assertIn("http://example.com/hls/relay/news/index.m3u8", streams_html)
        # playlists page: cache card + lifecycle controls + copy links intact
        payloads = {
            "snapshot": VALID_SNAPSHOT,
            "playlist_list": [
                {
                    "name": "loop",
                    "active": "no",
                    "enabled": "no",
                    "items": "2",
                    "health": "STOPPED",
                }
            ],
            "playlist_cache_status": {
                "total_bytes": "0",
                "artifact_count": "0",
                "protected_bytes": "0",
                "protected_count": "0",
                "reclaimable_bytes": "0",
                "reclaimable_count": "0",
            },
        }
        app2 = app_module.create_app(
            state_dir=self.state, engine=FakeEngine(payloads)
        )
        client2 = app2.test_client()
        login(client2)
        playlists_html = client2.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Playlist Cache", playlists_html)
        self.assertIn("Clear Unused Cache", playlists_html)
        self.assertIn("M3U8", playlists_html)
        self.assertIn("Web Player", playlists_html)
        self.assertIn("http://example.com/hls/playlist/loop/index.m3u8", playlists_html)

    # ------------------------------------------------------------------
    # GUI-4 layout polish: live/action content first, static info lower,
    # and a silent 3-second metrics refresh (no visible interval text).
    # ------------------------------------------------------------------
    def test_27_dashboard_shows_live_content_before_static_info(self):
        html = self._dashboard_html()
        utilities = html.index("<h2>Server Utilities</h2>")
        quick = html.index("<h2>Quick actions</h2>")
        relays = html.index("<h2>Relays</h2>")
        playlists = html.index("<h2>Playlists</h2>")
        server_info = html.index("<h2>Server</h2>")
        self.assertLess(utilities, quick, "Server Utilities should lead the page")
        self.assertLess(quick, relays, "Quick actions should precede Relays")
        self.assertLess(relays, playlists, "Relays should precede Playlists")
        self.assertLess(
            playlists, server_info, "static Server info should be lower"
        )

    def test_28_metrics_initial_fetch_and_single_repeating_timer(self):
        html = self._dashboard_html()
        # the immediate fetch on page load is still there
        self.assertIn("refresh();", html)
        self.assertIn("fetch(METRICS_ENDPOINT", html)
        # exactly ONE repeating interval timer is registered
        self.assertEqual(
            html.count("window.setInterval(refresh, METRICS_REFRESH_MS)"), 1
        )

    def test_29_no_visible_refresh_status_text(self):
        html = self._dashboard_html()
        self.assertNotIn("Updates every", html)
        self.assertNotIn("last updated", html)
        self.assertNotIn("refreshes automatically", html)
        self.assertNotIn("metric-status", html)

    # ------------------------------------------------------------------
    # Dashboard Utilities polish: true RAM bytes (KiB -> bytes) and the
    # logical CPU core count.
    # ------------------------------------------------------------------
    def test_30_cpu_count_uses_safe_stdlib_and_returns_integer(self):
        # os.cpu_count() is the only mechanism - no subprocess/shell tools.
        source = Path(__file__).resolve().parent.joinpath("metrics.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("os.cpu_count()", source)
        self.assertFalse(hasattr(metrics_module, "subprocess"))
        with mock.patch.object(metrics_module.os, "cpu_count", return_value=8):
            self.assertEqual(metrics_module._collect_cpu_count(), 8)
        with mock.patch.object(metrics_module.os, "cpu_count", return_value=1):
            self.assertEqual(metrics_module._collect_cpu_count(), 1)

    def test_31_cpu_count_unavailable_or_invalid_returns_none(self):
        for bad in (None, 0, -2):
            with mock.patch.object(metrics_module.os, "cpu_count", return_value=bad):
                self.assertIsNone(metrics_module._collect_cpu_count())
        with mock.patch.object(
            metrics_module.os, "cpu_count", side_effect=OSError("no sysfs")
        ):
            self.assertIsNone(metrics_module._collect_cpu_count())
        # collect_metrics() surfaces the same safe None.
        with mock.patch.object(metrics_module.os, "cpu_count", return_value=None):
            collected = metrics_module.collect_metrics(
                stat_path=str(Path(self._tmp.name) / "missing"),
                meminfo_path=str(Path(self._tmp.name) / "missing"),
                storage_path=None,
                sample_interval=0,
            )
        self.assertIsNone(collected["cpu_count"])

    def test_32_metrics_endpoint_includes_cpu_count(self):
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=self.FULL_METRICS
        ):
            payload = self.client.get("/console/system-metrics").get_json()
        self.assertEqual(payload["cpu_count"], 8)
        # an unavailable count serializes as JSON null, never an error
        unavailable = dict(self.FULL_METRICS)
        unavailable["cpu_count"] = None
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=unavailable
        ):
            payload = self.client.get("/console/system-metrics").get_json()
        self.assertIsNone(payload["cpu_count"])

    def test_33_cpu_card_renders_core_count(self):
        payload = dict(self.FULL_METRICS)
        payload["cpu_count"] = 8
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=payload
        ):
            html = self.client.get("/console/").get_data(as_text=True)
        cpu_card = html[html.index('data-metric="cpu"') : html.index('data-metric="ram"')]
        self.assertIn("8 cores", cpu_card)
        self.assertIn('data-metric-cores', cpu_card)
        self.assertIn("updateCores(\"cpu\", data.cpu_count);", html)
        # singular wording when exactly one core is reported
        payload["cpu_count"] = 1
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=payload
        ):
            html1 = self.client.get("/console/").get_data(as_text=True)
        cpu_card1 = html1[html1.index('data-metric="cpu"') : html1.index('data-metric="ram"')]
        self.assertIn("1 core", cpu_card1)

    def test_34_dashboard_ram_display_uses_server_byte_units(self):
        # ~8 GiB VPS with ~600 MiB used: human_size() must show MiB/GiB, never
        # the old raw-KiB-as-bytes KiB/MiB scale.
        payload = {
            "cpu_percent": 18.4,
            "cpu_count": 8,
            "ram_percent": 7.3,
            "ram_used_bytes": 600 * 1024 * 1024,       # 600 MiB
            "ram_total_bytes": 8 * 1024 * 1024 * 1024, # 8 GiB
            "storage_percent": 4.1,
            "storage_used_bytes": 3200000000,
            "storage_total_bytes": 96000000000,
        }
        with mock.patch.object(
            metrics_module, "collect_metrics", return_value=payload
        ):
            html = self.client.get("/console/").get_data(as_text=True)
        ram_card = html[html.index('data-metric="ram"') : html.index('data-metric="storage"')]
        self.assertIn("600.0 MiB / 8.0 GiB", ram_card)
        self.assertNotIn("KiB", ram_card)


class DestinationsWorkflowTests(unittest.TestCase):
    """GUI-4 Phase 1A: Destinations manager foundation (create/list/toggle/
    delete). No outgoing RTMP push exists yet, and the stored stream key never
    reaches the HTML, flash, list payloads, logs or URLs."""

    SECRET = "test-stream-key-NEVER-REAL-1234567890"
    PAYLOADS = {
        "snapshot": VALID_SNAPSHOT,
        "relay_list": [
            {
                "name": "news",
                "type": "remote-hls",
                "source": "https://source.example.com/live/index.m3u8",
                "active": "no",
                "enabled": "yes",
                "health": "STOPPED",
            }
        ],
        "destination_list": [
            {
                "name": "main-youtube",
                "display_name": "Main YouTube",
                "platform": "youtube",
                "server_url": "rtmps://a.rtmp.youtube.com/live2",
                "enabled": "no",
                "has_stream_key": "yes",
            }
        ],
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine(dict(self.PAYLOADS))
        self.app = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=self.engine
        )
        self.client = self.app.test_client()
        login(self.client)

    def tearDown(self):
        self._tmp.cleanup()

    def _create_data(self, **overrides):
        data = {
            "display": "Main YouTube",
            "platform": "youtube",
            "server_url": "rtmps://a.rtmp.youtube.com/live2",
            "stream_key": self.SECRET,
            "enabled": "on",
        }
        data.update(overrides)
        return data

    def _post_create(self, data):
        token = get_csrf(self.client, path="/console/destinations/create")
        return self.client.post(
            "/console/destinations/create", data=dict(data, csrf_token=token)
        )

    def _list_html(self):
        return self.client.get("/console/destinations").get_data(as_text=True)

    # ------------------------------------------------------------------
    # Auth + navigation
    # ------------------------------------------------------------------
    def test_01_destinations_requires_authentication(self):
        anon = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=self.engine
        ).test_client()
        for path in ("/console/destinations", "/console/destinations/create"):
            rv = anon.get(path, follow_redirects=False)
            self.assertEqual(rv.status_code, 302, path)
            self.assertIn("/console/login", rv.headers["Location"], path)

    def test_02_navigation_item_renders_and_is_active(self):
        html = self.client.get("/console/destinations").get_data(as_text=True)
        self.assertIn(">Destinations</a>", html)
        self.assertIn('href="/console/destinations"', html)
        active = re.search(
            r'<a href="([^"]+)"[^>]*aria-current="page"[^>]*>Destinations', html
        )
        self.assertIsNotNone(active)
        self.assertTrue(active.group(1).endswith("/console/destinations"))

    def test_03_destinations_nav_active_on_create_page(self):
        html = self.client.get("/console/destinations/create").get_data(as_text=True)
        self.assertRegex(
            html, r'href="[^"]*destinations[^"]*"[^>]*aria-current="page"'
        )


    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------
    def test_04_empty_state_and_populated_listing_render(self):
        empty = FakeEngine({"destination_list": []})
        app2 = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=empty
        )
        client2 = app2.test_client()
        login(client2)
        html = client2.get("/console/destinations").get_data(as_text=True)
        self.assertIn("No streaming destinations configured yet.", html)
        self.assertIn("+ Add Destination", html)

        html = self._list_html()
        self.assertIn("Main YouTube", html)
        self.assertIn("main-youtube", html)
        self.assertIn("YouTube", html)
        self.assertIn("rtmps://a.rtmp.youtube.com/live2", html)
        self.assertIn("&bull;", html)  # masked key indicator
        self.assertIn("Disabled", html)

    def test_05_list_never_contains_stream_key(self):
        html = self._list_html()
        self.assertNotIn(self.SECRET, html)
        # the safe per-destination presentation has no stream_key field
        record = app_module._present_destination(self.PAYLOADS["destination_list"][0])
        self.assertIsNotNone(record)
        self.assertNotIn("stream_key", record)

    def test_06_no_stream_key_copy_button(self):
        html = self._list_html()
        self.assertEqual(html.count('class="btn btn-sm copy-btn"'), 0)

    # ------------------------------------------------------------------
    # Create form + POST safety
    # ------------------------------------------------------------------
    def test_07_create_form_renders_fields(self):
        html = self.client.get("/console/destinations/create").get_data(as_text=True)
        self.assertIn("Create Destination", html)
        self.assertIn('name="display"', html)
        self.assertIn('name="platform"', html)
        for label in ("YouTube", "Facebook", "Twitch", "Rumble", "Instagram", "Custom RTMP"):
            self.assertIn(label, html)
        self.assertIn('name="server_url"', html)
        self.assertIn('name="stream_key"', html)
        self.assertIn('type="password"', html)
        self.assertIn('name="enabled"', html)

    def test_08_create_requires_csrf_and_is_post_only(self):
        rv = self.client.post(
            "/console/destinations/create", data=self._create_data()
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])
        # GET on a mutation action route is not allowed
        rv = self.client.get("/console/destinations/main-youtube/enable")
        self.assertEqual(rv.status_code, 405)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_09_anonymous_create_cannot_mutate(self):
        anon = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=self.engine
        ).test_client()
        token = get_csrf(anon)  # anonymous CSRF from the login page
        rv = anon.post(
            "/console/destinations/create",
            data=dict(self._create_data(), csrf_token=token),
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [])

    # ------------------------------------------------------------------
    # Create validation
    # ------------------------------------------------------------------
    def test_10_create_success_normalizes_friendly_name(self):
        rv = self._post_create(self._create_data())
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/destinations", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [
                (
                    "destination_create",
                    "main-youtube",
                    "Main YouTube",
                    "youtube",
                    "rtmps://a.rtmp.youtube.com/live2",
                    self.SECRET,
                    "yes",
                )
            ],
        )
        # flash mentions the friendly name only, never the key
        html = self.client.get("/console/destinations").get_data(as_text=True)
        self.assertIn("Main YouTube", html)
        self.assertNotIn(self.SECRET, html)

    def test_11_duplicate_destination_rejected_safely(self):
        args = (
            "main-youtube",
            "Main YouTube",
            "youtube",
            "rtmps://a.rtmp.youtube.com/live2",
            self.SECRET,
            "yes",
        )
        self.engine.mutation_payloads[("destination_create",) + args] = EngineError(
            "a destination named 'main-youtube' already exists",
            code="ALREADY_EXISTS",
        )
        rv = self._post_create(self._create_data())
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/destinations/create").get_data(as_text=True)
        self.assertIn("already exists", html)
        self.assertNotIn(self.SECRET, html)

    def test_12_supported_platform_labels_accepted(self):
        for platform in ("youtube", "facebook", "twitch", "rumble", "instagram", "tiktok", "custom"):
            self.engine.mutation_calls = []
            rv = self._post_create(
                self._create_data(platform=platform, display="Dest " + platform)
            )
            self.assertEqual(rv.status_code, 302, platform)
            self.assertEqual(len(self.engine.mutation_calls), 1, platform)

    def test_13_unknown_platform_rejected(self):
        rv = self._post_create(self._create_data(platform="youtube-extra"))
        self.assertEqual(rv.status_code, 302)
        html = self.client.get("/console/destinations/create").get_data(as_text=True)
        self.assertIn("Unsupported platform", html)
        self.assertEqual(self.engine.mutation_calls, [])


    # ------------------------------------------------------------------
    # URL / stream-key validation
    # ------------------------------------------------------------------
    def test_14_rtmp_and_rtmps_urls_accepted(self):
        for url in ("rtmp://ingest.example.com/live", "rtmps://live.example.com/x"):
            self.engine.mutation_calls = []
            rv = self._post_create(self._create_data(server_url=url))
            self.assertEqual(rv.status_code, 302, url)
            self.assertEqual(len(self.engine.mutation_calls), 1, url)

    def test_15_unsupported_or_malformed_urls_rejected(self):
        for url in (
            "http://ingest.example.com/live",
            "https://ingest.example.com/live",
            "rtsp://ingest.example.com/live",
            "file:///etc/passwd",
            "ftp://x",
            "/etc/passwd",
            "rtmp://has space.example.com/x",
            "rtmps://bad\nexample.com/x",
            "rtmps://ctl\x1b.example.com/x",
            "rtmps://q\"uote.example.com/x",
            "",
        ):
            self.engine.mutation_calls = []
            rv = self._post_create(self._create_data(server_url=url))
            self.assertEqual(rv.status_code, 302, url)
            self.assertEqual(self.engine.mutation_calls, [], url)
            html = self.client.get("/console/destinations/create").get_data(as_text=True)
            self.assertIn("Server URL", html)

    def test_16_empty_stream_key_rejected(self):
        rv = self._post_create(self._create_data(stream_key=""))
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        html = self.client.get("/console/destinations/create").get_data(as_text=True)
        self.assertIn("Stream key is required", html)

    def test_17_stream_key_control_characters_rejected(self):
        for key in ("line1\nline2", "ctl\x00key", "ctl\x1bkey", "tab\tkey"):
            self.engine.mutation_calls = []
            rv = self._post_create(self._create_data(stream_key=key))
            self.assertEqual(rv.status_code, 302, repr(key))
            self.assertEqual(self.engine.mutation_calls, [], repr(key))

    def test_18_stream_key_length_bound_enforced(self):
        self.engine.mutation_calls = []
        rv = self._post_create(self._create_data(stream_key="k" * 257))
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])
        # a 256-char printable key is accepted (opaque data)
        self.engine.mutation_calls = []
        rv = self._post_create(self._create_data(stream_key="k" * 256))
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(len(self.engine.mutation_calls), 1)

    # ------------------------------------------------------------------
    # Enable / disable / delete (POST + auth + CSRF, fixed target)
    # ------------------------------------------------------------------
    def test_19_enable_and_disable_require_post_auth_csrf(self):
        # missing CSRF -> 400, nothing executed
        rv = self.client.post("/console/destinations/main-youtube/enable")
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])
        # anonymous -> redirect, nothing executed
        anon = app_module.create_app(
            state_dir=make_state(self._tmp.name), engine=self.engine
        ).test_client()
        token = get_csrf(anon)
        rv = anon.post(
            "/console/destinations/main-youtube/enable", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [])
        # authenticated + CSRF succeeds and calls the fixed engine method
        token = get_csrf(self.client, path="/console/destinations")
        rv = self.client.post(
            "/console/destinations/main-youtube/enable", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [("destination_enable", "main-youtube")])
        self.engine.mutation_calls = []
        rv = self.client.post(
            "/console/destinations/main-youtube/disable", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [("destination_disable", "main-youtube")])

    def test_20_delete_requires_post_auth_csrf_and_validates_target(self):
        # GET cannot delete
        rv = self.client.get("/console/destinations/main-youtube/delete")
        self.assertEqual(rv.status_code, 405)
        self.assertEqual(self.engine.mutation_calls, [])
        # missing CSRF -> 400
        rv = self.client.post("/console/destinations/main-youtube/delete")
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])
        # invalid/traversal target never reaches the engine
        token = get_csrf(self.client, path="/console/destinations")
        for hostile in ("..%2Fetc%2Fdelete", "x%3Brm%20-rf%20%2F"):
            self.engine.mutation_calls = []
            rv = self.client.post(
                "/console/destinations/" + hostile, data={"csrf_token": token}
            )
            self.assertIn(rv.status_code, (302, 404), hostile)
            self.assertEqual(self.engine.mutation_calls, [], hostile)
        # authenticated + CSRF on a valid id deletes it
        rv = self.client.post(
            "/console/destinations/main-youtube/delete", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [("destination_delete", "main-youtube")])


    # ------------------------------------------------------------------
    # Engine-level safety + regression guards
    # ------------------------------------------------------------------
    def test_21_engine_rejects_arbitrary_path_for_delete(self):
        client = EngineClient(
            web_ctl=Path(self._tmp.name) / "web-ctl", bash="/bin/bash"
        )
        for bad in ("../x", "/etc/passwd", "x;rm", "x y", ".."):
            with self.assertRaises(EngineError, msg=bad):
                client._validate_mutation_values("destination_delete", (bad,))
        # destination_create argv carries NO secret: exactly five non-secret
        # values (id display platform url enabled), all validated pre-subprocess.
        with self.assertRaises(EngineError):
            client._validate_mutation_values(
                "destination_create",
                ("ok", "display\nname", "youtube", "rtmps://x", "yes"),
            )
        with self.assertRaises(EngineError):
            client._validate_mutation_values(
                "destination_create",
                ("../ok", "Name", "youtube", "rtmps://x", "yes"),
            )
        # the stream key itself is rejected at the method boundary before any
        # subprocess runs (never reaching argv or stdin)
        with self.assertRaises(EngineError):
            client.destination_create(
                "ok", "Name", "youtube", "rtmps://x", "key\nwith newline", "yes"
            )

    def test_22_engine_destination_validators_unit(self):
        for good in ("youtube", "facebook", "twitch", "rumble", "instagram", "tiktok", "custom"):
            self.assertTrue(valid_dest_platform(good), good)
        self.assertFalse(valid_dest_platform("youtube-extra"))
        self.assertFalse(valid_dest_platform("tiktok-live"))
        self.assertTrue(valid_dest_url("rtmp://host/live"))
        self.assertTrue(valid_dest_url("rtmps://host/app?key=1"))
        for bad in (
            "http://x", "https://x", "rtsp://x", "file:///etc/passwd",
            "rtmp://a b", "rtmps://a\nb", "", "rtmp://`x`",
        ):
            self.assertFalse(valid_dest_url(bad), bad)
        self.assertTrue(valid_dest_stream_key("abc-123_XYZ:key/with=chars"))
        self.assertFalse(valid_dest_stream_key(""))
        self.assertFalse(valid_dest_stream_key("a\nb"))
        self.assertFalse(valid_dest_stream_key("k" * 257))
        self.assertTrue(valid_dest_display("Main YouTube"))
        self.assertFalse(valid_dest_display("bad\nname"))

    def test_23_existing_navigation_and_links_untouched(self):
        html = self.client.get("/console/destinations").get_data(as_text=True)
        for label in ("Dashboard", "Streams", "Media Library", "Playlists", "Destinations"):
            self.assertIn(">" + label + "</a>", html)
        # streams page still renders copy controls unchanged
        streams = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn('class="btn btn-sm copy-btn"', streams)
        self.assertIn("http://example.com/hls/relay/news/index.m3u8", streams)


class Phase1bDestinationAttachmentTests(unittest.TestCase):
    """GUI-4 Phase 1B: attach existing destinations to a stream/playlist.

    IDs only ever cross the boundary; the stream key is never rendered.
    Saving attachments never invokes a lifecycle (start/stop/restart) method.
    """

    RELAY_DEST_LIST = [
        {
            "name": "main-youtube",
            "display_name": "Main YouTube",
            "platform": "youtube",
            "server_url": "rtmps://a.rtmp.youtube.com/live2",
            "enabled": "yes",
            "has_stream_key": "yes",
            "attached_count": "1",
        },
        {
            "name": "backup-fb",
            "display_name": "Backup Facebook",
            "platform": "facebook",
            "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
            "enabled": "no",
            "has_stream_key": "yes",
            "attached_count": "0",
        },
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [
                {
                    "name": "news",
                    "type": "remote-hls",
                    "source": "https://s.example/live.m3u8",
                    "active": "yes",
                    "enabled": "no",
                    "health": "HEALTHY",
                    "destination_count": "1",
                }
            ],
            "playlist_list": [
                {
                    "name": "promo",
                    "active": "no",
                    "enabled": "no",
                    "items": "3",
                    "health": "STOPPED",
                    "destination_count": "0",
                }
            ],
            "media_list": [],
            "destination_list": self.RELAY_DEST_LIST,
            ("relay_destinations_get", "news"): ["main-youtube"],
            ("playlist_destinations_get", "promo"): [],
            "playlist_cache_status": {
                "total_bytes": "0", "artifact_count": "0",
                "protected_bytes": "0", "protected_count": "0",
                "reclaimable_bytes": "0", "reclaimable_count": "0",
            },
        }
        self.engine = FakeEngine(payloads)
        self.app, _ = make_app(self._tmp.name, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def _csrf(self, path):
        m = CSRF_RE.search(self.client.get(path).get_data(as_text=True))
        assert m, "no csrf on %s" % path
        return m.group(1)

    def test_01_requires_authentication(self):
        anon = self.app.test_client()
        rv = anon.get("/console/streams/news/destinations")
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/login", rv.headers["Location"])

    def test_02_assignment_page_lists_destinations_without_key(self):
        html = self.client.get("/console/streams/news/destinations").get_data(as_text=True)
        self.assertIn("Main YouTube", html)
        self.assertIn("Backup Facebook", html)
        # attached destination is pre-checked
        self.assertRegex(html, r'value="main-youtube"[^>]*checked')
        self.assertNotRegex(html, r'value="backup-fb"[^>]*checked')
        # never a stream key or mask leakage of the secret itself
        self.assertNotIn("live2?", html)

    def test_03_save_attachments_calls_setter_not_lifecycle(self):
        token = self._csrf("/console/streams/news/destinations")
        rv = self.client.post(
            "/console/streams/news/destinations",
            data={"csrf_token": token,
                  "destinations": ["main-youtube", "backup-fb"]},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_destinations_set", "news", ("main-youtube", "backup-fb"))],
        )
        for call in self.engine.mutation_calls:
            self.assertNotIn(
                call[0],
                ("relay_start", "relay_stop", "relay_restart"),
            )

    def test_04_empty_selection_detaches_all(self):
        token = self._csrf("/console/streams/news/destinations")
        rv = self.client.post(
            "/console/streams/news/destinations",
            data={"csrf_token": token},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_destinations_set", "news", ())],
        )

    def test_05_invalid_destination_id_rejected_before_engine(self):
        token = self._csrf("/console/streams/news/destinations")
        rv = self.client.post(
            "/console/streams/news/destinations",
            data={"csrf_token": token, "destinations": "../etc/passwd"},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_06_save_requires_csrf(self):
        rv = self.client.post(
            "/console/streams/news/destinations",
            data={"destinations": "main-youtube"},
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_07_playlist_assignment_page_and_save(self):
        html = self.client.get("/console/playlists/promo/destinations").get_data(as_text=True)
        self.assertIn("Main YouTube", html)
        token = self._csrf("/console/playlists/promo/destinations")
        rv = self.client.post(
            "/console/playlists/promo/destinations",
            data={"csrf_token": token, "destinations": "main-youtube"},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/playlists", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("playlist_destinations_set", "promo", ("main-youtube",))],
        )

    def test_08_streams_and_playlists_show_destinations_action(self):
        streams = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("/console/streams/news/destinations", streams)
        self.assertIn("Destinations (1)", streams)
        playlists = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("/console/playlists/promo/destinations", playlists)
        self.assertIn("Destinations (0)", playlists)

    def test_09_destinations_list_guards_delete_when_attached(self):
        html = self.client.get("/console/destinations").get_data(as_text=True)
        # attached destination: disabled Delete button, no delete form action
        self.assertIn("1 target", html)
        self.assertRegex(html, r"btn-danger[^>]*disabled")
        # unattached destination still has a working delete form
        self.assertIn(
            '/console/destinations/backup-fb/delete', html
        )

    def test_10_delete_attached_destination_reports_safe_message(self):
        self.engine.mutation_payloads[("destination_delete", "main-youtube")] = EngineError(
            "attached", code="DESTINATION_ATTACHED"
        )
        token = self._csrf("/console/destinations")
        rv = self.client.post(
            "/console/destinations/main-youtube/delete",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        html = rv.get_data(as_text=True)
        self.assertIn("Detach it before deleting", html)

    def test_11_assignment_nav_keeps_parent_section_active(self):
        html = self.client.get("/console/streams/news/destinations").get_data(as_text=True)
        self.assertRegex(html, r'href="/console/streams"[^>]*nav-active')
        html = self.client.get("/console/playlists/promo/destinations").get_data(as_text=True)
        self.assertRegex(html, r'href="/console/playlists"[^>]*nav-active')

    def test_12_invalid_target_name_redirects_safely(self):
        rv = self.client.get("/console/streams/Bad_UPPER/destinations")
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])


class EnginePhase1bValidationTests(unittest.TestCase):
    """The EngineClient validates attachment argv fully before any subprocess."""

    def _client(self):
        return EngineClient(web_ctl=Path(__file__), bash="/bin/sh")

    def test_set_rejects_bad_target_name(self):
        c = self._client()
        with self.assertRaises(EngineError):
            c.relay_destinations_set("../evil", ["a"])

    def test_set_rejects_bad_destination_id(self):
        c = self._client()
        with self.assertRaises(EngineError) as ctx:
            c.relay_destinations_set("news", ["ok", "../bad"])
        self.assertEqual(ctx.exception.code, "INVALID_DESTINATION")

    def test_set_rejects_duplicate_ids(self):
        c = self._client()
        with self.assertRaises(EngineError) as ctx:
            c.playlist_destinations_set("promo", ["a", "a"])
        self.assertEqual(ctx.exception.code, "DUPLICATE_DESTINATION")

    def test_set_rejects_over_limit(self):
        from webapp.engine import MAX_TARGET_DESTINATIONS
        c = self._client()
        ids = ["d%02d" % i for i in range(MAX_TARGET_DESTINATIONS + 1)]
        with self.assertRaises(EngineError) as ctx:
            c.relay_destinations_set("news", ids)
        self.assertEqual(ctx.exception.code, "TOO_MANY_DESTINATIONS")

    def test_get_rejects_bad_name(self):
        c = self._client()
        with self.assertRaises(EngineError):
            c.relay_destinations_get("../x")

    def test_csv_normalization_drops_blanks_and_spaces(self):
        from webapp.engine import _ids_to_csv
        self.assertEqual(_ids_to_csv([" a ", "", "b"]), "a,b")
        self.assertEqual(_ids_to_csv("a, b ,,c"), "a,b,c")
        self.assertEqual(_ids_to_csv(None), "")


class YouTubeLiveSupportTests(unittest.TestCase):
    """GUI-7A: public YouTube Live sources surface as their own type in the UI.

    Classification and resolution are engine-side (bash) and covered by the
    web-ctl functional tests; here we confirm the console renders the new
    'youtube' type and the yt-dlp availability hint without regressions.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [
                {"name": "yt", "type": "youtube",
                 "source": "https://www.youtube.com/watch?v=LIVE",
                 "active": "no", "enabled": "no", "health": "STOPPED",
                 "destination_count": "0"},
            ],
            "playlist_list": [],
            "media_list": [],
            "destination_list": [],
            "playlist_cache_status": {
                "total_bytes": "0", "artifact_count": "0", "protected_bytes": "0",
                "protected_count": "0", "reclaimable_bytes": "0",
                "reclaimable_count": "0",
            },
        }
        self.engine = FakeEngine(payloads)
        self.app, _ = make_app(self._tmp.name, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def test_01_streams_list_labels_youtube_type(self):
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("YouTube Live", html)
        # a youtube stream is URL-backed, so it gets the Edit Source control
        self.assertIn("/console/streams/yt/edit-source", html)

    def test_02_create_page_documents_youtube(self):
        html = self.client.get("/console/streams/create").get_data(as_text=True)
        self.assertIn("YouTube Live", html)
        self.assertIn("yt-dlp", html)

    def test_03_dashboard_shows_ytdlp_status(self):
        html = self.client.get("/console/").get_data(as_text=True)
        self.assertIn("yt-dlp", html)

    def test_04_type_label_map_has_youtube(self):
        self.assertEqual(app_module.TYPE_LABELS.get("youtube"), "YouTube Live")


class SafeDeleteWorkflowTests(unittest.TestCase):
    """GUI-5A: safe delete for streams, playlists and media.

    The route always calls the FIXED engine delete method for that resource,
    never a lifecycle or generic method; the engine enforces the real "stopped"
    / "unreferenced" guard. Every mutation is POST + CSRF + auth + PRG.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [
                {"name": "news", "type": "remote-hls", "source": "https://s/x.m3u8",
                 "active": "no", "enabled": "no", "health": "STOPPED",
                 "destination_count": "0"},
                {"name": "live", "type": "remote-hls", "source": "https://s/y.m3u8",
                 "active": "yes", "enabled": "no", "health": "HEALTHY",
                 "destination_count": "0"},
            ],
            "playlist_list": [
                {"name": "promo", "active": "no", "enabled": "no", "items": "2",
                 "health": "STOPPED", "destination_count": "0"},
            ],
            "media_list": [
                {"name": "intro.mp4", "extension": "mp4", "size": "1024"},
            ],
            "destination_list": [],
            "playlist_cache_status": {
                "total_bytes": "0", "artifact_count": "0", "protected_bytes": "0",
                "protected_count": "0", "reclaimable_bytes": "0",
                "reclaimable_count": "0",
            },
        }
        self.engine = FakeEngine(self.payloads)
        self.app, _ = make_app(self._tmp.name, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def _csrf(self, path="/console/streams"):
        m = CSRF_RE.search(self.client.get(path).get_data(as_text=True))
        assert m, "no csrf on %s" % path
        return m.group(1)

    def test_01_delete_is_post_only(self):
        rv = self.client.get("/console/streams/news/delete")
        self.assertEqual(rv.status_code, 405)

    def test_02_delete_requires_csrf(self):
        rv = self.client.post("/console/streams/news/delete", data={})
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_03_delete_requires_auth(self):
        anon = self.app.test_client()
        rv = anon.post("/console/streams/news/delete", data={})
        self.assertIn(rv.status_code, (302, 400))
        self.assertEqual(self.engine.mutation_calls, [])

    def test_04_relay_delete_calls_fixed_method_and_prg(self):
        token = self._csrf()
        rv = self.client.post(
            "/console/streams/news/delete", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [("relay_delete", "news")])

    def test_05_relay_delete_not_stopped_message(self):
        self.engine.mutation_payloads[("relay_delete", "live")] = EngineError(
            "not stopped", code="NOT_STOPPED"
        )
        token = self._csrf()
        rv = self.client.post(
            "/console/streams/live/delete", data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("not stopped", rv.get_data(as_text=True).lower())

    def test_06_running_relay_delete_button_disabled_in_ui(self):
        html = self.client.get("/console/streams").get_data(as_text=True)
        # the running relay 'live' row has a disabled Delete button
        live_row = html.split("<td>live</td>", 1)[1].split("</tr>", 1)[0]
        self.assertRegex(live_row, r"(?s)btn-danger[^>]*disabled")
        # the stopped relay 'news' row's Delete button is NOT disabled
        news_row = html.split("<td>news</td>", 1)[1].split("</tr>", 1)[0]
        self.assertNotRegex(news_row, r"(?s)btn-danger[^>]*disabled")

    def test_07_playlist_delete_calls_fixed_method(self):
        token = self._csrf("/console/playlists")
        rv = self.client.post(
            "/console/playlists/promo/delete", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [("playlist_delete", "promo")])

    def test_08_media_delete_calls_fixed_method(self):
        token = self._csrf("/console/media")
        rv = self.client.post(
            "/console/media/intro.mp4/delete", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/media", rv.headers["Location"])
        self.assertEqual(self.engine.mutation_calls, [("media_delete", "intro.mp4")])

    def test_09_media_delete_referenced_message(self):
        self.engine.mutation_payloads[("media_delete", "intro.mp4")] = EngineError(
            "referenced", code="MEDIA_REFERENCED"
        )
        token = self._csrf("/console/media")
        rv = self.client.post(
            "/console/media/intro.mp4/delete", data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertIn("used by a stream or playlist", rv.get_data(as_text=True))

    def test_10_media_delete_rejects_arbitrary_path(self):
        token = self._csrf("/console/media")
        for bad in ("..%2Fetc%2Fpasswd", "sub%2Ffile.mp4"):
            self.engine.mutation_calls = []
            rv = self.client.post(
                "/console/media/%s/delete" % bad, data={"csrf_token": token}
            )
            self.assertIn(rv.status_code, (302, 404))
            self.assertEqual(self.engine.mutation_calls, [])

    def test_11_relay_delete_rejects_bad_name_before_engine(self):
        token = self._csrf()
        rv = self.client.post(
            "/console/streams/Bad_UP/delete", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_12_delete_buttons_use_danger_class(self):
        for path in ("/console/streams", "/console/playlists", "/console/media"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("btn-danger", html, path)
            self.assertIn("confirm(", html, path)


class EditStreamSourceTests(unittest.TestCase):
    """GUI-6A: edit the source URL of a stopped, URL-backed stream."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [
                {"name": "news", "type": "remote-hls",
                 "source": "https://old.example/live.m3u8",
                 "active": "no", "enabled": "no", "health": "STOPPED",
                 "destination_count": "1"},
                {"name": "live", "type": "rtmp", "source": "rtmp://x/app",
                 "active": "yes", "enabled": "no", "health": "HEALTHY",
                 "destination_count": "0"},
                {"name": "loopvid", "type": "local-file",
                 "source": "/var/lib/bluestream/media/intro.mp4",
                 "active": "no", "enabled": "no", "health": "STOPPED",
                 "destination_count": "0"},
            ],
            "playlist_list": [],
            "media_list": [],
            "destination_list": [],
            "playlist_cache_status": {
                "total_bytes": "0", "artifact_count": "0", "protected_bytes": "0",
                "protected_count": "0", "reclaimable_bytes": "0",
                "reclaimable_count": "0",
            },
        }
        self.engine = FakeEngine(self.payloads)
        self.app, _ = make_app(self._tmp.name, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def _csrf(self, path):
        m = CSRF_RE.search(self.client.get(path).get_data(as_text=True))
        assert m, "no csrf on %s" % path
        return m.group(1)

    def test_01_edit_page_shows_form_for_stopped_url_stream(self):
        html = self.client.get("/console/streams/news/edit-source").get_data(as_text=True)
        self.assertIn("New source URL", html)
        self.assertIn("https://old.example/live.m3u8", html)  # shown as context
        # the input is NOT pre-filled with the (possibly credentialed) URL
        self.assertNotRegex(html, r'name="url"[^>]*value="https://old')

    def test_02_running_stream_shows_no_form(self):
        html = self.client.get("/console/streams/live/edit-source").get_data(as_text=True)
        self.assertNotIn("New source URL", html)
        self.assertIn("running", html.lower())

    def test_03_media_backed_stream_has_no_editor(self):
        html = self.client.get("/console/streams/loopvid/edit-source").get_data(as_text=True)
        self.assertIn("managed media", html.lower())
        self.assertNotIn("New source URL", html)

    def test_04_streams_list_shows_edit_source_only_for_url_streams(self):
        html = self.client.get("/console/streams").get_data(as_text=True)
        self.assertIn("/console/streams/news/edit-source", html)
        self.assertNotIn("/console/streams/loopvid/edit-source", html)

    def test_05_save_calls_relay_set_source_and_prg(self):
        token = self._csrf("/console/streams/news/edit-source")
        rv = self.client.post(
            "/console/streams/news/edit-source",
            data={"csrf_token": token, "url": "https://new.example/live.m3u8"},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/streams", rv.headers["Location"])
        self.assertEqual(
            self.engine.mutation_calls,
            [("relay_set_source", "news", "https://new.example/live.m3u8")],
        )

    def test_06_save_requires_csrf(self):
        rv = self.client.post(
            "/console/streams/news/edit-source",
            data={"url": "https://new.example/live.m3u8"},
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_07_invalid_url_rejected_before_engine(self):
        token = self._csrf("/console/streams/news/edit-source")
        for bad in ("file:///etc/passwd", "not a url", "https://x/`whoami`.m3u8"):
            self.engine.mutation_calls = []
            rv = self.client.post(
                "/console/streams/news/edit-source",
                data={"csrf_token": token, "url": bad},
            )
            self.assertEqual(rv.status_code, 302)
            self.assertEqual(self.engine.mutation_calls, [], bad)

    def test_08_engine_not_stopped_reported(self):
        self.engine.mutation_payloads[
            ("relay_set_source", "news", "https://new.example/live.m3u8")
        ] = EngineError("running", code="NOT_STOPPED")
        token = self._csrf("/console/streams/news/edit-source")
        rv = self.client.post(
            "/console/streams/news/edit-source",
            data={"csrf_token": token, "url": "https://new.example/live.m3u8"},
            follow_redirects=True,
        )
        self.assertIn("Stop the stream", rv.get_data(as_text=True))

    def test_09_engine_media_backed_reported(self):
        self.engine.mutation_payloads[
            ("relay_set_source", "news", "https://new.example/live.m3u8")
        ] = EngineError("media", code="MEDIA_BACKED")
        token = self._csrf("/console/streams/news/edit-source")
        rv = self.client.post(
            "/console/streams/news/edit-source",
            data={"csrf_token": token, "url": "https://new.example/live.m3u8"},
            follow_redirects=True,
        )
        self.assertIn("managed media", rv.get_data(as_text=True).lower())


class PlaylistScheduleTests(unittest.TestCase):
    """GUI-8A: one-time playlist start scheduling with timezone normalization."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.payloads = {
            "snapshot": VALID_SNAPSHOT,
            "relay_list": [],
            "playlist_list": [
                {"name": "promo", "active": "no", "enabled": "no", "items": "3",
                 "health": "STOPPED", "destination_count": "0",
                 "scheduled": "yes", "scheduled_at": "4102400000",
                 "scheduled_at_iso": "2099-12-31T00:00:00Z"},
            ],
            "media_list": [],
            "destination_list": [],
            "playlist_cache_status": {
                "total_bytes": "0", "artifact_count": "0", "protected_bytes": "0",
                "protected_count": "0", "reclaimable_bytes": "0",
                "reclaimable_count": "0",
            },
            ("playlist_schedule_get", "promo"): {
                "scheduled": "no", "start_at": "", "start_at_iso": "",
            },
        }
        self.engine = FakeEngine(self.payloads)
        self.app, _ = make_app(self._tmp.name, engine=self.engine)
        self.client = self.app.test_client()
        login(self.client)

    def _csrf(self, path):
        m = CSRF_RE.search(self.client.get(path).get_data(as_text=True))
        assert m, "no csrf on %s" % path
        return m.group(1)

    def _future_iso(self, **kw):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()

    def test_01_schedule_page_requires_auth(self):
        rv = self.app.test_client().get("/console/playlists/promo/schedule")
        self.assertEqual(rv.status_code, 302)

    def test_02_schedule_page_renders_form(self):
        html = self.client.get("/console/playlists/promo/schedule").get_data(as_text=True)
        self.assertIn('type="datetime-local"', html)
        self.assertIn('name="iso"', html)
        self.assertIn("time zone", html.lower())

    def test_03_valid_future_time_calls_engine_with_utc_epoch(self):
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": self._future_iso(days=2)},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/console/playlists", rv.headers["Location"])
        self.assertEqual(len(self.engine.mutation_calls), 1)
        op, name, epoch, iso = self.engine.mutation_calls[0]
        self.assertEqual((op, name), ("playlist_schedule_set", "promo"))
        self.assertTrue(epoch.isdigit())
        self.assertGreater(int(epoch), 1577836800)
        self.assertTrue(iso.endswith("Z"))

    def test_04_past_time_rejected_before_engine(self):
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": self._future_iso(days=-1)},
            follow_redirects=True,
        )
        self.assertIn("future", rv.get_data(as_text=True).lower())
        self.assertEqual(self.engine.mutation_calls, [])

    def test_05_naive_time_without_offset_rejected(self):
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": "2099-01-01T12:00:00"},
            follow_redirects=True,
        )
        self.assertIn("timezone", rv.get_data(as_text=True).lower())
        self.assertEqual(self.engine.mutation_calls, [])

    def test_06_garbage_time_rejected(self):
        token = self._csrf("/console/playlists/promo/schedule")
        for bad in ("not-a-date", "'; rm -rf /", "2099-13-45T99:99:99Z"):
            self.engine.mutation_calls = []
            rv = self.client.post(
                "/console/playlists/promo/schedule",
                data={"csrf_token": token, "iso": bad},
            )
            self.assertEqual(rv.status_code, 302)
            self.assertEqual(self.engine.mutation_calls, [], bad)

    def test_07_far_future_beyond_horizon_rejected(self):
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": self._future_iso(days=900)},
            follow_redirects=True,
        )
        self.assertIn("days", rv.get_data(as_text=True).lower())
        self.assertEqual(self.engine.mutation_calls, [])

    def test_08_schedule_requires_csrf(self):
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"iso": self._future_iso(days=2)},
        )
        self.assertEqual(rv.status_code, 400)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_09_cancel_calls_clear(self):
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule/cancel", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls, [("playlist_schedule_clear", "promo")])

    def test_10_engine_past_error_surfaced(self):
        self.engine.mutation_payloads[("playlist_schedule_set",)] = EngineError(
            "past", code="SCHEDULE_IN_PAST"
        )
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": self._future_iso(days=2)},
            follow_redirects=True,
        )
        self.assertIn("past", rv.get_data(as_text=True).lower())

    def test_11_playlists_list_shows_schedule_link_and_badge(self):
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("/console/playlists/promo/schedule", html)
        self.assertIn("2099-12-31T00:00:00Z", html)  # <time> value, JS localizes

    def test_12_existing_schedule_shows_cancel(self):
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": "4102400000",
            "start_at_iso": "2099-12-31T00:00:00Z", "state": "pending",
        }
        html = self.client.get("/console/playlists/promo/schedule").get_data(as_text=True)
        self.assertIn("/console/playlists/promo/schedule/cancel", html)
        self.assertIn("Cancel Schedule", html)

    # -- GUI-8A missed-time safety (Persistent=false) --------------------
    def _past_epoch(self, minutes=90):
        import time
        return str(int(time.time()) - minutes * 60)

    def test_13_missed_schedule_reported_as_missed_not_pending(self):
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": self._past_epoch(),
            "start_at_iso": "2020-01-01T00:00:00Z", "state": "missed",
        }
        html = self.client.get("/console/playlists/promo/schedule").get_data(as_text=True)
        self.assertIn("Missed", html)
        # a missed schedule is NOT presented as still waiting to run
        self.assertNotIn(">Pending<", html)
        self.assertIn("never run late", html.lower())
        # Change + Cancel remain available
        self.assertIn("/console/playlists/promo/schedule/cancel", html)
        self.assertIn('type="datetime-local"', html)

    def test_14_missed_derived_from_past_epoch_when_state_absent(self):
        # engine did not send a state field -> the app derives it from the epoch
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": self._past_epoch(),
            "start_at_iso": "2020-01-01T00:00:00Z",
        }
        html = self.client.get("/console/playlists/promo/schedule").get_data(as_text=True)
        self.assertIn("Missed", html)
        self.assertNotIn(">Pending<", html)

    def test_15_future_schedule_reported_as_pending(self):
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": "4102400000",
            "start_at_iso": "2099-12-31T00:00:00Z", "state": "pending",
        }
        html = self.client.get("/console/playlists/promo/schedule").get_data(as_text=True)
        self.assertIn("Pending", html)
        self.assertNotIn("Missed", html)

    def test_16_playlists_list_shows_missed_badge_not_time(self):
        self.payloads["playlist_list"] = [
            {"name": "promo", "active": "no", "enabled": "no", "items": "3",
             "health": "STOPPED", "destination_count": "0",
             "scheduled": "yes", "scheduled_at": self._past_epoch(),
             "scheduled_at_iso": "2020-01-01T00:00:00Z",
             "schedule_state": "missed"},
        ]
        html = self.client.get("/console/playlists").get_data(as_text=True)
        self.assertIn("Missed", html)
        self.assertNotIn("2020-01-01T00:00:00Z", html)

    def test_17_reschedule_still_works_when_missed(self):
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": self._past_epoch(),
            "start_at_iso": "2020-01-01T00:00:00Z", "state": "missed",
        }
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule",
            data={"csrf_token": token, "iso": self._future_iso(days=3)},
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(len(self.engine.mutation_calls), 1)
        self.assertEqual(self.engine.mutation_calls[0][:2],
                         ("playlist_schedule_set", "promo"))

    def test_18_cancel_still_works_when_missed(self):
        self.payloads[("playlist_schedule_get", "promo")] = {
            "scheduled": "yes", "start_at": self._past_epoch(),
            "start_at_iso": "2020-01-01T00:00:00Z", "state": "missed",
        }
        token = self._csrf("/console/playlists/promo/schedule")
        rv = self.client.post(
            "/console/playlists/promo/schedule/cancel", data={"csrf_token": token}
        )
        self.assertEqual(rv.status_code, 302)
        self.assertEqual(self.engine.mutation_calls,
                         [("playlist_schedule_clear", "promo")])


class EngineScheduleValidationTests(unittest.TestCase):
    def _client(self):
        return EngineClient(web_ctl=Path(__file__), bash="/bin/sh")

    def test_set_rejects_non_numeric_epoch(self):
        with self.assertRaises(EngineError) as ctx:
            self._client().playlist_schedule_set("promo", "not-a-number", "2099-01-01T00:00:00Z")
        self.assertEqual(ctx.exception.code, "INVALID_SCHEDULE")

    def test_set_rejects_out_of_range_epoch(self):
        with self.assertRaises(EngineError):
            self._client().playlist_schedule_set("promo", "10", "2099-01-01T00:00:00Z")

    def test_set_rejects_unsafe_iso(self):
        with self.assertRaises(EngineError) as ctx:
            self._client().playlist_schedule_set("promo", "4000000000", "2099-01-01 $(id)")
        self.assertEqual(ctx.exception.code, "INVALID_SCHEDULE")

    def test_set_rejects_bad_name(self):
        with self.assertRaises(EngineError):
            self._client().playlist_schedule_set("../evil", "4000000000", "2099-01-01T00:00:00Z")

    def test_clear_rejects_bad_name(self):
        with self.assertRaises(EngineError):
            self._client().playlist_schedule_clear("../evil")


class EngineSafeDeleteValidationTests(unittest.TestCase):
    def _client(self):
        return EngineClient(web_ctl=Path(__file__), bash="/bin/sh")

    def test_relay_delete_rejects_bad_name(self):
        with self.assertRaises(EngineError) as ctx:
            self._client().relay_delete("../evil")
        self.assertEqual(ctx.exception.code, "INVALID_NAME")

    def test_media_delete_rejects_bad_name(self):
        with self.assertRaises(EngineError) as ctx:
            self._client().media_delete("../evil")
        self.assertEqual(ctx.exception.code, "INVALID_MEDIA")

    def test_media_delete_rejects_separator(self):
        with self.assertRaises(EngineError):
            self._client().media_delete("sub/dir.mp4")


if __name__ == "__main__":
    unittest.main()
