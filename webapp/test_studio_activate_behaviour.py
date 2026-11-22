"""Behavioural regression tests for tools/canary/studio_activate.sh and studio_activate_rollback.sh.

They run the REAL scripts against a fake root (BS_ACT_ROOT), a stub systemctl, real processes standing in for FFmpeg and a local
HTTP server standing in for the dashboard. Linux only (needs /proc and pgrep). Nothing here touches the machine's real services."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANARY = ROOT / "tools" / "canary"

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
# fake systemctl driven by files under $ST
echo "$*" >> "$ST/calls.log"
prop=""; args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -p) prop="$2"; shift 2 ;;
        --*) shift ;;
        *) args+=("$1"); shift ;;
    esac
done
verb="${args[0]:-}"; unit="${args[1]:-}"
case "$verb" in
  cat) echo "ExecStart=/usr/bin/gunicorn --workers 1 --bind 127.0.0.1:$(cat "$ST/port") webapp.wsgi:application" ;;
  is-active)
      if [ "$unit" = bluestream-web.service ] && [ ! -f "$ST/web-down" ]; then echo active; exit 0; fi
      if [ -f "$ST/active/$unit" ]; then echo active; exit 0; fi
      echo inactive; exit 3 ;;
  list-units) ls "$ST/active" 2>/dev/null | sed 's/$/ loaded active running x/' ;;
  show) case "$prop" in
          MainPID) cat "$ST/pid/$unit" 2>/dev/null || echo 0 ;;
          NRestarts) cat "$ST/nr/$unit" 2>/dev/null || echo 0 ;;
          InvocationID) cat "$ST/inv/$unit" 2>/dev/null || echo "inv-$unit" ;;
          Environment) [ -f "$ROOTFS/etc/systemd/system/bluestream-web.service.d/50-studio.conf" ] && echo "BLUESTREAM_STUDIO_ENABLED=1" ;;
        esac ;;
  daemon-reload) [ -f "$ST/reload-fails" ] && exit 1; exit 0 ;;
  restart) echo "$unit" >> "$ST/restarted"
           [ -f "$ST/restart-fails" ] && exit 1
           [ -f "$ST/unhealthy-after-restart" ] && touch "$ST/unhealthy"
           exit 0 ;;
  *) echo "unexpected systemctl verb: $verb" >> "$ST/violations"; exit 1 ;;
esac
"""


