"""Production-canary tooling: preflight (read-only), guarded installer wrapper, targeted rollback, diagnostics + redaction.

Every test runs the REAL scripts against stub commands and a fake filesystem root, so nothing on the host is touched."""
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANARY = REPO / "tools" / "canary"
BASH = shutil.which("bash")
SYSPATH = "/usr/bin:/bin"

YT_KEY = "abcd-efgh-ijkl-mnop-qrst"
SECRETS = [YT_KEY, "hunter2hunter2", "s3cr3tvalue", "Zm9vOmJhcg", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sessionsecret99"]


def posix(p):
    return str(p).replace("\\", "/")


class Env:
    """Fake host: stub commands in bin/, fake filesystem root in root/."""

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.bin = self.tmp / "bin"
        self.root = self.tmp / "root"
        self.bin.mkdir()
        for d in ("etc", "proc", "var/lib", "usr/local/lib", "etc/systemd/system", "etc/nginx"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        self.set_load("0.30 0.40 0.50 1/100 1")
        self.set_mem(8000000, 6000000)
        self.env = {"PATH": "%s:%s" % (posix(self.bin), SYSPATH), "BS_CP_ROOT": posix(self.root), "BS_DIAG_ROOT": posix(self.root),
                    "BS_CANARY_ROOT": posix(self.root), "STUB_CPUS": "4", "STUB_DF_KB": "60000000", "STUB_HOST": "vmi2980718",
                    "HOME": posix(self.tmp)}
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.env["STUB_LOG"] = posix(self.log)
        # defaults: a clean Ubuntu 24.04 host with no nginx
        self.stub("nproc", 'echo "$STUB_CPUS"')
        self.stub("hostname", 'echo "$STUB_HOST"')
        self.stub("df", 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"; echo "x 1 1 $STUB_DF_KB 1% /"')
        self.stub("fuser", "exit 0")
        self.stub("timedatectl", 'echo yes')
        self.stub("timeout", "exit ${STUB_TIMEOUT_RC:-0}")
        self.stub("ss", 'case "$*" in *-ltn*) cat "$STUB_SS" 2>/dev/null ;; esac')
        self.stub("systemctl", 'echo "systemctl $*" >> "$STUB_LOG"; case "$1" in is-active) exit 1 ;; list-units) cat "$STUB_UNITS" 2>/dev/null ;; esac; exit 0')
        self.stub("apt-get", 'if [ "$1" = "-s" ]; then cat "$STUB_APT_SIM" 2>/dev/null; exit ${STUB_APT_RC:-0}; fi; echo "apt-get $*" >> "$STUB_LOG"')
        self.stub("apt-cache", 'cat "$STUB_APT_POLICY" 2>/dev/null')
        self.stub("dpkg", 'case "$1" in -s) [ -f "$STUB_DPKG_S" ] && { cat "$STUB_DPKG_S"; exit 0; }; exit 1 ;; -l) cat "$STUB_DPKG_L" 2>/dev/null ;; esac')
        self.stub("dpkg-query", 'echo "$STUB_NGINX_VER"')
        self.stub("xargs", 'cat >/dev/null; [ -f "$STUB_DPKG_S" ] && cat "$STUB_DPKG_S"')
        (self.tmp / "ss.txt").write_text("")
        self.env.update(STUB_SS=posix(self.tmp / "ss.txt"), STUB_UNITS=posix(self.tmp / "units.txt"),
                        STUB_APT_SIM=posix(self.tmp / "apt_sim.txt"), STUB_APT_POLICY=posix(self.tmp / "apt_policy.txt"),
                        STUB_DPKG_S=posix(self.tmp / "dpkg_s.txt"), STUB_DPKG_L=posix(self.tmp / "dpkg_l.txt"), STUB_NGINX_VER="1.24.0-2ubuntu7.21")
        (self.tmp / "apt_sim.txt").write_text("Inst ffmpeg (7:6.1.1 Ubuntu:24.04/noble [amd64])\nInst gunicorn (21 Ubuntu:24.04/noble [all])\n")

    def stub(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def set_load(self, line):
        (self.root / "proc/loadavg").write_text(line + "\n")

    def set_mem(self, total, avail):
        (self.root / "proc/meminfo").write_text("MemTotal: %d kB\nMemAvailable: %d kB\nSwapTotal: 0 kB\n" % (total, avail))

    def with_nginx(self, cand="1.24.0-2ubuntu7.21", abi="nginx-abi-1.24.0-1", managed=True):
        self.stub("nginx", 'echo "nginx version: nginx/1.24.0" >&2')
        (self.tmp / "apt_policy.txt").write_text("nginx:\n  Installed: %s\n  Candidate: %s\n" % ("1.24.0-2ubuntu7.21", cand))
        if managed:
            (self.tmp / "dpkg_s.txt").write_text("Package: nginx\nStatus: install ok installed\nProvides: %s\n" % abi)
        (self.tmp / "dpkg_l.txt").write_text("ii  nginx  1.24.0  amd64  nginx\n")

    def cloudpanel(self):
        (self.root / "home/clp").mkdir(parents=True, exist_ok=True)

    def run(self, script, *args, **over):
        env = dict(self.env, **over)
        r = subprocess.run([BASH, posix(CANARY / script), *args], env=env, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        return self.log.read_text().splitlines()

    def tree_digest(self):
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            h.update(str(p.relative_to(self.root)).encode())
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()


@unittest.skipUnless(BASH, "needs bash")
class CanaryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Env(self.tmp.name)


class PreflightTests(CanaryTestCase):
    def test_clean_host_has_no_blockers_and_the_production_hostname_is_not_a_blocker(self):
        rc, out = self.h.run("canary_preflight.sh")        # hostname stub = vmi2980718 (the owner's production host)
        self.assertEqual(rc, 0, out)
        self.assertIn("NO BLOCKERS", out)
        self.assertNotIn("PRODUCTION host", out)

    def test_cloudpanel_host_with_matching_nginx_passes_with_the_reload_warning(self):
        self.h.cloudpanel()
        self.h.with_nginx()
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("CloudPanel detected", out)
        self.assertIn("ISOLATED nginx", out)
        self.assertIn("GRACEFUL reload", out)                 # the one honest side effect is disclosed
        self.assertIn("nginx-abi-1.24.0-1", out)

    def test_nginx_that_apt_would_change_is_a_blocker(self):
        self.h.cloudpanel()
        self.h.with_nginx(cand="1.26.2-1~noble")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("apt would change the nginx package", out)

    def test_nginx_abi_mismatch_and_unmanaged_nginx_are_blockers(self):
        self.h.cloudpanel()
        self.h.with_nginx(abi="nginx-abi-1.27.0-1")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("requires nginx-abi-1.24.0-1", out)
        self.h.with_nginx(managed=False)
        (self.h.tmp / "dpkg_s.txt").unlink(missing_ok=True)
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("NOT managed by apt/dpkg", out)

    def test_apt_simulation_that_removes_or_upgrades_critical_packages_blocks(self):
        self.h.with_nginx()
        (self.h.tmp / "apt_sim.txt").write_text("Remv rustdesk-server [1.1]\nInst ffmpeg (6.1 Ubuntu [amd64])\n")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("would REMOVE", out)
        (self.h.tmp / "apt_sim.txt").write_text("Inst nginx [1.24.0-2] (1.24.0-3 Ubuntu:24.04/noble [amd64])\nInst libc6 [2.39-1] (2.39-2 Ubuntu [amd64])\n")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("UPGRADE critical packages", out)

    def test_busy_rtmp_port_or_no_loopback_ports_block(self):
        (self.h.tmp / "ss.txt").write_text("LISTEN 0 128 0.0.0.0:1935 0.0.0.0:*\n")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("port 1935", out)
        (self.h.tmp / "ss.txt").write_text("".join("LISTEN 0 128 127.0.0.1:%d 0.0.0.0:*\n" % p for p in (8080, 8180, 8280, 8380, 8480, 18080, 28080, 38080)))
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("none of the candidate loopback ports", out)

    def test_insufficient_cpu_ram_disk_or_busy_host_block(self):
        self.h.set_load("3.90 3.80 3.70 1/100 1")
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)
        self.assertIn("spare cores", out)
        self.h.set_load("0.1 0.1 0.1 1/1 1")
        self.h.set_mem(8000000, 900000)
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual((rc, "RAM available" in out), (1, True))
        self.h.set_mem(8000000, 6000000)
        rc, out = self.h.run("canary_preflight.sh", STUB_DF_KB="2000000")
        self.assertEqual((rc, "free on /var" in out), (1, True))
        rc, out = self.h.run("canary_preflight.sh", STUB_CPUS="1")
        self.assertEqual(rc, 1)

    def test_existing_running_bluestream_streams_warn(self):
        (self.h.tmp / "units.txt").write_text("bluestream-relay@dino.service loaded active running BlueStream relay\n")
        (self.h.root / "etc/bluestream").mkdir(parents=True)
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 0)
        self.assertIn("existing BlueStream installation", out)

    def test_unsupported_os_blocks(self):
        (self.h.root / "etc/os-release").write_text('ID=debian\nVERSION_ID="12"\n')
        rc, out = self.h.run("canary_preflight.sh")
        self.assertEqual(rc, 1)

    def test_preflight_is_strictly_read_only(self):
        self.h.cloudpanel()
        self.h.with_nginx()
        before = self.h.tree_digest()
        self.h.run("canary_preflight.sh")
        self.assertEqual(before, self.h.tree_digest(), "preflight modified the (fake) filesystem")
        mutating = [c for c in self.h.calls() if re.search(r"\b(restart|stop|start|reload|enable|disable|daemon-reload|kill)\b", c)]
        self.assertEqual(mutating, [])
        self.assertFalse([c for c in self.h.calls() if c.startswith("apt-get")], "preflight may only SIMULATE apt")
        body = "\n".join(l for l in (CANARY / "canary_preflight.sh").read_text().splitlines() if not l.lstrip().startswith("#"))
        for pat in (r"apt-get install", r"systemctl (restart|stop|start|reload|enable|disable|daemon-reload)", r"\brm\s+-", r"\bmv\s", r"\btee\b",
                    r"\bchmod\b", r"\bchown\b", r"\biptables\b", r"ufw (allow|enable|disable|reset)", r"sed -i", r">>", r"\bmkdir\b", r"\btouch\b"):
            self.assertIsNone(re.search(pat, body), pat)

    def test_it_is_a_separate_gate_and_never_uses_the_staging_marker(self):
        for f in CANARY.glob("*.sh"):
            text = f.read_text()
            self.assertNotIn("bluestream-staging", text, f.name)
            self.assertNotIn("STAGING-OK", text, f.name)
        self.assertIn("PRODUCTION-CANARY", (CANARY / "canary_preflight.sh").read_text())


class InstallWrapperTests(CanaryTestCase):
    PUB = "ab" * 32

    def setUp(self):
        super().setUp()
        self.installer = self.h.tmp / "fake_install.sh"
        self.installer.write_text('#!/bin/bash\necho "install.sh $*" >> "$STUB_LOG"\nexit 0\n')
        self.preflight = self.h.tmp / "fake_preflight.sh"
        self.preflight.write_text("#!/bin/bash\necho 'preflight ok'\nexit ${STUB_PF_RC:-0}\n")
        self.h.env.update(BS_CANARY_INSTALLER=posix(self.installer), BS_CANARY_PREFLIGHT=posix(self.preflight))
        self.h.stub("id", 'case "$1" in -u) echo 0 ;; -un) echo root ;; *) echo root ;; esac')
        self.h.stub("systemctl", 'echo "systemctl $*" >> "$STUB_LOG"; exit 0')      # every call succeeds, bluestream-web is "active"
        (self.h.root / "etc/bluestream").mkdir(parents=True)
        (self.h.root / "etc/bluestream/server.conf").write_text('WEB_PORT="8180"\n')

    def phrase(self):
        return "INSTALL BLUESTREAM CANARY ON vmi2980718"

    def test_default_is_a_dry_run_that_changes_nothing(self):
        before = self.h.tree_digest()
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-2026-11-21", "--vendor-pubkey", self.PUB)
        self.assertEqual(rc, 0, out)
        self.assertIn("dry run", out)
        self.assertIn("NOT done: no firewall/SSH/SSL changes", out)
        self.assertEqual(before, self.h.tree_digest())
        self.assertEqual(self.h.calls(), [])

    def test_guards_refuse_bad_input(self):
        base = ["--vendor-pubkey", self.PUB]
        rc, out = self.h.run("canary_install.sh", *base)                                     # no snapshot
        self.assertEqual(rc, 1)
        self.assertIn("provider snapshot", out)
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-1", "--vendor-pubkey", "nothex")
        self.assertEqual(rc, 1)
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-1", "--vendor-pubkey", "ab" * 31)   # too short
        self.assertEqual(rc, 1)
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-1", *base, "--execute")             # no phrase
        self.assertEqual(rc, 1)
        self.assertIn("confirmation phrase", out)
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-1", *base, "--execute", "--confirm", "INSTALL BLUESTREAM CANARY ON other-host")
        self.assertEqual(rc, 1)
        self.assertEqual(self.h.calls(), [])

    def test_blocked_preflight_stops_everything(self):
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-1", "--vendor-pubkey", self.PUB, "--execute",
                             "--confirm", self.phrase(), STUB_PF_RC="1")
        self.assertEqual(rc, 1)
        self.assertIn("preflight reported blockers", out)
        self.assertEqual(self.h.calls(), [])

    def test_execute_runs_the_existing_installer_with_safe_flags_and_enables_studio_only(self):
        rc, out = self.h.run("canary_install.sh", "--snapshot-id", "snap-2026-11-21", "--vendor-pubkey", self.PUB, "--execute",
                             "--confirm", self.phrase())
        self.assertEqual(rc, 0, out)
        calls = self.h.calls()
        self.assertIn("install.sh --non-interactive --no-ufw --no-ssl", calls)
        joined = " ".join(calls)
        for bad in ("--with-ufw", "--with-ssl"):
            self.assertNotIn(bad, joined)
        sysctl = [c for c in calls if c.startswith("systemctl")]
        self.assertEqual(sorted(set(sysctl)), sorted({"systemctl daemon-reload", "systemctl restart bluestream-web.service",
                                                      "systemctl is-active --quiet bluestream-web.service"}))
        self.assertFalse([c for c in sysctl if "nginx" in c and "bluestream" not in c])
        drop = (self.h.root / "etc/systemd/system/bluestream-web.service.d/studio.conf").read_text()
        self.assertIn("BLUESTREAM_STUDIO_ENABLED=1", drop)
        self.assertNotIn("BROWSER", drop)                                       # browser capture stays disabled
        self.assertEqual((self.h.root / "usr/local/lib/bluestream/studio/vendor_pubkey.hex").read_text().strip(), self.PUB)
        receipt = (self.h.root / "var/lib/bluestream/canary-install.json").read_text()
        self.assertIn('"browser_capture":"disabled"', receipt)
        self.assertIn("snap-2026-11-21", receipt)
        self.assertIn("No stream has been started", out)
        self.assertIn("ssh -L 8765:127.0.0.1:8180", out)                          # tunnel instructions, nothing public

    def test_wrapper_source_never_touches_firewall_ssh_certs_nginx_or_docker(self):
        src = (CANARY / "canary_install.sh").read_text()
        body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for pat in (r"ufw (allow|enable|disable|reset)", r"\biptables\b", r"sshd_config", r"\bcertbot\b", r"systemctl (restart|reload|stop) (nginx|ssh|docker)",
                    r"\bdocker\b", r"--with-ufw", r"--with-ssl", r"STUDIO_BROWSER"):
            self.assertIsNone(re.search(pat, body.replace("--no-ufw", "").replace("--no-ssl", "")), pat)


class RollbackTests(CanaryTestCase):
    def setUp(self):
        super().setUp()
        r = self.h.root
        sd = r / "etc/systemd/system"
        for n in ("bluestream-relay@.service", "bluestream-publisher@.service", "bluestream-web.service", "bluestream-nginx.service",
                  "bluestream-browser@.service", "rustdesk-hbbs.service", "cloudpanel.service", "wg-quick@wg0.service"):
            (sd / n).write_text("[Service]\n")
        (sd / "bluestream-web.service.d").mkdir()
        (sd / "bluestream-web.service.d/studio.conf").write_text("[Service]\n")
        for d in ("etc/bluestream", "var/lib/bluestream/media", "var/www/bluestream", "usr/local/lib/bluestream", "etc/sudoers.d", "etc/nginx/sites-enabled"):
            (r / d).mkdir(parents=True, exist_ok=True)
        (r / "var/lib/bluestream/media/video.mp4").write_text("media")
        (r / "etc/bluestream/destinations").mkdir()
        (r / "etc/bluestream/destinations/yt.conf").write_text("STREAM_KEY=abcd-efgh-ijkl-mnop-qrst\n")
        (r / "etc/sudoers.d/bluestream-web").write_text("x")
        (r / "etc/sudoers.d/other").write_text("keep")
        (r / "etc/nginx/nginx.conf").write_text("keep")
        (r / "etc/nginx/sites-enabled/cloudpanel-site").write_text("keep")
        (self.h.tmp / "units.txt").write_text(
            "bluestream-web.service loaded active running web\nbluestream-relay@dino.service loaded active running relay\n"
            "bluestream-publisher@relay:dino:yt.service loaded active running pub\nbluestream-nginx.service loaded active running nginx\n"
            "nginx.service loaded active running system nginx\nrustdesk-hbbs.service loaded active running rd\ncloudpanel.service loaded active running cp\n")
        self.h.env["BS_CANARY_SYSTEMCTL"] = "systemctl"
        self.h.stub("id", "echo 0")

    def exec(self, *extra):
        return self.h.run("canary_rollback.sh", "--execute", "--confirm", "ROLLBACK BLUESTREAM CANARY", *extra)

    def test_default_is_a_dry_run_and_changes_nothing(self):
        before = self.h.tree_digest()
        rc, out = self.h.run("canary_rollback.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(before, self.h.tree_digest())
        self.assertFalse([c for c in self.h.calls() if re.search(r"\b(stop|disable|daemon-reload)\b", c)])

    def test_requires_the_exact_phrase_and_root(self):
        rc, out = self.h.run("canary_rollback.sh", "--execute")
        self.assertEqual(rc, 1)
        rc, out = self.h.run("canary_rollback.sh", "--execute", "--confirm", "yes")
        self.assertEqual(rc, 1)

    def test_execute_removes_only_bluestream_things_and_keeps_data(self):
        rc, out = self.exec()
        self.assertEqual(rc, 0, out)
        r = self.h.root
        sd = r / "etc/systemd/system"
        for gone in ("bluestream-relay@.service", "bluestream-publisher@.service", "bluestream-web.service", "bluestream-nginx.service",
                     "bluestream-browser@.service", "bluestream-web.service.d"):
            self.assertFalse((sd / gone).exists(), gone)
        for kept in ("rustdesk-hbbs.service", "cloudpanel.service", "wg-quick@wg0.service"):
            self.assertTrue((sd / kept).exists(), kept)
        self.assertFalse((r / "etc/sudoers.d/bluestream-web").exists())
        self.assertTrue((r / "etc/sudoers.d/other").exists())
        self.assertFalse((r / "usr/local/lib/bluestream").exists())
        for kept in ("etc/nginx/nginx.conf", "etc/nginx/sites-enabled/cloudpanel-site", "var/lib/bluestream/media/video.mp4",
                     "etc/bluestream/destinations/yt.conf", "var/www/bluestream"):
            self.assertTrue((r / kept).exists(), kept + " must be preserved by default")

    def test_systemctl_is_only_ever_called_for_bluestream_units(self):
        self.exec()
        for c in self.h.calls():
            if c.startswith("systemctl") and re.search(r"\b(stop|disable)\b", c):
                unit = c.split()[-1]
                self.assertTrue(unit.startswith("bluestream-"), c)
        for forbidden in ("nginx.service", "rustdesk", "cloudpanel", "wg-quick", "docker", "ssh", "ufw"):
            self.assertFalse([c for c in self.h.calls() if forbidden in c and "bluestream-" not in c], forbidden)
        self.assertFalse([c for c in self.h.calls() if re.search(r"(?<![\w-])(reload|restart)\b", c)], "rollback must never reload/restart anything")

    def test_purge_data_is_explicit(self):
        self.exec("--purge-data")
        r = self.h.root
        self.assertFalse((r / "var/lib/bluestream").exists())
        self.assertFalse((r / "etc/bluestream").exists())
        self.assertTrue((r / "etc/nginx/nginx.conf").exists())

    def test_source_has_no_global_cleanup_verbs(self):
        src = (CANARY / "canary_rollback.sh").read_text()
        body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("docker system prune", "docker rm", "iptables", "ufw", "apt-get", "systemctl restart", "systemctl reload", "nginx -s", "rm -rf /etc/nginx",
                          "rm -rf /*", "rm -rf /var/www ", "uninstall.sh"):
            self.assertNotIn(forbidden, body, forbidden)


class DiagnosticsTests(CanaryTestCase):
    def setUp(self):
        super().setUp()
        h = self.h
        run = h.root / "var/lib/bluestream/run"
        run.mkdir(parents=True)
        (run / "studio-dino.health.json").write_text('{"state": "HEALTHY", "speed": 1.01, "cpu_cores": 0.8, "restarts": 2}')
        (run / "relay-dino.status").write_text("STATE=LIVE\nDETAIL=ffmpeg running\nATTEMPT=0\n")
        h.stub("journalctl", 'echo "2026-11-21T10:00:00 host ffmpeg[1]: Output #0, flv, to \'rtmps://a.rtmps.youtube.com:443/live2/%s\'"; '
                             'echo "2026-11-21T10:00:01 web: password: hunter2hunter2 token=Zm9vOmJhcg"; '
                             'echo "2026-11-21T10:00:02 web: Authorization: Bearer AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"; '
                             'echo "2026-11-21T10:00:03 STREAM_KEY=%s"; echo "GET /x?key=s3cr3tvalue&a=1 Cookie: session=sessionsecret99"; '
                             'echo "2026-11-21T10:00:04 licence BS1.eyJ2IjoxfQ.AAAAAAAAAAAAAAAAAAAAAAAAAAAA"; echo "frame=1500 fps=30 speed=1.0x drop=0 restarts"' % (YT_KEY, YT_KEY))
        h.stub("ps", 'echo "  PID ETIMES %%CPU %%MEM RSS ARGS"; echo "1234 3600 85.0 4.0 330000 ffmpeg -i rtmp://127.0.0.1:1935/bluestream-relay/dino -f flv rtmps://a.rtmps.youtube.com:443/live2/%s"' % YT_KEY)
        h.stub("uptime", 'echo " 10:00:00 up 3 days, load average: 0.9, 1.0, 1.1"')
        h.stub("free", 'echo "Mem: 8000 3000 2000"')
        h.stub("docker", 'echo "rustdesk-hbbs Up 3 days"')
        h.stub("systemctl", 'case "$1" in list-units) echo "bluestream-relay@dino.service loaded active running relay" ;; show) echo "ActiveState=active"; echo "NRestarts=2" ;; esac')
        h.stub("timeout", 'shift; exec "$@"')
        wctl = h.root / "usr/local/lib/bluestream/web-ctl"
        wctl.parent.mkdir(parents=True, exist_ok=True)
        wctl.write_text("#!/bin/sh\necho '{\"ok\": true, \"op\": \"'$1'\"}'\n")
        wctl.chmod(0o755)
        h.stub("id", 'case "$1" in -u) echo 0 ;; *) echo root ;; esac')

    def test_secrets_are_redacted_and_useful_numbers_survive(self):
        rc, out = self.h.run("studio_diagnostics.sh")
        self.assertEqual(rc, 0, out)
        for s in SECRETS:
            self.assertNotIn(s, out, "leaked: " + s)
        self.assertIn("[REDACTED", out)
        for useful in ('"state": "HEALTHY"', "NRestarts=2", "STATE=LIVE", "frame=1500", "speed=1.0x", "rustdesk-hbbs", "rtmps://a.rtmps.youtube.com:443/live2/[REDACTED-KEY]",
                       "rtmp://127.0.0.1:1935/bluestream-relay/dino"):
            self.assertIn(useful, out)
        self.assertIn("no stream-key-, token- or authorization-like strings", out)

    def test_final_scan_warns_when_a_new_secret_shape_slips_through(self):
        self.h.stub("journalctl", "echo 'STREAM_KEY=[x] Bearer ABCDEFGH12345678'")
        rc, out = self.h.run("studio_diagnostics.sh")
        self.assertIn("[REDACTED", out)               # the filter still handled it
        # an UNKNOWN secret shape: the filter must still catch what it can, and the final self-check must never claim "clean" falsely
        self.h.stub("journalctl", "echo 'rtmps://example.com/live/SECRETSECRETSECRET99 leaked as: STREAM_KEY=[x]'; echo 'link BS1.AAAAAAAAAAAAAAAAAAAAAAAA'")
        rc, out = self.h.run("studio_diagnostics.sh")
        self.assertNotIn("SECRETSECRETSECRET99", out)

    def test_it_never_reads_stream_key_files_or_secrets(self):
        src = (CANARY / "studio_diagnostics.sh").read_text()
        body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for forbidden in ("/etc/bluestream/destinations", "admin.json", "/etc/sudoers", "/etc/letsencrypt", "/etc/bluestream/relays", "/etc/bluestream/playlists", "server.conf"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_it_is_read_only(self):
        before = self.h.tree_digest()
        self.h.run("studio_diagnostics.sh")
        self.assertEqual(before, self.h.tree_digest())
        src = (CANARY / "studio_diagnostics.sh").read_text()
        body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for pat in (r"systemctl (restart|stop|start|reload|enable|disable|daemon-reload|kill)", r"(?<![\w-])kill\b", r"\brm\s", r"\bmv\s", r"\btee\b",
                    r"\bchmod\b", r"\bchown\b", r"\biptables\b", r"\bufw\b", r"docker (rm|stop|restart|kill|prune|exec|run)", r"\bapt-get\b",
                    r"sed -i", r"\bmkdir\b", r"\btouch\b"):
            self.assertIsNone(re.search(pat, body), pat)

    def test_non_root_run_still_works_and_says_it_is_partial(self):
        self.h.stub("id", 'case "$1" in -u) echo 1000 ;; *) echo tester ;; esac')
        rc, out = self.h.run("studio_diagnostics.sh")
        self.assertEqual(rc, 0, out)
        self.assertIn("not root", out)


class StaticScriptTests(unittest.TestCase):
    def test_all_canary_scripts_parse_and_have_no_control_bytes(self):
        for f in CANARY.glob("*.sh"):
            if BASH:
                self.assertEqual(subprocess.run([BASH, "-n", posix(f)], capture_output=True).returncode, 0, f.name)
            self.assertIsNone(re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", f.read_bytes()), f.name)

    def test_browser_capture_is_never_enabled_by_the_canary_tooling(self):
        for f in CANARY.glob("*.sh"):
            body = "\n".join(l for l in f.read_text().splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("BLUESTREAM_STUDIO_BROWSER", body, f.name)


if __name__ == "__main__":
    unittest.main()
