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

    def test_11_installed_libraries_root_owned_and_not_writable(self):
        # Root web-ctl sources lib/*.sh, so the deployed copies must be pinned
        # root-owned/non-writable independent of the source checkout's modes.
        inst = repo_text("install.sh")
        self.assertIn('cp -f "$BS_ROOT"/lib/*.sh "$libdir/lib/"', inst)
        self.assertIn('chown root:root "$libdir"/lib/*.sh', inst)
        self.assertIn('chmod 0644 "$libdir"/lib/*.sh', inst)

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


class HttpsFollowupTests(unittest.TestCase):
    """HTTPS follow-up bugs: SSL-aware checks + web console restart on SSL."""

    def test_01_http_only_diagnostics_expects_200(self):
        d = repo_text("lib/diagnostics.sh")
        self.assertIn('if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]', d)
        self.assertIn('= "200"', d)

    def test_02_http_only_selftest_expects_200(self):
        s = repo_text("lib/selftest.sh")
        self.assertIn('if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]', s)
        self.assertIn('= "200"', s)

    def test_03_ssl_enabled_diagnostics_expects_301(self):
        self.assertIn('= "301"', repo_text("lib/diagnostics.sh"))

    def test_04_ssl_enabled_selftest_expects_301(self):
        self.assertIn('= "301"', repo_text("lib/selftest.sh"))

    def test_05_exact_redirect_location_validated(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            t = repo_text(rel)
            self.assertIn('"$wloc" = "https://$BLUESTREAM_DOMAIN/console/login"', t)

    def test_06_arbitrary_301_not_accepted(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            t = repo_text(rel)
            # strict equality on BOTH code and Location; no 3xx wildcard
            self.assertIn('[ "$wcode" = "301" ]', t)
            self.assertNotIn("-ge 300", t)
            self.assertNotIn("3[0-9][0-9]", t)

    def test_07_local_loopback_retained(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            self.assertIn("http://127.0.0.1/console/login", repo_text(rel))

    def test_08_host_header_validated_domain(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            t = repo_text(rel)
            self.assertIn('-H "Host: $BLUESTREAM_DOMAIN"', t)
            self.assertIn('bs_valid_domain "$BLUESTREAM_DOMAIN"', t)

    def test_09_no_hardcoded_customer_domain(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            self.assertNotIn("stream.therealworldboosts.com", repo_text(rel))

    def test_10_no_eval_or_sh_c(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            t = repo_text(rel)
            self.assertNotIn("eval", t)
            self.assertNotIn("sh -c", t)

    def test_11_ssl_updates_secure_cookie_via_nginx_gen(self):
        s = repo_text("lib/ssl.sh")
        self.assertIn("nginx_gen_config", s)
        # the nginx pipeline itself must call nginx_sync_web_conf: its call site
        # appears before its definition, i.e. inside nginx_gen_config
        n = repo_text("lib/nginx.sh")
        self.assertLess(n.index("nginx_sync_web_conf"),
                        n.index("nginx_sync_web_conf()"))

    def test_12_ssl_restarts_active_web_service(self):
        s = repo_text("lib/ssl.sh")
        self.assertIn("ssl_restart_web_console", s)
        self.assertIn('systemctl restart "$unit"', s)
        # the restart call must sit inside the successful nginx-validation block
        # (after `if nginx_test; then`, before the success message) so a broken
        # config or failed reload can never trigger a restart.
        self.assertLess(s.index("if nginx_test; then"),
                        s.index("ssl_restart_web_console"))
        self.assertLess(s.index("ssl_restart_web_console"),
                        s.index('bs_ok "HTTPS enabled for $BLUESTREAM_DOMAIN"'))
        # it must also come AFTER nginx_gen_config regenerated web.conf
        self.assertLess(s.index("nginx_gen_config"),
                        s.index("ssl_restart_web_console"))

    def test_13_inactive_service_not_started(self):
        s = repo_text("lib/ssl.sh")
        self.assertIn('[ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ]', s)
        self.assertNotIn('systemctl start "$unit"', s)

    def test_14_missing_service_no_break(self):
        s = repo_text("lib/ssl.sh")
        self.assertIn('[ -f "/etc/systemd/system/$unit" ] || return 0', s)

    def test_15_restart_failure_reported(self):
        s = repo_text("lib/ssl.sh")
        self.assertIn("restart FAILED", s)
        self.assertIn("bs_warn", s)
        # the failure warning must be in the restart's failure branch: it is
        # placed after the restart attempt and before the manual-remediation
        # message, so a real failure is never silently swallowed.
        self.assertLess(s.index('systemctl restart "$unit"'),
                        s.index("restart FAILED"))
        self.assertLess(s.index("restart FAILED"),
                        s.index('bs_warn "Run manually: systemctl restart $unit"'))

    def test_16_no_webconsole_dependency_in_manager(self):
        self.assertNotIn("webconsole", repo_text("bluestream-manager"))

    def test_17_nginx_redirect_template_unchanged(self):
        redir = repo_text("config/nginx/console-redirect-http.conf")
        self.assertIn("return 301 https://$host$request_uri;", redir)
        self.assertIn("return 301 https://$host/console/;", redir)

    def test_18_no_curl_follow_or_tls_disable_in_console_check(self):
        for rel in ("lib/diagnostics.sh", "lib/selftest.sh"):
            t = repo_text(rel)
            self.assertNotIn(" -L", t)
            self.assertNotIn(" --location", t)
            self.assertNotIn(" --insecure", t)


class ReadOnlyGuaranteeTests(unittest.TestCase):
    """Mutation surface is exactly GUI-1B.1/GUI-1C.1's controlled operations."""

    def test_22_flask_routes_read_only_beyond_lifecycle(self):
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
                "edit", "delete", "remove", "restore",
                "enable", "disable",
                "nginx", "ssl", "firewall",
            )
            for rule in app.url_map.iter_rules():
                lower = rule.endpoint.lower()
                for word in forbidden:
                    self.assertNotIn(word, lower)

    def test_23_engineclient_operations_exactly_five(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import ALLOWED_OPERATIONS

        self.assertEqual(
            ALLOWED_OPERATIONS,
            frozenset(
                {"version", "snapshot", "relay_list", "playlist_list", "media_list"}
            ),
        )

    def test_24_engineclient_mutation_operations_exactly_nine(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import ALLOWED_MUTATION_OPERATIONS, EngineClient

        self.assertEqual(
            ALLOWED_MUTATION_OPERATIONS,
            frozenset(
                {
                    "relay_start", "relay_stop", "relay_restart",
                    "playlist_start", "playlist_stop", "playlist_restart",
                    "relay_create_url", "relay_create_media", "media_import_staged",
                }
            ),
        )
        client = EngineClient(production=True)
        for name in (
            "relay_start", "relay_stop", "relay_restart",
            "playlist_start", "playlist_stop", "playlist_restart",
            "relay_create_url", "relay_create_media", "media_import_staged",
        ):
            self.assertTrue(callable(getattr(client, name, None)), name)
        for name in (
            "relay_edit", "relay_delete", "playlist_edit", "playlist_delete",
            "media_remove", "media_delete",
        ):
            self.assertFalse(hasattr(client, name), name)

    def test_25_webctl_dispatch_exact_operation_allowlist(self):
        w = repo_text("web-ctl")
        for op in (
            "version", "snapshot", "relay_list", "playlist_list",
            "relay_start", "relay_stop", "relay_restart",
            "playlist_start", "playlist_stop", "playlist_restart",
        ):
            self.assertIn(op, w)
        # no shell code-execution constructs in the bridge
        self.assertNotIn("eval ", w)
        self.assertNotIn("eval(", w)
        self.assertNotIn("sh -c", w)
        # no OUT-OF-SCOPE mutation operation tokens anywhere in web-ctl
        # (create/upload are in scope for GUI-1C.1; edit/delete/remove/restore
        # and the infrastructure managers are not)
        for op in (
            "relay_delete", "relay_edit", "relay_remove",
            "playlist_delete", "playlist_edit", "playlist_remove",
            "media_delete", "media_remove", "restore", "firewall",
        ):
            self.assertNotIn(op, w)

    def test_26_webctl_reuses_engine_name_rule_and_lifecycle_functions(self):
        w = repo_text("web-ctl")
        self.assertIn('bs_valid_name "$name"', w)
        for fn in (
            "relay_start", "relay_stop", "relay_restart",
            "playlist_start", "playlist_stop", "playlist_restart",
        ):
            self.assertIn("( %s \"$name\" )" % fn, w)
        self.assertIn("relay_exists \"$name\"", w)
        self.assertIn("playlist_exists \"$name\"", w)

    def test_27_webctl_rejects_extra_args_and_missing_target(self):
        w = repo_text("web-ctl")
        self.assertIn("TOO_MANY_ARGUMENTS", w)
        self.assertIn("MISSING_TARGET", w)
        self.assertIn("INVALID_NAME", w)
        self.assertIn("NOT_FOUND", w)

    def test_28_sudoers_no_new_executable_no_setenv(self):
        s = repo_text("config/sudoers/bluestream-web")
        # active policy lines = non-comment, non-Defaults entries
        grant_lines = [
            ln.strip() for ln in s.splitlines()
            if ln.strip()
            and not ln.lstrip().startswith("#")
            and not ln.lstrip().startswith("Defaults")
        ]
        # the ONLY executable grant is the fixed web-ctl command
        self.assertEqual(
            grant_lines,
            ["bluestream-web ALL=(root) NOPASSWD: /usr/local/lib/bluestream/web-ctl"],
        )
        # no SETENV anywhere in the active policy
        for ln in grant_lines:
            self.assertNotIn("SETENV", ln)
            for exe in ("systemctl", "bluestream-manager", "bash", "python",
                        "env", "tee", "cp", "mv", "rm"):
                self.assertNotIn(exe, ln)
        # env hardening remains
        self.assertIn("env_reset", s)
        self.assertIn("secure_path", s)

    def test_21_version_unchanged(self):
        self.assertEqual(repo_text("VERSION").strip(), "0.1.0")


class LifecycleDeploymentFixesTests(unittest.TestCase):
    """GUI-1B.1 deployment fixes: sandbox write paths, intentional-stop
    normalization, and installer service restart on upgrade."""

    def test_01_service_keeps_strict_systemd_hardening(self):
        s = repo_text("config/systemd/bluestream-web.service")
        self.assertIn("ProtectSystem=strict", s)
        self.assertNotIn("ProtectSystem=off", s)
        for directive in (
            "ProtectHome=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "LockPersonality=true",
            "RestrictRealtime=true",
        ):
            self.assertIn(directive, s)

    def test_02_service_readwrite_paths_are_narrow_and_complete(self):
        s = repo_text("config/systemd/bluestream-web.service")
        rw_lines = [
            ln.strip() for ln in s.splitlines() if ln.strip().startswith("ReadWritePaths=")
        ]
        self.assertTrue(rw_lines, "no ReadWritePaths directive found")
        rw = " ".join(rw_lines)
        for path in (
            "/run/sudo",
            "/etc/systemd/system",
            "/var/www/bluestream/hls",
            "/var/lib/bluestream/run",
        ):
            self.assertIn(path, rw, path)
        # no broad/loosening writes
        self.assertNotIn("ReadWritePaths=/ ", rw)
        self.assertNotIn("ReadWritePaths=/var ", rw)
        self.assertNotIn("ReadWritePaths=/\n", rw)

    def test_03_service_web_state_stays_read_only(self):
        # the web state dir itself must never be in the ReadWritePaths list
        # (comments legitimately mention it); it stays read-only for the
        # unprivileged bluestream-web user. Only the narrow upload staging
        # subdir may be writable by the web process.
        s = repo_text("config/systemd/bluestream-web.service")
        rw = set()
        for ln in s.splitlines():
            ln = ln.strip()
            if ln.startswith("ReadWritePaths="):
                rw.update(ln.split("=", 1)[1].split())
        self.assertNotIn("/var/lib/bluestream/web", rw)
        self.assertIn("/var/lib/bluestream/web/upload", rw)

    def test_04_relay_stop_normalizes_intentional_stop(self):
        r = repo_text("lib/relay.sh")
        self.assertIn('unit="$(bs_unit relay "$name")"', r)
        self.assertIn('systemctl stop "$unit" 2>/dev/null || bs_die "Failed to stop relay', r)
        self.assertIn('systemctl reset-failed "$unit" 2>/dev/null || true', r)
        # the normalization targets the exact fixed unit, never a raw name
        self.assertNotIn('systemctl reset-failed "$name"', r)

    def test_05_playlist_stop_normalizes_intentional_stop(self):
        p = repo_text("lib/playlist.sh")
        self.assertIn('unit="$(bs_unit playlist "$name")"', p)
        self.assertIn('systemctl stop "$unit" 2>/dev/null || bs_die "Failed to stop playlist', p)
        self.assertIn('systemctl reset-failed "$unit" 2>/dev/null || true', p)
        self.assertNotIn('systemctl reset-failed "$name"', p)

    def test_06_stop_failure_is_not_converted_to_success(self):
        # both stop paths fail loudly via bs_die when systemctl stop fails;
        # there is no unconditional success after an unverified stop.
        for rel in ("lib/relay.sh", "lib/playlist.sh"):
            t = repo_text(rel)
            self.assertIn('|| bs_die "Failed to stop', t)
            self.assertNotIn('systemctl stop "$unit" 2>/dev/null\n    bs_ok', t)

    def test_07_installer_restarts_active_web_service_on_upgrade(self):
        wc = repo_text("lib/webconsole.sh")
        # upgrade path: active service is restarted so new code/templates load
        self.assertIn('if systemctl is-active "$BLUESTREAM_WEB_SERVICE" >/dev/null 2>&1; then', wc)
        self.assertIn('systemctl restart "$BLUESTREAM_WEB_SERVICE"', wc)
        # fresh-install path retained
        self.assertIn('systemctl start "$BLUESTREAM_WEB_SERVICE"', wc)
        # daemon-reload precedes the start/restart decision
        self.assertLess(wc.index("systemctl daemon-reload"), wc.index("systemctl is-active"))

    def test_08_fresh_install_start_behavior_remains(self):
        wc = repo_text("lib/webconsole.sh")
        self.assertIn('if ! systemctl start "$BLUESTREAM_WEB_SERVICE" 2>/dev/null; then', wc)
        self.assertIn("bs_die \"bluestream-web service failed to start. Fix the issue and re-run install.sh.\"", wc)

    def test_09_webctl_fixed_operation_model_unchanged(self):
        # the existing allowlist/argv/target model is untouched by these fixes
        w = repo_text("web-ctl")
        self.assertIn('relay_start|relay_stop|relay_restart|playlist_start|playlist_stop|playlist_restart', w)
        self.assertIn('bs_valid_name "$name"', w)
        self.assertNotIn("eval ", w)
        self.assertNotIn("sh -c", w)


class HealthStateBehaviorTests(unittest.TestCase):
    """Behavioral tests for lib/health.sh classification with a mocked systemctl.

    Runs the real bash health_state() against a fake ``systemctl`` on PATH so
    the ActiveState / enabled / Result combination logic is exercised rather
    than only text-checked. Scenario values are injected into the fake binary.
    """

    _FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# Fake systemctl for health classification tests (values injected per case).
case "$1" in
    is-active)  printf '%s\\n' '__IS_ACTIVE__' ;;
    is-enabled) printf '%s\\n' '__IS_ENABLED__' ;;
    show)
        if [ "$2" = "-p" ] && [ "$3" = "Result" ]; then
            printf '%s\\n' '__RESULT__'
        fi
        ;;
    *) exit 0 ;;
esac
"""

    _HARNESS = """#!/usr/bin/env bash
set -u
repo="$(cygpath -u "$1" 2>/dev/null || printf '%s' "$1")"
bin="$(cygpath -u "$2" 2>/dev/null || printf '%s' "$2")"
name="$3"
kind="$4"
export PATH="$bin:$PATH"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
confdir="$tmp/conf"
hlsroot="$tmp/hls"
mkdir -p "$confdir" "$hlsroot"
if [ "$kind" = "relay" ]; then
    : > "$confdir/$name.conf"
else
    : > "$confdir/$name.playlist"
fi
# shellcheck source=lib/common.sh
. "$repo/lib/common.sh"
# shellcheck source=lib/health.sh
. "$repo/lib/health.sh"
BLUESTREAM_RELAY_CONF_DIR="$confdir"
BLUESTREAM_PLAYLIST_CONF_DIR="$confdir"
BLUESTREAM_HLS_ROOT="$hlsroot"
# Deterministic active-branch classification: pretend HLS is always fresh.
bs_hls_is_fresh() { return 0; }
health_state "$kind" "$name"
printf '%s\\n' "$HEALTH_STATE"
"""

    def _run(self, kind, name, is_active, is_enabled, result):
        import subprocess
        import tempfile

        import webapp.engine as engine_module

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            bindir = tmpdir / "bin"
            bindir.mkdir()
            fake = bindir / "systemctl"
            fake.write_text(
                self._FAKE_SYSTEMCTL.replace("__IS_ACTIVE__", is_active)
                .replace("__IS_ENABLED__", is_enabled)
                .replace("__RESULT__", result),
                encoding="utf-8",
            )
            fake.chmod(0o700)
            harness = tmpdir / "harness.sh"
            harness.write_text(self._HARNESS, encoding="utf-8")
            bash = engine_module.default_bash_path()
            proc = subprocess.run(
                [bash, str(harness), str(REPO_ROOT), str(bindir), name, kind],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            return proc.stdout.decode("utf-8").strip()

    def test_01_inactive_enabled_result_success_is_stopped(self):
        self.assertEqual(self._run("relay", "test1080", "inactive", "enabled", "success"), "STOPPED")

    def test_02_inactive_disabled_result_success_is_stopped(self):
        self.assertEqual(self._run("playlist", "pl1", "inactive", "disabled", "success"), "STOPPED")

    def test_03_failed_active_state_is_failed(self):
        self.assertEqual(self._run("relay", "test1080", "failed", "enabled", "exit-code"), "FAILED")

    def test_04_inactive_enabled_non_success_result_is_failed(self):
        self.assertEqual(self._run("relay", "test1080", "inactive", "enabled", "exit-code"), "FAILED")

    def test_05_active_remains_healthy(self):
        self.assertEqual(self._run("relay", "test1080", "active", "enabled", "success"), "HEALTHY")

    def test_06_activating_is_starting(self):
        self.assertEqual(self._run("playlist", "pl1", "activating", "enabled", "success"), "STARTING")

    def test_07_inactive_enabled_empty_result_is_failed(self):
        # unknown/empty Result is NOT treated as success - failures stay visible
        self.assertEqual(self._run("relay", "test1080", "inactive", "enabled", ""), "FAILED")

    def test_08_inactive_disabled_non_success_result_is_stopped(self):
        self.assertEqual(self._run("playlist", "pl1", "inactive", "disabled", "exit-code"), "STOPPED")

    def test_09_playlist_shares_relay_classification(self):
        self.assertEqual(self._run("playlist", "pl1", "inactive", "enabled", "success"), "STOPPED")
        self.assertEqual(self._run("playlist", "pl1", "inactive", "enabled", "exit-code"), "FAILED")

    def test_10_no_exec_main_status_255_special_case_in_health(self):
        h = repo_text("lib/health.sh")
        self.assertNotIn("ExecMainStatus", h)
        self.assertNotIn("255", h)

    def test_11_health_uses_systemd_result(self):
        h = repo_text("lib/health.sh")
        self.assertIn('systemctl show -p Result --value "$unit"', h)
        self.assertIn('[ "$result" = "success" ]', h)


class Gui1cValidationTests(unittest.TestCase):
    """Python-side validation mirrors for the engine rules used by GUI-1C.1."""

    def test_01_valid_source_url_accepts_supported_schemes(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import valid_source_url

        for good in (
            "https://example.com/live/index.m3u8",
            "http://example.com/video.mp4",
            "rtmp://127.0.0.1:1935/app/key",
            "rtmps://example.com/app/key",
            "rtsp://example.com/live/stream",
            "https://example.com/live/index.m3u8?token=abc&x=1",
        ):
            self.assertTrue(valid_source_url(good), good)

    def test_02_valid_source_url_rejects_unsupported_schemes(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import valid_source_url

        for bad in (
            "file:///etc/passwd",
            "ftp://example.com/x.mp4",
            "ssh://host/x",
            "data:text/html,<script>",
            "javascript:alert(1)",
            "",
            "https://example.com/a b.m3u8",
            "https://example.com/`id`.m3u8",
            "https://" + "a" * 5000,
        ):
            self.assertFalse(valid_source_url(bad), bad)

    def test_03_valid_media_name_rule(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.engine import valid_media_name

        for good in ("promo.mp4", "clip_01.mkv", "file.mov", "A.B.webm", "x" * 255):
            self.assertTrue(valid_media_name(good), good)
        for bad in ("", "..", "../x", "a/b", "a\\b", ".hidden", "x y", "a;b", "x" * 256):
            self.assertFalse(valid_media_name(bad), bad)

    def test_04_sanitize_upload_filename_strips_traversal(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.app import sanitize_upload_filename

        self.assertEqual(sanitize_upload_filename("promo.mp4"), "promo.mp4")
        self.assertEqual(sanitize_upload_filename("promo.MP4"), "promo.MP4")
        # directory components are stripped, never trusted
        self.assertEqual(sanitize_upload_filename("C:\\fakepath\\promo.mp4"), "promo.mp4")
        self.assertEqual(sanitize_upload_filename("../../etc/passwd"), None)
        self.assertEqual(sanitize_upload_filename("..\\..\\evil.mp4"), None)
        self.assertEqual(sanitize_upload_filename("a/b.mp4"), "b.mp4")
        self.assertEqual(sanitize_upload_filename(""), None)
        self.assertEqual(sanitize_upload_filename(".hidden.mp4"), None)
        self.assertEqual(sanitize_upload_filename("x y.mp4"), None)
        self.assertEqual(sanitize_upload_filename("evil.exe"), None)
        self.assertEqual(sanitize_upload_filename("script.sh"), None)
        self.assertEqual(sanitize_upload_filename("x" * 300 + ".mp4"), None)

    def test_05_human_size_formats(self):
        import sys

        sys.path.insert(0, str(REPO_ROOT))
        from webapp.app import human_size

        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.0 KiB")
        self.assertEqual(human_size(1048576), "1.0 MiB")
        self.assertEqual(human_size("not-a-number"), "?")

    def test_06_media_quarantine_dir_root_only_by_installer(self):
        common = repo_text("lib/common.sh")
        # quarantine lives under the existing root-run tree, not a web-writable
        # path
        self.assertIn('BLUESTREAM_MEDIA_QUARANTINE_DIR="${BLUESTREAM_RUN_DIR}/media-import"', common)
        wc = repo_text("lib/webconsole.sh")
        # installer creates it root:root 0700 idempotently
        self.assertIn('chown root:root "$BLUESTREAM_MEDIA_QUARANTINE_DIR"', wc)
        self.assertIn('chmod 0700 "$BLUESTREAM_MEDIA_QUARANTINE_DIR"', wc)
        inst = repo_text("install.sh")
        # its parent /var/lib/bluestream/run is NOT web-writable
        # (bluestream-relay:bluestream-relay 0750; web user is not in the group)
        self.assertIn('chown "$BLUESTREAM_USER":"$BLUESTREAM_GROUP" "$BLUESTREAM_RUN_DIR"', inst)
        self.assertIn('chmod 0750 "$BLUESTREAM_RUN_DIR"', inst)
        media = repo_text("lib/media.sh")
        # the import path transfers ownership via rename and rejects symlinks
        self.assertIn('mv "$staged" "$quarantined"', media)
        self.assertIn('[ -L "$quarantined" ]', media)
        # ffprobe runs on the root-owned snapshot, never the web-originated
        # inode or the staging path
        self.assertIn('probe_media_path "$snapshot"', media)
        self.assertNotIn('probe_media_path "$quarantined"', media)
        self.assertNotIn('probe_media_path "$staged"', media)

    def test_07_snapshot_handoff_ordering(self):
        # Root-owned snapshot handoff: copy the web-originated inode into a
        # NEW root-owned inode, unlink the original, and only then probe and
        # publish the snapshot. The object validated is exactly the object
        # published.
        media = repo_text("lib/media.sh")
        cp_pos = media.index('cp -f -- "$quarantined" "$snapshot"')
        unlink_pos = media.rindex('rm -f -- "$quarantined"')
        probe_pos = media.index('probe_media_path "$snapshot"')
        self.assertLess(cp_pos, unlink_pos)
        self.assertLess(unlink_pos, probe_pos)
        # the web-originated inode is never probed or published
        self.assertNotIn('probe_media_path "$quarantined"', media)
        self.assertNotIn('mv -n "$quarantined"', media)
        # ownership failures fail closed on the snapshot itself
        self.assertIn('chown root:"$BLUESTREAM_GROUP" "$snapshot"', media)
        self.assertIn('chmod 0640 "$snapshot"', media)
        self.assertNotIn('chown root:"$BLUESTREAM_GROUP" "$tmp"', media)


class Gui1cEngineBehaviorTests(unittest.TestCase):
    """Behavioral tests for the GUI-1C.1 engine functions with a sandboxed dir
    layout (conf/media/upload dirs overridden to a temp tree)."""

    _HARNESS = """#!/usr/bin/env bash
set -u
repo="$(cygpath -u "$1" 2>/dev/null || printf '%s' "$1")"
scenario="$(cygpath -u "$2" 2>/dev/null || printf '%s' "$2")"
tmp="$(cygpath -u "$3" 2>/dev/null || printf '%s' "$3")"
. "$repo/lib/common.sh"
. "$repo/lib/probe.sh"
. "$repo/lib/relay.sh"
. "$repo/lib/media.sh"
BLUESTREAM_RELAY_CONF_DIR="$tmp/conf"
BLUESTREAM_MEDIA_DIR="$tmp/media"
BLUESTREAM_WEB_UPLOAD_DIR="$tmp/upload"
BLUESTREAM_MEDIA_QUARANTINE_DIR="$tmp/quarantine"
mkdir -p "$BLUESTREAM_RELAY_CONF_DIR" "$BLUESTREAM_MEDIA_DIR" "$BLUESTREAM_WEB_UPLOAD_DIR" "$BLUESTREAM_MEDIA_QUARANTINE_DIR"
# Unit-test isolation: engine ops require root + ffprobe; stub both so the
# tests exercise the pure logic only. chown is a no-op here (non-root); the
# quarantine owner check is gated on id -u in the engine function, and the
# snapshot chown is overridden to succeed so import logic is exercised.
bs_require_root() { return 0; }
chown() { return 0; }
probe_media_path() { return 0; }
mkdir -p "$tmp/bin"
cat > "$tmp/bin/ffprobe" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp/bin/ffprobe" "$tmp/bin/install"
export PATH="$tmp/bin:$PATH"
. "$scenario"
"""

    def _run(self, scenario_text):
        import subprocess
        import tempfile

        import webapp.engine as engine_module

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            scenario = tmpdir / "scenario.sh"
            scenario.write_text(scenario_text, encoding="utf-8")
            harness = tmpdir / "harness.sh"
            harness.write_text(self._HARNESS, encoding="utf-8")
            bash = engine_module.default_bash_path()
            proc = subprocess.run(
                [bash, str(harness), str(REPO_ROOT), str(scenario), tmp],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            return proc.stdout.decode("utf-8").strip().splitlines()

    def _symlinks_supported(self):
        """Detect whether the host can create REAL symlinks via ln -s.

        Git Bash/MSYS on some Windows hosts silently falls back to copying the
        target instead of creating a symlink; the symlink-rejection tests are
        only meaningful where a real symlink is produced (they run on POSIX).
        """
        out = self._run(
            "printf 'x' > \"$tmp/src.mp4\"\n"
            "ln -s \"$tmp/src.mp4\" \"$tmp/link.mp4\"\n"
            "[ -L \"$tmp/link.mp4\" ] && echo YES || echo NO\n"
        )
        return out == ["YES"]

    def test_01_classify_source_type(self):
        out = self._run(
            "echo \"$(bs_classify_source_type 'https://x/live/index.m3u8')\"\n"
            "echo \"$(bs_classify_source_type 'https://x/live/index.m3u8?token=a')\"\n"
            "echo \"$(bs_classify_source_type 'https://x/video.mp4')\"\n"
            "echo \"$(bs_classify_source_type 'http://x/a.ts')\"\n"
            "echo \"$(bs_classify_source_type 'rtmp://h/a/k')\"\n"
            "echo \"$(bs_classify_source_type 'rtmps://h/a/k')\"\n"
            "echo \"$(bs_classify_source_type 'rtsp://h/s')\"\n"
            "echo \"$(bs_classify_source_type 'file:///etc/passwd' || echo UNSUPPORTED)\"\n"
        )
        self.assertEqual(
            out,
            [
                "remote-hls", "remote-hls", "http-file", "http-file",
                "rtmp", "rtmps", "rtsp", "UNSUPPORTED",
            ],
        )

    def test_02_relay_create_url_config_and_duplicate(self):
        out = self._run(
            "relay_create news24 remote-hls 'https://x/live/index.m3u8'\n"
            "echo CREATE_RC=$?\n"
            "echo CONF=$([ -f \"$BLUESTREAM_RELAY_CONF_DIR/news24.conf\" ] && echo yes || echo no)\n"
            "relay_load_config news24\n"
            "echo TYPE=$RELAY_TYPE URL=$RELAY_URL ENABLED=$RELAY_ENABLED\n"
            "relay_create news24 remote-hls 'https://x/again.m3u8'\n"
            "echo DUP_RC=$?\n"
            "relay_create 'Bad Name' remote-hls 'https://x/a.m3u8'\n"
            "echo BADNAME_RC=$?\n"
        )
        self.assertEqual(
            out,
            [
                "CREATE_RC=0",
                "CONF=yes",
                "TYPE=remote-hls URL=https://x/live/index.m3u8 ENABLED=no",
                "DUP_RC=1",
                "BADNAME_RC=1",
            ],
        )

    def test_03_relay_create_media_local_file(self):
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_MEDIA_DIR/promo.mp4\"\n"
            "relay_create promo-loop local-file \"$BLUESTREAM_MEDIA_DIR/promo.mp4\"\n"
            "echo CREATE_RC=$?\n"
            "relay_load_config promo-loop\n"
            "echo \"TYPE=$RELAY_TYPE URL=$(basename \"$RELAY_URL\") ENABLED=$RELAY_ENABLED\"\n"
        )
        self.assertEqual(
            out,
            [
                "CREATE_RC=0",
                "TYPE=local-file URL=promo.mp4 ENABLED=no",
            ],
        )

    def test_04_media_import_staged_imports_and_cleans(self):
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "media_import_staged promo.mp4\n"
            "echo IMPORT_RC=$?\n"
            "echo MEDIA=$([ -f \"$BLUESTREAM_MEDIA_DIR/promo.mp4\" ] && echo yes || echo no)\n"
            "echo STAGED_GONE=$([ -e \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\" ] && echo no || echo yes)\n"
            "printf 'x' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "media_import_staged promo.mp4\n"
            "echo DUP_RC=$?\n"
        )
        self.assertEqual(
            out,
            ["IMPORT_RC=0", "MEDIA=yes", "STAGED_GONE=yes", "DUP_RC=3"],
        )

    def test_05_media_import_staged_rejects_traversal_and_bad_names(self):
        out = self._run(
            "printf 'x' > \"$BLUESTREAM_WEB_UPLOAD_DIR/ok.mp4\"\n"
            "media_import_staged '../evil.mp4'\n"
            "echo TRAV_RC=$?\n"
            "media_import_staged 'a b.mp4'\n"
            "echo SPACE_RC=$?\n"
            "media_import_staged ''\n"
            "echo EMPTY_RC=$?\n"
            "echo NO_EVIL=$([ -e \"$BLUESTREAM_MEDIA_DIR/../evil.mp4\" ] && echo yes || echo no)\n"
        )
        self.assertEqual(out, ["TRAV_RC=1", "SPACE_RC=1", "EMPTY_RC=1", "NO_EVIL=no"])

    def test_06_media_list_names_only_files(self):
        out = self._run(
            "printf 'a' > \"$BLUESTREAM_MEDIA_DIR/a.mp4\"\n"
            "printf 'b' > \"$BLUESTREAM_MEDIA_DIR/b.ts\"\n"
            "mkdir -p \"$BLUESTREAM_MEDIA_DIR/subdir\"\n"
            "printf 'c' > \"$BLUESTREAM_MEDIA_DIR/subdir/c.mp4\"\n"
            "for n in $(media_list_names | sort); do echo \"LIST:$n\"; done\n"
        )
        self.assertEqual(out, ["LIST:a.mp4", "LIST:b.ts"])

    def test_07_bs_redact_url_removes_credentials_and_query_tokens(self):
        out = self._run(
            "echo \"$(bs_redact_url 'https://user:pass@example.com/live/index.m3u8?token=abc&x=1')\"\n"
            "echo \"$(bs_redact_url 'https://example.com/live/index.m3u8?X-Amz-Signature=deadbeef&y=2')\"\n"
            "echo \"$(bs_redact_url 'https://example.com/live/plain.m3u8')\"\n"
            "echo \"$(bs_redact_url '/var/lib/bluestream/media/promo.mp4')\"\n"
        )
        self.assertEqual(
            out,
            [
                "https://example.com/live/index.m3u8?token=***&x=1",
                "https://example.com/live/index.m3u8?X-Amz-Signature=***&y=2",
                "https://example.com/live/plain.m3u8",
                "/var/lib/bluestream/media/promo.mp4",
            ],
        )

    def test_08_relay_config_never_stores_redacted_values(self):
        # storage keeps the REAL URL; redaction is display-only
        out = self._run(
            "relay_create secfeed remote-hls 'https://user:pass@example.com/live/index.m3u8?token=abc'\n"
            "relay_load_config secfeed\n"
            "echo STORED=$RELAY_URL\n"
            "echo DISPLAY=$(bs_redact_url \"$RELAY_URL\")\n"
        )
        self.assertEqual(
            out,
            [
                "STORED=https://user:pass@example.com/live/index.m3u8?token=abc",
                "DISPLAY=https://example.com/live/index.m3u8?token=***",
            ],
        )

    def test_09_symlink_to_media_rejected(self):
        if not self._symlinks_supported():
            self.skipTest("host cannot create real symlinks (ln -s copies); POSIX-only")
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_MEDIA_DIR/existing.mp4\"\n"
            "ln -s \"$BLUESTREAM_MEDIA_DIR/existing.mp4\" \"$BLUESTREAM_WEB_UPLOAD_DIR/evil.mp4\"\n"
            "media_import_staged evil.mp4\n"
            "echo SYMLINK_RC=$?\n"
            "echo IMPORTED=$([ -e \"$BLUESTREAM_MEDIA_DIR/evil.mp4\" ] && echo yes || echo no)\n"
            "echo STAGING_GONE=$([ -e \"$BLUESTREAM_WEB_UPLOAD_DIR/evil.mp4\" ] && echo no || echo yes)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["SYMLINK_RC=4", "IMPORTED=no", "STAGING_GONE=yes", "QUARANTINE_COUNT=0"],
        )

    def test_10_symlink_to_outside_path_rejected(self):
        # a symlink pointing OUTSIDE the staging/web writable area must be
        # rejected before any ffprobe/import, even if the target is readable
        # by root only.
        if not self._symlinks_supported():
            self.skipTest("host cannot create real symlinks (ln -s copies); POSIX-only")
        out = self._run(
            "printf 'not-media' > \"$tmp/outside.bin\"\n"
            "ln -s \"$tmp/outside.bin\" \"$BLUESTREAM_WEB_UPLOAD_DIR/outside.mp4\"\n"
            "media_import_staged outside.mp4\n"
            "echo OUTSIDE_RC=$?\n"
            "echo IMPORTED=$([ -e \"$BLUESTREAM_MEDIA_DIR/outside.mp4\" ] && echo yes || echo no)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["OUTSIDE_RC=4", "IMPORTED=no", "QUARANTINE_COUNT=0"],
        )

    def test_11_broken_symlink_rejected(self):
        if not self._symlinks_supported():
            self.skipTest("host cannot create real symlinks (ln -s copies); POSIX-only")
        out = self._run(
            "ln -s \"$BLUESTREAM_WEB_UPLOAD_DIR/does-not-exist.mp4\" \"$BLUESTREAM_WEB_UPLOAD_DIR/broken.mp4\"\n"
            "media_import_staged broken.mp4\n"
            "echo BROKEN_RC=$?\n"
            "echo IMPORTED=$([ -e \"$BLUESTREAM_MEDIA_DIR/broken.mp4\" ] && echo yes || echo no)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["BROKEN_RC=4", "IMPORTED=no", "QUARANTINE_COUNT=0"],
        )

    def test_12_staging_recreated_after_quarantine_cannot_alter_import(self):
        # Deterministic ownership-transfer proof: a hook (test-only seam, inert
        # in production) recreates the ORIGINAL staging pathname AFTER the
        # privileged quarantine rename. The import must still use the
        # quarantined object (ORIGINAL content), never the recreated staging
        # entry.
        out = self._run(
            "printf 'ORIGINAL' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "cat > \"$tmp/hook.sh\" <<EOF\n"
            "#!/usr/bin/env bash\n"
            "printf 'EVIL' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "exit 0\n"
            "EOF\n"
            "chmod +x \"$tmp/hook.sh\"\n"
            "export BLUESTREAM_MEDIA_IMPORT_HOOK=\"$tmp/hook.sh\"\n"
            "media_import_staged promo.mp4\n"
            "echo IMPORT_RC=$?\n"
            "echo CONTENT=$(cat \"$BLUESTREAM_MEDIA_DIR/promo.mp4\")\n"
            "echo STAGING_CONTENT=$(cat \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\")\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
            "unset BLUESTREAM_MEDIA_IMPORT_HOOK\n"
        )
        self.assertEqual(
            out,
            ["IMPORT_RC=0", "CONTENT=ORIGINAL", "STAGING_CONTENT=EVIL", "QUARANTINE_COUNT=0"],
        )

    def test_13_failed_probe_cleans_quarantine(self):
        # A staged file that fails the ffprobe gate must be removed from
        # quarantine (here the harness overrides probe_media_path to succeed,
        # so simulate failure by overriding it inside the scenario AFTER
        # defining the failure mode via a symlink-free regular file).
        out = self._run(
            "printf 'junk' > \"$BLUESTREAM_WEB_UPLOAD_DIR/junk.mp4\"\n"
            "probe_media_path() { return 1; }\n"
            "media_import_staged junk.mp4\n"
            "echo PROBE_RC=$?\n"
            "echo IMPORTED=$([ -e \"$BLUESTREAM_MEDIA_DIR/junk.mp4\" ] && echo yes || echo no)\n"
            "echo STAGING_GONE=$([ -e \"$BLUESTREAM_WEB_UPLOAD_DIR/junk.mp4\" ] && echo no || echo yes)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["PROBE_RC=6", "IMPORTED=no", "STAGING_GONE=yes", "QUARANTINE_COUNT=0"],
        )

    def test_14_retained_writable_fd_cannot_alter_snapshot(self):
        # Deterministic snapshot-handoff proof (POSIX/Linux only - Windows/MSYS
        # does not reproduce rename-with-open-handle or write-to-unlinked-inode
        # semantics). A writable FD is opened on the staged file and KEPT OPEN;
        # the hook writes through it AFTER the root snapshot handoff. The
        # imported file must still contain the snapshot content, the ffprobe
        # path must be the root-owned snapshot, and the probed inode must be
        # the published inode.
        import sys

        if sys.platform not in ("linux", "darwin"):
            self.skipTest("retained-FD write-to-unlinked-inode semantics require a POSIX host")
        out = self._run(
            "printf 'ORIGINAL' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "exec 9>>\"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "probe_media_path() { stat -c %i \"$1\" > \"$tmp/probed_inode.txt\"; printf '%s\\n' \"$1\" > \"$tmp/probed_path.txt\"; return 0; }\n"
            "cat > \"$tmp/hook.sh\" <<EOF\n"
            "#!/usr/bin/env bash\n"
            "printf 'EVIL' >&9\n"
            "exit 0\n"
            "EOF\n"
            "chmod +x \"$tmp/hook.sh\"\n"
            "export BLUESTREAM_MEDIA_IMPORT_HOOK=\"$tmp/hook.sh\"\n"
            "media_import_staged promo.mp4\n"
            "echo IMPORT_RC=$?\n"
            "echo CONTENT=$(cat \"$BLUESTREAM_MEDIA_DIR/promo.mp4\")\n"
            "echo SAME_INODE=$([ \"$(cat \"$tmp/probed_inode.txt\")\" = \"$(stat -c %i \"$BLUESTREAM_MEDIA_DIR/promo.mp4\")\" ] && echo yes || echo no)\n"
            "P=$(cat \"$tmp/probed_path.txt\")\n"
            "echo PROBED_QUARANTINE=$([ \"${P#$BLUESTREAM_MEDIA_QUARANTINE_DIR/}\" != \"$P\" ] && echo yes || echo no)\n"
            "echo PROBED_SNAPSHOT_SUFFIX=$([ \"${P%.snapshot}\" != \"$P\" ] && echo yes || echo no)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
            "exec 9>&-\n"
        )
        self.assertEqual(
            out,
            [
                "IMPORT_RC=0",
                "CONTENT=ORIGINAL",
                "SAME_INODE=yes",
                "PROBED_QUARANTINE=yes",
                "PROBED_SNAPSHOT_SUFFIX=yes",
                "QUARANTINE_COUNT=0",
            ],
        )

    def test_15_snapshot_copy_failure_cleans_original_and_snapshot(self):
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "cp() { return 1; }\n"
            "media_import_staged promo.mp4\n"
            "echo COPY_RC=$?\n"
            "echo MEDIA=$([ -e \"$BLUESTREAM_MEDIA_DIR/promo.mp4\" ] && echo yes || echo no)\n"
            "echo STAGING_GONE=$([ -e \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\" ] && echo no || echo yes)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["COPY_RC=7", "MEDIA=no", "STAGING_GONE=yes", "QUARANTINE_COUNT=0"],
        )

    def test_16_chown_failure_fails_closed(self):
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "chown() { return 1; }\n"
            "media_import_staged promo.mp4\n"
            "echo CHOWN_RC=$?\n"
            "echo MEDIA=$([ -e \"$BLUESTREAM_MEDIA_DIR/promo.mp4\" ] && echo yes || echo no)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["CHOWN_RC=7", "MEDIA=no", "QUARANTINE_COUNT=0"],
        )

    def test_17_chmod_failure_fails_closed(self):
        out = self._run(
            "printf 'dummy' > \"$BLUESTREAM_WEB_UPLOAD_DIR/promo.mp4\"\n"
            "chmod() { case \"$1\" in 0700) command chmod \"$@\" ;; *) return 1 ;; esac; }\n"
            "media_import_staged promo.mp4\n"
            "echo CHMOD_RC=$?\n"
            "echo MEDIA=$([ -e \"$BLUESTREAM_MEDIA_DIR/promo.mp4\" ] && echo yes || echo no)\n"
            "echo QUARANTINE_COUNT=$(ls -1 \"$BLUESTREAM_MEDIA_QUARANTINE_DIR\" 2>/dev/null | wc -l)\n"
        )
        self.assertEqual(
            out,
            ["CHMOD_RC=7", "MEDIA=no", "QUARANTINE_COUNT=0"],
        )


if __name__ == "__main__":
    unittest.main()
