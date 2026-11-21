"""bsunitd (the in-container unit manager) - real daemon, real shim CLIs, throwaway unit files. POSIX only (Docker/CI)."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNITD = REPO / "docker" / "unitd"
POSIX = sys.platform.startswith("linux")


@unittest.skipUnless(POSIX, "Linux only (process groups, unix sockets)")
class UnitdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.units, self.state, self.run, self.logs = t / "units", t / "state", t / "run", t / "logs"
        self.units.mkdir()
        self.env = dict(os.environ, BS_UNITD_UNITS=str(self.units), BS_UNITD_STATE=str(self.state), BS_UNITD_RUN=str(self.run),
                        BS_UNITD_LOGS=str(self.logs), BLUESTREAM_TEST_MARK="pass-through")
        self.daemon = None
        self.addCleanup(self.stop_daemon)

    # ---- plumbing
    def unit(self, name, body):
        (self.units / name).write_text(textwrap.dedent(body))

    def start_daemon(self):
        self.daemon = subprocess.Popen([sys.executable, str(UNITD / "bsunitd.py")], env=self.env, stderr=subprocess.PIPE, start_new_session=True)
        for _ in range(100):
            if (self.run / "sock").exists():
                return
            time.sleep(0.05)
        self.fail("daemon did not start")

    def stop_daemon(self):
        if self.daemon and self.daemon.poll() is None:
            self.daemon.send_signal(signal.SIGTERM)
            try:
                self.daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.daemon.pid, signal.SIGKILL)

    def ctl(self, *args, tool="systemctl"):
        r = subprocess.run([sys.executable, str(UNITD / tool), *args], env=self.env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def state_of(self, unit):
        return self.ctl("is-active", unit)[1]

    def wait_state(self, unit, want, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if self.state_of(unit) == want:
                return True
            time.sleep(0.1)
        return False

    # ---- tests
    def test_start_stop_and_exit_codes(self):
        self.unit("svc.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.start_daemon()
        self.assertEqual(self.ctl("is-active", "svc.service")[:2], (3, "inactive"))
        self.assertEqual(self.ctl("start", "svc.service")[0], 0)
        self.assertEqual(self.ctl("is-active", "svc.service")[:2], (0, "active"))
        self.assertEqual(self.ctl("stop", "svc.service")[0], 0)
        self.assertEqual(self.ctl("is-active", "svc.service")[:2], (3, "inactive"))
        rc, _, err = self.ctl("start", "missing.service")
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)

    def test_restart_policies_and_nrestarts(self):
        self.unit("fail.service", "[Unit]\nStartLimitBurst=50\nStartLimitIntervalSec=60\n[Service]\nExecStart=/bin/sh -c 'exit 1'\nRestart=on-failure\nRestartSec=0.2\n")
        self.unit("ok.service", "[Service]\nExecStart=/bin/sh -c 'exit 0'\nRestart=on-failure\nRestartSec=0.2\n")
        self.unit("always.service", "[Unit]\nStartLimitBurst=50\nStartLimitIntervalSec=60\n[Service]\nExecStart=/bin/sh -c 'exit 0'\nRestart=always\nRestartSec=0.2\n")
        self.start_daemon()
        for u in ("fail.service", "ok.service", "always.service"):
            self.ctl("start", u)
        time.sleep(2.0)
        n = lambda u: int(self.ctl("show", u, "-p", "NRestarts", "--value")[1] or 0)  # noqa: E731
        self.assertGreaterEqual(n("fail.service"), 3)                 # crashes are retried
        self.assertEqual(n("ok.service"), 0)                           # clean exit under on-failure is final
        self.assertEqual(self.state_of("ok.service"), "inactive")
        self.assertGreaterEqual(n("always.service"), 3)                # Restart=always retries even exit 0

    def test_start_limit_hit_then_reset_failed(self):
        self.unit("flap.service", "[Unit]\nStartLimitBurst=3\nStartLimitIntervalSec=30\n[Service]\nExecStart=/bin/sh -c 'exit 1'\nRestart=on-failure\nRestartSec=0.1\n")
        self.start_daemon()
        self.ctl("start", "flap.service")
        self.assertTrue(self.wait_state("flap.service", "failed", 6))
        self.assertEqual(self.ctl("show", "flap.service", "-p", "Result", "--value")[1], "start-limit-hit")
        self.assertEqual(self.ctl("reset-failed", "flap.service")[0], 0)
        self.assertEqual(self.state_of("flap.service"), "inactive")

    def test_bindsto_semantics_dependent_stops_with_its_source(self):
        self.unit("relay@.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.unit("pub@.service", "[Service]\nExecStart=/bin/sleep 60\n")
        d = self.units / "pub@a:b.service.d"
        d.mkdir()
        (d / "override.conf").write_text("[Unit]\nBindsTo=relay@a.service\nAfter=relay@a.service\n[Service]\nRestartSec=3\n")
        self.start_daemon()
        rc, _, err = self.ctl("start", "pub@a:b.service")                  # source not running: refused
        self.assertEqual(rc, 1)
        self.assertIn("not active", err)
        self.assertEqual(self.ctl("start", "relay@a.service")[0], 0)
        self.assertEqual(self.ctl("start", "pub@a:b.service")[0], 0)
        self.assertEqual(self.state_of("pub@a:b.service"), "active")
        self.ctl("stop", "relay@a.service")
        self.assertTrue(self.wait_state("pub@a:b.service", "inactive", 6), "publisher must stop with its source")

    def test_dependent_stops_when_source_crashes(self):
        self.unit("src.service", "[Service]\nExecStart=/bin/sh -c 'sleep 1; exit 1'\n")
        self.unit("dep.service", "[Unit]\nBindsTo=src.service\n[Service]\nExecStart=/bin/sleep 60\n")
        self.start_daemon()
        self.ctl("start", "src.service")
        self.ctl("start", "dep.service")
        self.assertTrue(self.wait_state("dep.service", "inactive", 8))
        self.assertEqual(self.state_of("src.service"), "failed")

    def test_templates_instances_dropins_and_environment(self):
        self.unit("job@.service", "[Service]\nExecStart=/bin/sh -c 'echo \"%i|$FOO|$BLUESTREAM_TEST_MARK\" > %i.out; sleep 30'\nEnvironment=FOO=base\n")
        d = self.units / "job@x-1.service.d"
        d.mkdir()
        (d / "o.conf").write_text("[Service]\nEnvironment=FOO=dropin\n")
        self.env["BS_UNITD_OUT"] = self.tmp.name
        self.start_daemon()
        self.unit("job@x-1.service", "[Service]\nExecStart=/bin/sh -c 'echo \"%i|$FOO|$BLUESTREAM_TEST_MARK\" > " + self.tmp.name + "/%i.out; sleep 30'\n")
        (self.units / "job@x-1.service").unlink()
        self.unit("job@.service", "[Service]\nExecStart=/bin/sh -c 'echo \"%i|$FOO|$BLUESTREAM_TEST_MARK\" > " + self.tmp.name + "/%i.out; sleep 30'\nEnvironment=FOO=base\n")
        self.assertEqual(self.ctl("start", "job@x-1.service")[0], 0)
        time.sleep(0.8)
        self.assertEqual((Path(self.tmp.name) / "x-1.out").read_text().strip(), "x-1|dropin|pass-through")   # %i, drop-in wins, env allow-list

    def test_enable_disable_persist_and_boot_starts_enabled_units(self):
        self.unit("auto.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.unit("manual.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.assertEqual(self.ctl("enable", "auto.service")[0], 0)             # works with NO daemon (image build)
        self.assertEqual(self.ctl("is-enabled", "auto.service")[:2], (0, "enabled"))
        self.assertEqual(self.ctl("is-enabled", "manual.service")[:2], (1, "disabled"))
        self.assertEqual(self.ctl("enable", "ghost.service")[0], 1)
        self.start_daemon()                                                    # "container boot"
        self.assertTrue(self.wait_state("auto.service", "active", 5))
        self.assertEqual(self.state_of("manual.service"), "inactive")
        self.ctl("disable", "auto.service")
        self.assertEqual(self.ctl("is-enabled", "auto.service")[1], "disabled")

    def test_stop_kills_the_whole_process_group_and_deliberate_stop_is_not_restarted(self):
        self.unit("tree.service", "[Service]\nExecStart=/bin/sh -c 'sleep 300 & sleep 300 & wait'\nRestart=always\nRestartSec=0.1\n")
        self.start_daemon()
        self.ctl("start", "tree.service")
        time.sleep(0.6)
        pid = int(self.ctl("show", "tree.service", "-p", "MainPID", "--value")[1])
        self.assertGreater(pid, 1)
        pgid_members = subprocess.run(["pgrep", "-g", str(pid)], capture_output=True, text=True).stdout.split()
        self.assertGreaterEqual(len(pgid_members), 3)
        self.ctl("stop", "tree.service")
        time.sleep(0.8)
        self.assertEqual(subprocess.run(["pgrep", "-g", str(pid)], capture_output=True, text=True).stdout.split(), [])
        time.sleep(0.6)
        self.assertEqual(self.state_of("tree.service"), "inactive")           # Restart=always must not resurrect a deliberate stop

    def test_journal_shim_formats_and_filters(self):
        self.unit("log.service", "[Service]\nExecStart=/bin/sh -c 'echo out_time_ms=1000; echo hello; echo out_time_ms=2000; sleep 30'\n")
        self.start_daemon()
        self.ctl("start", "log.service")
        time.sleep(0.8)
        rc, out, _ = self.ctl("-u", "log.service", "-n", "10", "-o", "short-unix", "--no-pager", tool="journalctl")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(l.split()[0].replace(".", "").isdigit() for l in lines))
        self.assertTrue([l for l in lines if " out_time_ms=1000" in l])          # the exact shape publisher_last_progress_age parses
        self.assertEqual(len(self.ctl("-u", "log.service", "-n", "1", tool="journalctl")[1].splitlines()), 1)
        self.assertEqual(self.ctl("-u", "log.service", "--since", "@%d" % (time.time() + 100), tool="journalctl")[1], "")

    def test_show_properties_and_timestamp_are_parseable(self):
        self.unit("p.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.start_daemon()
        self.ctl("start", "p.service")
        ts = self.ctl("show", "p.service", "-p", "ActiveEnterTimestamp", "--value")[1]
        self.assertEqual(subprocess.run(["date", "-d", ts, "+%s"], capture_output=True, text=True).returncode, 0, ts)
        self.assertEqual(self.ctl("show", "p.service", "-p", "ActiveState,SubState")[1].splitlines(), ["ActiveState=active", "SubState=running"])

    def test_reload_uses_execreload_and_list_units(self):
        mark = Path(self.tmp.name) / "reloaded"
        self.unit("web.service", "[Service]\nExecStart=/bin/sleep 60\nExecReload=/bin/sh -c 'touch %s'\n" % mark)
        self.start_daemon()
        self.ctl("start", "web.service")
        self.assertEqual(self.ctl("reload", "web.service")[0], 0)
        self.assertTrue(mark.exists())
        out = self.ctl("list-units", "--all", "--plain", "--no-legend", "web*")[1]
        self.assertIn("web.service loaded active running", out)
        self.unit("noreload.service", "[Service]\nExecStart=/bin/sleep 60\n")
        self.ctl("start", "noreload.service")
        self.assertEqual(self.ctl("reload", "noreload.service")[0], 1)

    def test_unit_names_are_validated_and_the_socket_is_private(self):
        self.start_daemon()
        for bad in ("../etc/passwd", "a b.service", "x;y.service", "", "a/b"):
            self.assertEqual(self.ctl("start", bad)[0], 1, bad)
        self.assertEqual(oct(os.stat(self.run).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(self.run / "sock").st_mode & 0o777), "0o600")

    def test_logs_are_bounded_by_rotation(self):
        self.env["BS_UNITD_LOG_MAX"] = "20000"
        self.unit("chatty.service", "[Service]\nExecStart=/bin/sh -c 'i=0; while [ $i -lt 4000 ]; do echo line-$i-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; i=$((i+1)); done; sleep 30'\n")
        self.start_daemon()
        self.ctl("start", "chatty.service")
        time.sleep(2.0)
        total = sum(p.stat().st_size for p in self.logs.glob("chatty*"))
        self.assertLess(total, 80000)

    def test_ctl_without_daemon_is_a_noop_only_when_explicitly_optional(self):
        self.unit("x.service", "[Service]\nExecStart=/bin/sleep 1\n")
        self.assertEqual(self.ctl("start", "x.service")[0], 1)
        self.env["BS_UNITD_OPTIONAL"] = "1"
        self.assertEqual(self.ctl("start", "x.service")[0], 0)
        self.assertEqual(self.ctl("is-active", "x.service")[:2], (3, "inactive"))


class StaticTests(unittest.TestCase):
    def test_sources_compile(self):
        for f in ("bsunitd.py", "systemctl", "journalctl"):
            compile((UNITD / f).read_text(), f, "exec")


if __name__ == "__main__":
    unittest.main()
