"""Static integration tests for GUI-1A.3B (installer/nginx/systemd/sudoers).

These are source-level assertions for the deployment plumbing. They run
locally without an Ubuntu host; live validation (visudo, real sudo boundary,
Gunicorn/service start, nginx -t/reload) is recorded as VPS-pending.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


class InstallerStaticTests(unittest.TestCase):
    """install.sh + lib/webconsole.sh wiring."""

    def test_01_references_required_web_packages(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("python3 python3-flask gunicorn sudo", wc)
        inst = repo_text("install.sh")
        self.assertIn("web_packages", inst)

    def test_02_verifies_usr_bin_gunicorn(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('BLUESTREAM_WEB_GUNICORN="/usr/bin/gunicorn"', wc)
        self.assertIn('! -x "$BLUESTREAM_WEB_GUNICORN"', wc)
        # must fail closed, not search PATH
        self.assertIn("will NOT search PATH", wc)

    def test_03_creates_bluestream_web_idempotently(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("bluestream-web", wc)
        self.assertIn("useradd --system", wc)
        self.assertIn("--shell /usr/sbin/nologin", wc)
        self.assertIn('id "$BLUESTREAM_WEB_USER"', wc)  # idempotent guard
        # must never be granted privileged groups
        self.assertIn("sudo root www-data", wc)
        inst = repo_text("install.sh")
        self.assertIn("web_create_user", inst)

    def test_04_production_state_path(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('BLUESTREAM_WEB_STATE_DIR="/var/lib/bluestream/web"', wc)
        app = repo_text("webapp/app.py")
        self.assertIn('PRODUCTION_STATE_DIR = "/var/lib/bluestream/web"', app)
        wsgi = repo_text("webapp/wsgi.py")
        self.assertIn("from webapp.app import PRODUCTION_STATE_DIR, create_app", wsgi)

    def test_05_admin_credentials_preserved_on_reinstall(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('if [ -f "$BLUESTREAM_WEB_STATE_DIR/admin.json" ]', wc)
        self.assertIn("preserved", wc)
        self.assertNotIn("--web-admin-password", wc)  # never an argv flag

    def test_06_secret_key_preserved_on_reinstall(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("secret_key", wc)
        self.assertIn("preserved", wc)

    def test_07_webctl_installed_root_0700(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('install -o root -g root -m 0700 "$BS_ROOT/web-ctl"', wc)
        self.assertIn("want root:root 0700", wc)

    def test_08_sudoers_install_mode_0440(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('install -o root -g root -m 0440', wc)
        self.assertIn("root:root 0440", wc)

    def test_09_sudoers_validated_with_visudo(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("visudo -cf", wc)
        # staged first, only kept if validation succeeds
        self.assertIn(".staged", wc)
        self.assertIn("mv -f", wc)

    def test_10_systemd_unit_install_path(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('BLUESTREAM_WEB_UNIT="/etc/systemd/system/bluestream-web.service"', wc)
        self.assertIn('"$BS_ROOT/config/systemd/bluestream-web.service" "$BLUESTREAM_WEB_UNIT"', wc)

    def test_18_no_ufw_8080_opening(self):
        for rel in ("install.sh", "lib/webconsole.sh", "lib/firewall.sh"):
            text = repo_text(rel)
            self.assertNotIn("ufw allow 8080", text)
            self.assertNotIn("8080/tcp", text)


class NginxTemplateTests(unittest.TestCase):
    """console-location.conf + site/https templates."""

    def setUp(self):
        self.console = repo_text("config/nginx/console-location.conf")

    def test_11_binds_only_loopback_8080(self):
        self.assertIn("proxy_pass http://127.0.0.1:8080;", self.console)
        self.assertNotIn("0.0.0.0", self.console)
        unit = repo_text("config/systemd/bluestream-web.service")
        self.assertIn("--bind 127.0.0.1:8080", unit)
        self.assertNotIn("0.0.0.0", unit)

    def test_12_proxy_points_only_to_loopback(self):
        self.assertIn("proxy_pass http://127.0.0.1:8080;", self.console)
        rest = self.console.replace("proxy_pass http://127.0.0.1:8080;", "")
        self.assertNotIn("proxy_pass http://", rest)

    def test_13_exact_forwarding_headers_present(self):
        for header in (
            "proxy_set_header Host              $host;",
            "proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;",
            "proxy_set_header X-Forwarded-Proto $scheme;",
            "proxy_set_header X-Forwarded-Host  $host;",
        ):
            self.assertIn(header, self.console)

    def test_14_raw_forwarded_variants_absent(self):
        for bad in ("$http_x_forwarded_for", "$http_x_forwarded_proto", "$http_x_forwarded_host"):
            self.assertNotIn(bad, self.console)

    def test_15_hls_unchanged(self):
        site = repo_text("config/nginx/bluestream-site.conf.template")
        self.assertIn("hls-location.conf", site)
        self.assertNotIn("/hls/", self.console)

    def test_16_player_unchanged(self):
        site = repo_text("config/nginx/bluestream-site.conf.template")
        self.assertIn("player-location.conf", site)
        self.assertNotIn("/player/", self.console)

    def test_17_rtmp_config_unchanged(self):
        rtmp = repo_text("config/nginx/rtmp.conf.template")
        self.assertIn("rtmp {", rtmp)
        self.assertIn("listen __RTMP_BIND__:__RTMP_PORT__;", rtmp)
        self.assertIn("allow publish 127.0.0.1;", rtmp)
        self.assertIn("application bluestream-relay", rtmp)
        self.assertIn("application bluestream-playlist", rtmp)

    def test_console_redirect_clean(self):
        self.assertIn("location = /console", self.console)
        self.assertIn("return 301 /console/;", self.console)

    def test_no_cache_for_console(self):
        self.assertIn("no-store", self.console)

    def test_http_block_uses_console_http_snippet(self):
        site = repo_text("config/nginx/bluestream-site.conf.template")
        self.assertIn("console-http.conf", site)
        self.assertNotIn("console-location.conf", site)

    def test_https_console_redirect_snippet(self):
        redir = repo_text("config/nginx/console-redirect-http.conf")
        self.assertIn("return 301 https://$host$request_uri;", redir)
        self.assertIn("return 301 https://$host/console/;", redir)
        self.assertNotIn("proxy_pass", redir)

    def test_nginx_gen_selects_redirect_when_ssl(self):
        nginx = repo_text("lib/nginx.sh")
        self.assertIn("console-redirect-http.conf", nginx)
        self.assertIn('if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]', nginx)

    def test_web_conf_sync_hook_in_nginx_pipeline(self):
        nginx = repo_text("lib/nginx.sh")
        self.assertIn("nginx_sync_web_conf", nginx)
        self.assertIn("secure_cookie=", nginx)

    def test_state_ownership_is_read_only_for_web_user(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("chown root:\"$BLUESTREAM_WEB_GROUP\"", wc)
        self.assertIn("chmod 0750 \"$BLUESTREAM_WEB_STATE_DIR\"", wc)
        self.assertIn("chmod 0640 \"$BLUESTREAM_WEB_STATE_DIR/$_f\"", wc)
        # the running web user must never be the owner of credential files
        self.assertNotIn("chown \"$BLUESTREAM_WEB_USER\":\"$BLUESTREAM_WEB_GROUP\"", wc)
        # /run/sudo is prepared for the sudo timestamp path
        self.assertIn("/run/sudo", wc)

    def test_secret_key_is_pre_created_by_installer(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn("web_ensure_secret_key", wc)
        self.assertIn("ensure_secret_key", wc)


class UninstallTests(unittest.TestCase):
    """uninstall.sh preserve-vs-purge behavior for web state."""

    def test_19_20_web_state_follows_purge_gate(self):
        text = repo_text("uninstall.sh")
        self.assertIn("web_uninstall", text)
        rm_line = "rm -rf /etc/bluestream /var/lib/bluestream /var/www/bluestream"
        self.assertIn(rm_line, text)
        # the removal must be gated behind the purge/confirm logic, not top-level
        self.assertLess(text.index('if [ "$PURGE" = "1" ]'), text.index(rm_line))
        # web state lives under /var/lib/bluestream so it follows the same gate
        self.assertIn("/var/lib/bluestream", text)


class VpsFixTests(unittest.TestCase):
    """GUI deployment VPS bug fixes: parent traversal + Host-header check."""

    def test_01_parent_var_dir_mode_is_0751(self):
        inst = repo_text("install.sh")
        self.assertIn('chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_VAR_DIR"', inst)
        self.assertIn('chmod 0751 "$BLUESTREAM_VAR_DIR"', inst)
        self.assertNotIn('chmod 0750 "$BLUESTREAM_VAR_DIR"', inst)

    def test_02_parent_owner_group_unchanged(self):
        inst = repo_text("install.sh")
        self.assertIn('chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_VAR_DIR"', inst)

    def test_03_media_run_backups_modes_unchanged(self):
        inst = repo_text("install.sh")
        self.assertIn('chmod 0750 "$BLUESTREAM_MEDIA_DIR"', inst)
        self.assertIn('chmod 0750 "$BLUESTREAM_RUN_DIR"', inst)
        self.assertIn('chmod 0700 "$BLUESTREAM_BACKUP_DIR"', inst)

    def test_04_web_state_dir_remains_0750(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('chmod 0750 "$BLUESTREAM_WEB_STATE_DIR"', wc)

    def test_05_no_web_user_membership_in_relay_group(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertNotIn("bluestream-relay", wc)

    def test_06_diagnostics_sends_domain_host_header(self):
        d = repo_text("lib/diagnostics.sh")
        self.assertIn('-H "Host: $BLUESTREAM_DOMAIN"', d)
        self.assertIn('bs_valid_domain "$BLUESTREAM_DOMAIN"', d)
        self.assertIn("http://127.0.0.1/console/login", d)
        self.assertIn('= "200"', d)

    def test_07_selftest_sends_domain_host_header(self):
        s = repo_text("lib/selftest.sh")
        self.assertIn('-H "Host: $BLUESTREAM_DOMAIN"', s)
        self.assertIn('bs_valid_domain "$BLUESTREAM_DOMAIN"', s)
        self.assertIn("http://127.0.0.1/console/login", s)
        self.assertIn('= "200"', s)

    def test_08_no_hardcoded_customer_domain(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            self.assertNotIn("stream.therealworldboosts.com", repo_text(rel))

    def test_09_both_continue_using_loopback(self):
        d = repo_text("lib/diagnostics.sh")
        s = repo_text("lib/selftest.sh")
        self.assertIn("http://127.0.0.1/console/login", d)
        self.assertIn("http://127.0.0.1/console/login", s)
        self.assertNotIn("https://", d.split("console/login")[0][-80:])
        self.assertNotIn("https://", s.split("console/login")[0][-80:])


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """Flask/EngineClient remain read-only after integration."""

    def test_22_flask_routes_read_only(self):
        import sys
        import tempfile

        sys.path.insert(0, str(REPO_ROOT))
        from webapp import security
        from webapp.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            security.init_admin(state, "pw")
            app = create_app(state_dir=state)
            forbidden = (
                "start", "stop", "restart", "enable", "disable",
                "create", "edit", "delete", "upload", "restore",
                "nginx", "ssl", "firewall",
            )
            for rule in app.url_map.iter_rules():
                lower = rule.endpoint.lower()
                for word in forbidden:
                    self.assertNotIn(word, lower)

    def test_23_engineclient_operations_exactly_four(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import ALLOWED_OPERATIONS

        self.assertEqual(
            ALLOWED_OPERATIONS,
            frozenset({"version", "snapshot", "relay_list", "playlist_list"}),
        )

    def test_21_version_unchanged(self):
        self.assertEqual(repo_text("VERSION").strip(), "0.1.0")


if __name__ == "__main__":
    unittest.main()
