"""The staging preflight must refuse production-like hosts and under-spec machines."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "tools" / "staging_preflight.sh").as_posix()
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "needs bash")
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        (self.root / "etc").mkdir(parents=True)
        (self.root / "etc" / "bluestream-staging").write_text("STAGING-OK\n")
        (self.root / "etc" / "os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        self.env = {"PATH": os.environ["PATH"], "BS_PF_ROOT": self.root.as_posix(), "BS_PF_CPUS": "4",
                    "BS_PF_MEM_KB": "8000000", "BS_PF_FREE_KB": "60000000", "BS_PF_HOSTNAME": "studio-staging-1",
                    "BS_PF_REPORT_DIR": (Path(self.tmp.name) / "rep").as_posix()}

    def run_pf(self, *args, **over):
        env = dict(self.env, **over)
        r = subprocess.run([BASH, SCRIPT, *args], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def test_good_staging_box_passes(self):
        rc, out = self.run_pf("--snapshot-id", "snap-2026-11-21")
        self.assertEqual(rc, 0, out)
        self.assertIn("ALL CHECKS PASSED", out)

    def test_refuses_without_the_operator_marker(self):
        (self.root / "etc" / "bluestream-staging").unlink()
        rc, out = self.run_pf("--snapshot-id", "snap-1")
        self.assertEqual(rc, 1)
        self.assertIn("missing staging marker", out)

    def test_refuses_the_production_hostname_even_with_the_marker(self):
        rc, out = self.run_pf("--snapshot-id", "snap-1", BS_PF_HOSTNAME="vmi2980718")
        self.assertEqual(rc, 1)
        self.assertIn("PRODUCTION host", out)

    def test_refuses_a_cloudpanel_server(self):
        (self.root / "home" / "clp").mkdir(parents=True)
        rc, out = self.run_pf("--snapshot-id", "snap-1")
        self.assertEqual(rc, 1)
        self.assertIn("CloudPanel detected", out)

    def test_refuses_underspec_machines_and_wrong_os_and_low_disk(self):
        for over, text in ((dict(BS_PF_CPUS="2"), "CPU count 2"), (dict(BS_PF_MEM_KB="3900000"), "not an 8 GB"),
                           (dict(BS_PF_FREE_KB="1000"), "free disk")):
            rc, out = self.run_pf("--snapshot-id", "snap-1", **over)
            self.assertEqual(rc, 1, text)
            self.assertIn(text, out)
        (self.root / "etc" / "os-release").write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
        rc, out = self.run_pf("--snapshot-id", "snap-1")
        self.assertEqual(rc, 1)
        self.assertIn("Ubuntu 24.04", out)

    def test_refuses_without_a_recorded_snapshot(self):
        rc, out = self.run_pf()
        self.assertEqual(rc, 1)
        self.assertIn("no snapshot recorded", out)
        self.assertEqual(self.run_pf("--snapshot-id", "x")[0], 1)       # too short to be a real id

    def test_script_never_touches_ssh_or_firewall_or_services(self):
        text = SCRIPT and Path(SCRIPT).read_text()
        for forbidden in ("ufw ", "iptables", "systemctl stop", "systemctl restart", "systemctl disable",
                          "sshd_config\n", "apt ", "rm -rf", "chmod", "chown"):
            self.assertNotIn(forbidden, text, forbidden)


if __name__ == "__main__":
    unittest.main()
