"""Docker-first deployment: static safety of compose.yml, and behaviour of docker/bluestream.sh + docker/preflight.sh
against a STUB `docker` (no real Docker needed, nothing is ever started). The real container was exercised separately
in Docker Desktop (see docs/bluestream/TESTS.md)."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker" / "compose.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash")


def code_lines(text):
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


class ComposeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.code = "\n".join(code_lines(COMPOSE))

    def test_no_privilege_or_host_access(self):
        for bad in ("privileged: true", "docker.sock", "network_mode", "pid:", "ipc:", "userns_mode", "devices:",
                    "cap_add: [ALL]", "SYS_ADMIN", "NET_ADMIN", "security_opt", "extra_hosts"):
            self.assertNotIn(bad, self.code, bad)
        self.assertIn("privileged: false", self.code)

    def test_hardened_runtime(self):
        self.assertIn("read_only: true", self.code)
        self.assertIn("cap_drop: [ALL]", self.code)
        for limit in ("cpus:", "mem_limit:", "memswap_limit:", "pids_limit:"):
            self.assertIn(limit, self.code)
        self.assertIn("init: true", self.code)

    def test_only_loopback_publishing_and_never_the_rtmp_bus(self):
        ports = re.findall(r'^\s+- "([^"]+)"\s*(?:#.*)?$', self.code.split("ports:", 1)[1].split("volumes:", 1)[0], re.M)
        self.assertEqual(len(ports), 1, ports)
        self.assertTrue(ports[0].startswith("127.0.0.1:"), ports[0])
        self.assertNotIn("1935", self.code.split("ports:", 1)[1].split("volumes:", 1)[0])

    def test_only_named_volumes_no_host_paths(self):
        block = self.code.split("    volumes:", 1)[1].split("environment:", 1)[0]
        for ln in block.splitlines():
            m = re.match(r"\s*- (\S+):(/\S+)", ln)
            if m:
                self.assertRegex(m.group(1), r"^bs_[a-z]+$", "host path or bind mount: " + ln)

    def test_every_resource_is_named_after_the_project(self):
        names = re.findall(r"^\s+(?:name|container_name|hostname): (.+)$", self.code, re.M)
        self.assertGreaterEqual(len(names), 6)
        for n in names:
            self.assertIn("${BS_PROJECT:-bluestream}", n, n)

    def test_resources_carry_the_project_label_used_for_scoped_rollback(self):
        self.assertEqual(self.code.count("com.bluestream.project"), 6)   # container, network, 4 volumes

    def test_studio_is_enabled_inside_the_one_console(self):
        self.assertIn('BLUESTREAM_STUDIO_ENABLED: "1"', self.code)
        self.assertNotIn("studio:", self.code.split("services:", 1)[1].split("networks:", 1)[0].replace("BLUESTREAM_STUDIO", ""))  # one service


def make_stub(tmp, docker_script):
    d = Path(tmp) / "stubbin"
    d.mkdir()
    p = d / "docker"
    p.write_text(docker_script, encoding="utf-8", newline="\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


STUB = r"""#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1" in
  inspect) case "$*" in *Labels*) echo "bstest";; *) echo "healthy";; esac ;;
  logs) echo 'push rtmp://a.rtmp.youtube.com/live2/abcd-efgh-ijkl-mnop-qrst now'; echo 'login password=hunter2 token=abcdefABCDEF0123456789abcdefABCDEF'; echo 'ok line' ;;
  exec) echo 'inside: STREAM_KEY=abcd-efgh-ijkl-mnop' ;;
  stats|port) echo "x" ;;
  *) echo "stub: $*" ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "bash required")
