"""Guards for the native in-place Studio activation scripts (tools/canary/studio_activate*.sh, studio_smoke.py).
The behaviour was exercised in a Docker container with a systemd shim; these tests pin the safety properties."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANARY = ROOT / "tools" / "canary"
DEPLOY = (CANARY / "studio_activate.sh").read_text(encoding="utf-8")
ROLLBACK = (CANARY / "studio_activate_rollback.sh").read_text(encoding="utf-8")


def code(text):
    return "\n".join(ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))


class SafetyTests(unittest.TestCase):
    def test_only_the_dashboard_is_ever_restarted(self):
        for name, text in (("deploy", code(DEPLOY)), ("rollback", code(ROLLBACK))):
            for m in re.finditer(r"systemctl\s+(restart|stop|start|kill|reload|try-restart|reload-or-restart|disable|mask)\b([^\n]*)", text):
                self.assertIn("bluestream-web.service", m.group(2), "%s: %s" % (name, m.group(0)))

    def test_no_host_or_package_or_stream_changes(self):
        for text in (code(DEPLOY), code(ROLLBACK)):
            for bad in ("apt-get", "apt install", "dpkg", "ufw", "iptables", "nginx -s", "nginx.service", "clpctl", "systemctl restart nginx",
                        "docker ", "reboot", "shutdown", "install.sh", "canary_install", "visudo -f"):
                self.assertNotIn(bad, text, bad)

    def test_sudoers_is_only_validated_never_written(self):
        self.assertIn("visudo -c", DEPLOY)
        self.assertNotRegex(code(DEPLOY), r">\s*/etc/sudoers")

    def test_files_are_replaced_atomically_and_backed_up_first(self):
        self.assertIn('mv -f "$dst.bs-new.$$" "$dst"', DEPLOY)
        self.assertLess(DEPLOY.index('cp -a "$dst" "$BK/files$dst"'), DEPLOY.index('mv -f "$dst.bs-new.$$" "$dst"'))
        self.assertIn('echo "MOD $dst" >> "$BK/manifest"', DEPLOY)

    def test_failure_triggers_rollback_without_touching_streams(self):
        self.assertIn("fail_and_rollback", DEPLOY)
        self.assertIn("rollback.sh", DEPLOY)
        for ln in code(ROLLBACK).splitlines():
            self.assertNotRegex(ln, r"bluestream-(relay|publisher|playlist|browser)@.*(restart|start|stop)")

    def test_default_is_a_read_only_dry_run(self):
        self.assertIn('if [ "$EXECUTE" != 1 ]; then', DEPLOY)
        self.assertLess(DEPLOY.index('if [ "$EXECUTE" != 1 ]; then'), DEPLOY.index('mkdir -p "$BK/files"'))

    def test_source_must_match_the_tested_commit(self):
        self.assertRegex(DEPLOY, r'EXPECT="[0-9a-f]{40}"')
        self.assertIn("git -C \"$SRC\" diff --quiet \"$EXPECT\" -- lib config web-ctl webapp", DEPLOY)

    def test_deploys_the_files_the_studio_backend_needs(self):
        for need in ("lib/*.sh", "run-relay.sh", "run-publisher.sh", "web-ctl", "bluestream-publisher@.service", "50-studio.conf",
                     "BLUESTREAM_STUDIO_ENABLED=1", "ReadWritePaths"):
            self.assertIn(need, DEPLOY, need)

    def test_stream_state_is_compared_before_and_after(self):
        for need in ("MainPID", "NRestarts", "InvocationID", "streams.before", "streams.after", "Interrupted"):
            self.assertIn(need, DEPLOY, need)

    def test_secrets_are_never_printed(self):
        for text in (DEPLOY, ROLLBACK, (CANARY / "studio_smoke.py").read_text(encoding="utf-8")):
            self.assertNotRegex(code(text), r"echo[^\n]*secret_key|print\([^\n]*(cookie|key)\b")

    def test_scripts_parse(self):
        ast.parse((CANARY / "studio_smoke.py").read_text(encoding="utf-8"))
        bash = shutil.which("bash")
        if bash:
            for f in ("studio_activate.sh", "studio_activate_rollback.sh"):
                r = subprocess.run([bash, "-n", (CANARY / f).as_posix()], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
