"""systemd units / installer consistency for Studio: accounts, permissions, isolation, defaults.

These are static + CI-level checks. They do NOT prove the units start on a real host; that
is staging gate B/E (docs/bluestream/RELEASE_CHECKLIST.md)."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNITS = REPO / "config" / "systemd"


def read(p):
    return Path(p).read_text(encoding="utf-8", errors="replace")


def section_values(text, section, key):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
        elif cur == section and line.startswith(key + "="):
            out.append(line.split("=", 1)[1])
    return out


class AccountTests(unittest.TestCase):
    def test_installer_defines_and_creates_the_service_account(self):
        common, install = read(REPO / "lib" / "common.sh"), read(REPO / "install.sh")
        user = re.search(r'^BLUESTREAM_USER="([^"]+)"', common, re.M).group(1)
        group = re.search(r'^BLUESTREAM_GROUP="([^"]+)"', common, re.M).group(1)
        self.assertEqual((user, group), ("bluestream-relay", "bluestream-relay"))
        self.assertIn('groupadd --system "$BLUESTREAM_GROUP"', install)
        self.assertIn('useradd --system', install)
        self.assertIn('"$BLUESTREAM_USER"', install)

    def test_browser_unit_runs_as_the_provisioned_unprivileged_account(self):
        common = read(REPO / "lib" / "common.sh")
        user = re.search(r'^BLUESTREAM_USER="([^"]+)"', common, re.M).group(1)
        group = re.search(r'^BLUESTREAM_GROUP="([^"]+)"', common, re.M).group(1)
        unit = read(UNITS / "bluestream-browser@.service")
        self.assertEqual(section_values(unit, "Service", "User"), [user])
        self.assertEqual(section_values(unit, "Service", "Group"), [group])
        self.assertNotIn("User=root", unit)
        self.assertNotIn("User=bluestream\n", unit)       # the old, never-created account

    def test_every_unit_user_is_an_account_the_installer_creates(self):
        known = {"bluestream-relay", "bluestream-web", "root"}
        web = read(REPO / "lib" / "webconsole.sh")
        self.assertIn('BLUESTREAM_WEB_USER="bluestream-web"', web)
        for f in UNITS.glob("*.service*"):
            for u in section_values(read(f), "Service", "User") + section_values(read(f), "Service", "Group"):
                self.assertIn(u, known, "%s names an unprovisioned account %r" % (f.name, u))

    def test_run_dir_is_owned_by_that_account_and_not_world_readable(self):
        install = read(REPO / "install.sh")
        self.assertIn('chown "$BLUESTREAM_USER":"$BLUESTREAM_GROUP" "$BLUESTREAM_RUN_DIR"', install)
        self.assertIn('chmod 0750 "$BLUESTREAM_RUN_DIR"', install)
        unit = read(UNITS / "bluestream-browser@.service")
        self.assertEqual(section_values(unit, "Service", "ReadWritePaths"), ["/var/lib/bluestream/run"])


class BrowserUnitIsolationTests(unittest.TestCase):
    def setUp(self):
        self.unit = read(UNITS / "bluestream-browser@.service")

    def test_bound_to_its_relay_and_kills_the_whole_tree(self):
        self.assertEqual(section_values(self.unit, "Unit", "BindsTo"), ["bluestream-relay@%i.service"])
        self.assertEqual(section_values(self.unit, "Service", "KillMode"), ["control-group"])

    def test_resource_caps_present(self):
        for key in ("MemoryMax", "MemoryHigh", "CPUQuota", "TasksMax", "LimitNOFILE"):
            self.assertTrue(section_values(self.unit, "Service", key), key)

    def test_kernel_ip_deny_covers_loopback_private_and_metadata_ranges(self):
        deny = " ".join(section_values(self.unit, "Service", "IPAddressDeny"))
        for rng in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                    "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"):
            self.assertIn(rng, deny)
        self.assertEqual(section_values(self.unit, "Service", "IPAddressAllow"), ["127.0.0.53"])

    def test_hardening_present_but_does_not_break_chromium(self):
        for k in ("NoNewPrivileges", "ProtectSystem", "PrivateTmp", "ProtectHome", "RestrictAddressFamilies"):
            self.assertTrue(section_values(self.unit, "Service", k), k)
        # Chromium's sandbox needs user namespaces and V8 needs JIT: these must NOT be set.
        for bad in ("RestrictNamespaces", "MemoryDenyWriteExecute", "PrivateUsers", "SystemCallFilter", "ProtectProc"):
            self.assertEqual(section_values(self.unit, "Service", bad), [], bad)
        self.assertIn("AF_UNIX", section_values(self.unit, "Service", "RestrictAddressFamilies")[0])

    def test_unit_is_installed_root_owned_world_readable_not_writable(self):
        install = read(REPO / "install.sh")
        self.assertRegex(install, r'install -o root -g root -m 0644 "\$BS_ROOT/config/systemd/bluestream-browser@\.service"'
                                  r' /etc/systemd/system/bluestream-browser@\.service')

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_accepts_the_unit_syntax(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(UNITS / "bluestream-browser@.service", d)
            r = subprocess.run(["systemd-analyze", "verify", d + "/bluestream-browser@.service"],
                               capture_output=True, text=True, timeout=30)
            text = r.stdout + r.stderr
            for bad in ("Unknown key", "Unknown section", "Invalid argument", "Failed to parse", "Unknown lvalue"):
                self.assertNotIn(bad, text, text)


class DisabledByDefaultTests(unittest.TestCase):
    def test_no_shipped_unit_or_script_turns_the_browser_on(self):
        install = read(REPO / "install.sh")
        self.assertNotIn("BLUESTREAM_STUDIO_BROWSER=1", install)
        for f in list(UNITS.glob("*.template")) + [UNITS / "bluestream-relay@.service", UNITS / "bluestream-publisher@.service"]:
            self.assertNotIn("BLUESTREAM_STUDIO_BROWSER", read(f), f.name)
        # the only unit that sets it is the worker itself, which is never started unless the operator opted in
        self.assertNotIn("WantedBy", read(UNITS / "bluestream-browser@.service"))
        run_relay = read(UNITS / "run-relay.sh")
        self.assertIn('[ "${BLUESTREAM_STUDIO_BROWSER:-}" = "1" ]', run_relay)

    def test_gate_is_closed_without_the_flag(self):
        import os
        from unittest import mock
        from webapp.studio import browsercap
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLUESTREAM_STUDIO_BROWSER", None)
            self.assertFalse(browsercap.enabled())
            self.assertFalse(browsercap.usable()[0])

    def test_console_does_not_offer_browser_overlays_by_default(self):
        import os
        from unittest import mock
        from webapp.studio import browsercap
        from webapp.studio.model import KINDS
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BLUESTREAM_STUDIO_BROWSER", None)
            ok, _ = browsercap.usable()
            self.assertNotIn("browser", [k for k in KINDS if k != "browser" or ok])


if __name__ == "__main__":
    unittest.main()