class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = make_stub(self.tmp, STUB)
        self.log = Path(self.tmp) / "calls.log"
        self.env = dict(os.environ, PATH="%s%s%s" % (self.stub.as_posix(), os.pathsep, os.environ["PATH"]),
                        STUB_LOG=self.log.as_posix(), BS_ENV_DIR=(Path(self.tmp) / "env").as_posix(), MSYS_NO_PATHCONV="1")

    def run_cli(self, *args):
        return subprocess.run([BASH, (ROOT / "docker" / "bluestream.sh").as_posix(), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_down_is_a_dry_run_by_default(self):
        r = self.run_cli("down", "--project", "bstest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertNotIn("compose", self.calls())

    def test_down_needs_the_exact_phrase_and_never_prunes(self):
        r = self.run_cli("down", "--project", "bstest", "--execute", "--confirm", "yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("confirmation phrase mismatch", r.stderr)
        self.assertNotRegex(self.calls(), r"prune|system|rm ")

    def test_purge_needs_a_second_phrase(self):
        os.makedirs(self.env["BS_ENV_DIR"], exist_ok=True)
        Path(self.env["BS_ENV_DIR"], "bstest.env").write_text("BS_PROJECT=bstest\n")
        r = self.run_cli("down", "--project", "bstest", "--execute", "--confirm", "REMOVE BLUESTREAM DOCKER bstest",
                         "--purge-volumes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--confirm-data-loss", r.stderr)
        self.assertNotIn("volume rm", self.calls())

    def test_rejects_unsafe_project_names_and_arguments(self):
        for bad in ("Bad Name", "x", "a;rm -rf /", "UPPER", "../x", "a" * 40):
            r = self.run_cli("status", "--project", bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertNotEqual(self.run_cli("status", "--bogus").returncode, 0)
        self.assertNotEqual(self.run_cli("diag", "--project", "bstest", "--since", "1h;id").returncode, 0)
        self.assertNotEqual(self.run_cli("up", "--vendor-pubkey", "not-hex").returncode, 0)

    def test_logs_and_diag_are_redacted(self):
        for cmd in (("logs",), ("diag",)):
            r = self.run_cli(*cmd, "--project", "bstest")
            out = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, out)
            for secret in ("abcd-efgh-ijkl-mnop", "hunter2", "abcdefABCDEF0123456789abcdefABCDEF"):
                self.assertNotIn(secret, out, cmd)
        self.assertIn("ok line", self.run_cli("logs", "--project", "bstest").stdout)

    def test_only_the_projects_own_container_is_addressed(self):
        self.run_cli("diag", "--project", "bstest")
        for ln in self.calls().splitlines():
            if ln.startswith(("exec", "logs", "stats", "port")):
                self.assertIn("bstest-app", ln)

    def test_script_contains_no_global_cleanup_or_host_changes(self):
        text = "\n".join(code_lines((ROOT / "docker" / "bluestream.sh").read_text(encoding="utf-8")))
        for bad in ("system prune", "volume prune", "container prune", "network prune", "image prune", "rm -rf",
                    "ufw ", "iptables", "systemctl", "apt-get", "apt install", "nginx -s", "docker.sock", "--privileged"):
            self.assertNotIn(bad, text, bad)


@unittest.skipUnless(BASH, "bash required")
class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_pf(self, stub, *args):
        d = make_stub(self.tmp, stub)
        env = dict(os.environ, PATH="%s%s%s" % (d.as_posix(), os.pathsep, os.environ["PATH"]),
                   STUB_LOG=(Path(self.tmp) / "c.log").as_posix(), MSYS_NO_PATHCONV="1")
        return subprocess.run([BASH, (ROOT / "docker" / "preflight.sh").as_posix(), *args], env=env,
                              capture_output=True, text=True, timeout=90)

    def test_unreachable_docker_blocks_and_points_to_the_setup_guide(self):
        r = self.run_pf("#!/usr/bin/env bash\nexit 1\n", "--project", "bstest")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("BLOCKED", r.stdout)
        self.assertIn("Nothing was changed", r.stdout)

    def test_foreign_container_with_the_same_name_blocks(self):
        stub = ("#!/usr/bin/env bash\ncase \"$1 $2\" in\n"
                "\"info --format\") echo '8 16000000000 2 x /var/lib/docker overlayfs';;\n"
                "\"inspect -f\") echo '';;\n"
                "\"inspect bstest-app\") exit 0;;\n"
                "*) exit 0;; esac\n")
        r = self.run_pf(stub, "--project", "bstest")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("bstest-app", r.stdout)

    def test_rejects_a_bad_project_name_without_touching_docker(self):
        r = self.run_pf("#!/usr/bin/env bash\necho called >> \"$STUB_LOG\"\n", "--project", "Bad;Name")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((Path(self.tmp) / "c.log").exists())

    def test_script_is_read_only(self):
        text = "\n".join(code_lines((ROOT / "docker" / "preflight.sh").read_text(encoding="utf-8")))
        for bad in ("docker run", "docker rm", "docker stop", "docker start", "compose up", "compose down", "volume create",
                    "network create", "docker pull", "docker build", "apt-get", "apt install", "ufw ", "iptables",
                    "rm -rf", "chmod", "chown", "> /etc", "docker exec"):
            self.assertNotIn(bad, text, bad)
        # the only systemctl use is a read-only listing of native units
        for use in re.findall(r"systemctl\s+\S+", text):
            self.assertEqual(use, "systemctl list-units", use)


class ImageAndGlueTests(unittest.TestCase):
    def test_dockerfile_never_bakes_secrets_or_uses_host_package_manager_at_runtime(self):
        text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("secret_key", text)             # removed after the installer ran
        self.assertIn("admin.json", text)
        self.assertNotIn("--privileged", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("ENV BS_ADMIN_PASSWORD", text)

    def test_entrypoint_never_prints_the_generated_password(self):
        text = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        for ln in code_lines(text):
            if re.search(r"(echo|logger)", ln) and "-n \"$PW\"" not in ln:
                self.assertNotIn("$PW", ln, ln)

    def test_dockerignore_keeps_secrets_and_git_out_of_the_build_context(self):
        text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for need in (".git", ".env"):
            self.assertIn(need, text)


if __name__ == "__main__":
    unittest.main()