class _Health(BaseHTTPRequestHandler):
    flag = ""

    def do_GET(self):
        self.send_response(500 if _Health.flag and os.path.exists(_Health.flag) else 200)
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, *a):
        pass


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("pgrep"), "needs Linux + procps")
class BehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        self.root, self.st, self.bin, self.bk = t / "root", t / "st", t / "bin", t / "backups"
        for d in (self.st / "active", self.st / "pid", self.st / "nr", self.bin, self.bk, t / "work"):
            d.mkdir(parents=True)
        inst = self.root / "usr/local/lib/bluestream"
        # "old" production = real repo files, then tampered so there is something to deploy
        shutil.copytree(ROOT / "lib", inst / "lib")
        shutil.copytree(ROOT / "webapp", inst / "webapp", ignore=shutil.ignore_patterns("test_*.py", "__pycache__", ".*"))
        shutil.copy(ROOT / "web-ctl", inst / "web-ctl")
        for n in ("run-relay.sh", "run-publisher.sh"):
            shutil.copy(ROOT / "config/systemd" / n, inst / n)
        for f in (inst / "lib/relay.sh", inst / "webapp/app.py"):
            f.write_text(f.read_text(encoding="utf-8") + "\n# OLD PRODUCTION\n", encoding="utf-8", newline="\n")
        (self.root / "var/lib/bluestream/web").mkdir(parents=True)
        (self.root / "var/lib/bluestream/web/secret_key").write_text("k")
        (self.root / "var/lib/bluestream/web/admin.json").write_text("{}")
        (self.root / "etc/sudoers.d").mkdir(parents=True)
        (self.root / "etc/sudoers.d/bluestream-web").write_text("bluestream-web ALL=(root) NOPASSWD: /usr/local/lib/bluestream/web-ctl\n")
        (self.root / "etc/bluestream/relays").mkdir(parents=True)
        (self.root / "etc/bluestream/relays/dino.conf").write_text("RELAY_NAME=dino\n")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        (self.root / "etc/systemd/system/bluestream-publisher@.service").write_text("[Service]\n# old\n")
        (self.root / "etc/systemd/system/bluestream-web.service").write_text("[Service]\n")
        (self.bin / "systemctl").write_text(STUB_SYSTEMCTL, newline="\n")
        (self.bin / "visudo").write_text("#!/usr/bin/env bash\nexit 0\n", newline="\n")
        for f in ("systemctl", "visudo"):
            (self.bin / f).chmod(0o755)
        os.symlink(shutil.which("sleep"), self.bin / "ffmpeg")      # a process whose name is "ffmpeg"
        _Health.flag = str(self.st / "unhealthy")
        self.httpd = HTTPServer(("127.0.0.1", 0), _Health)
        (self.st / "port").write_text(str(self.httpd.server_address[1]))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.procs = []
        self.addCleanup(self.kill_procs)
        self.before = self.snapshot_prod()

    def kill_procs(self):
        for p in self.procs:
            p.kill()
            p.wait()

    def live_unit(self, unit, with_ffmpeg=True):
        cmd = ("%s/ffmpeg 300 & wait" % self.bin) if with_ffmpeg else "sleep 300 & wait"
        p = subprocess.Popen(["bash", "-c", cmd])
        self.procs.append(p)
        (self.st / "active" / unit).write_text("")
        (self.st / "pid" / unit).write_text(str(p.pid))
        deadline = time.time() + 5
        while time.time() < deadline and not subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True).stdout:
            time.sleep(0.05)

    def live_dino(self):
        self.live_unit("bluestream-relay@dino.service")
        self.live_unit("bluestream-publisher@relay:dino:youtube.service")

    def snapshot_prod(self):
        return {str(f.relative_to(self.root)): f.read_bytes() for f in sorted(self.root.rglob("*")) if f.is_file()}

    def env(self, **extra):
        e = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]), ST=str(self.st), ROOTFS=str(self.root),
                 BS_ACT_ROOT=str(self.root), BS_ACT_SRC=str(ROOT), BS_ACT_SKIP_GIT="1", BS_ACT_SKIP_IMPORT="1",
                 BS_ACT_BACKUP_ROOT=str(self.bk), BS_ACT_TMP=str(self.tmp / "work"), BS_ACT_POLL="0",
                 BS_ACT_SMOKE_CMD="echo 'PASS  smoke stub'; exit 0")
        e.update(extra)
        return e

    def run_deploy(self, *args, **env):
        return subprocess.run(["bash", str(CANARY / "studio_activate.sh"), *args], env=self.env(**env), capture_output=True, text=True, timeout=180)

    def run_rollback(self, *args, **env):
        return subprocess.run(["bash", str(CANARY / "studio_activate_rollback.sh"), *args], env=self.env(**env), capture_output=True, text=True, timeout=120)

    def restarted(self):
        f = self.st / "restarted"
        return f.read_text().split() if f.exists() else []

    def backups(self):
        return sorted(self.bk.glob("studio-activate-*"))

    def assert_untouched(self):
        self.assertEqual(self.snapshot_prod(), self.before)
        self.assertEqual(self.restarted(), [])
        self.assertFalse((self.st / "violations").exists())

    # ---- positive identification of the live stream: must block BEFORE any change ----
    def test_dry_run_changes_nothing(self):
        self.live_dino()
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN complete", r.stdout)
        self.assert_untouched()
        self.assertEqual(self.backups(), [])

    def test_blocks_when_the_live_relay_is_not_found(self):
        r = self.run_deploy("--execute")
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not be identified", r.stdout)
        self.assert_untouched()
        self.assertEqual(self.backups(), [])

    def test_blocks_when_the_publisher_is_not_found(self):
        self.live_unit("bluestream-relay@dino.service")
        r = self.run_deploy("--execute")
        self.assertEqual(r.returncode, 1)
        self.assertIn("publisher", r.stdout)
        self.assert_untouched()

    def test_blocks_when_a_unit_has_no_running_ffmpeg(self):
        self.live_unit("bluestream-relay@dino.service")
        self.live_unit("bluestream-publisher@relay:dino:youtube.service", with_ffmpeg=False)
        r = self.run_deploy("--execute")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no live FFmpeg", r.stdout)
        self.assert_untouched()

    # ---- backups are verified before production is modified ----
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read everything")
    def test_unverifiable_backup_aborts_before_any_modification(self):
        self.live_dino()
        victim = self.root / "usr/local/lib/bluestream/lib/relay.sh"
        victim.chmod(0)
        try:
            r = self.run_deploy("--execute")
        finally:
            victim.chmod(0o644)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("verified backup", r.stdout)
        self.assert_untouched()

    # ---- successful deployment ----
    def test_successful_deploy_restarts_only_the_dashboard_and_keeps_data(self):
        self.live_dino()
        r = self.run_deploy("--execute")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.restarted(), ["bluestream-web.service"])
        self.assertFalse((self.st / "violations").exists())
        inst = self.root / "usr/local/lib/bluestream"
        self.assertEqual((inst / "lib/relay.sh").read_bytes(), (ROOT / "lib/relay.sh").read_bytes())
        conf = (self.root / "etc/systemd/system/bluestream-web.service.d/50-studio.conf").read_text()
        self.assertIn("BLUESTREAM_STUDIO_ENABLED=1", conf)
        self.assertIn("ReadWritePaths=/var/lib/bluestream/studio", conf)
        now = self.snapshot_prod()
        for keep in ("var/lib/bluestream/web/secret_key", "var/lib/bluestream/web/admin.json", "etc/bluestream/relays/dino.conf"):
            self.assertEqual(now[keep], self.before[keep], keep)
        self.assertIn("Dino livestream:  Running", r.stdout)
        self.assertNotIn("vendor", r.stdout.lower())

    # ---- failure -> automatic, verified rollback ----
    def test_smoke_failure_rolls_back_verified_and_restarts_only_the_dashboard(self):
        self.live_dino()
        r = self.run_deploy("--execute", BS_ACT_SMOKE_CMD="echo 'FAIL  stub'; exit 1")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Rollback VERIFIED", r.stdout)
        self.assertIn("did NOT succeed", r.stdout)
        self.assertEqual(self.snapshot_prod(), self.before)
        self.assertEqual(set(self.restarted()), {"bluestream-web.service"})
        self.assertFalse((self.st / "violations").exists())

    def test_unhealthy_dashboard_after_restart_is_a_failed_rollback_never_a_success(self):
        self.live_dino()
        (self.st / "unhealthy-after-restart").write_text("")
        r = self.run_deploy("--execute")
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("AUTOMATIC ROLLBACK DID NOT COMPLETE", r.stdout)
        self.assertNotIn("Rollback VERIFIED", r.stdout)
        self.assertEqual(set(self.restarted()), {"bluestream-web.service"})

    def test_a_changed_live_stream_is_reported_as_interrupted(self):
        self.live_dino()
        r = self.run_deploy("--execute", BS_ACT_SMOKE_CMD="echo 7 > '%s/nr/bluestream-relay@dino.service'; echo 'PASS x'" % self.st)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("Interrupted", r.stdout)

    # ---- manual rollback ----
    def test_manual_rollback_restores_everything_and_verifies(self):
        self.live_dino()
        self.assertEqual(self.run_deploy("--execute").returncode, 0)
        r = self.run_rollback()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Rollback VERIFIED", r.stdout)
        self.assertEqual(self.snapshot_prod(), self.before)
        self.assertEqual(set(self.restarted()), {"bluestream-web.service"})

    def test_rollback_with_a_missing_backup_file_is_incomplete_never_success(self):
        self.live_dino()
        self.assertEqual(self.run_deploy("--execute").returncode, 0)
        bk = self.backups()[0]
        next(p for p in (bk / "files").rglob("relay.sh")).unlink()
        n = len(self.restarted())
        r = self.run_rollback()
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("ROLLBACK INCOMPLETE", r.stdout)
        self.assertNotIn("Rollback VERIFIED", r.stdout)
        self.assertFalse((bk / "rolled-back").exists())
        self.assertEqual(len(self.restarted()), n, "an incomplete rollback must not restart the dashboard")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_rollback_that_cannot_write_a_file_is_incomplete(self):
        self.live_dino()
        self.assertEqual(self.run_deploy("--execute").returncode, 0)
        target = self.root / "usr/local/lib/bluestream/lib"
        target.chmod(0o555)
        try:
            r = self.run_rollback()
        finally:
            target.chmod(0o755)
        self.assertEqual(r.returncode, 4, r.stdout)
        self.assertIn("FAILED to restore", r.stdout)
        self.assertNotIn("Rollback VERIFIED", r.stdout)


if __name__ == "__main__":
    unittest.main()
