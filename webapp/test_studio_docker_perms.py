"""Preview-bridge permission model with REAL Linux accounts (Docker, run as root; BS_DOCKER_PERMS=1).

Reproduces the production layout:
  media dir  root:bluestream-relay 0750, files 0640     (bluestream-web must NOT be able to read them)
  HLS dir    www-data:www-data     0750                 (bluestream-web must NOT be able to read it)
and proves the studio_frame/studio_thumb CLI (run by web-ctl as root) can still produce previews by dropping
to the owning account, and refuses unmanaged paths. Container result only: it says nothing about systemd units."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENABLED = (os.environ.get("BS_DOCKER_PERMS") == "1" and hasattr(os, "geteuid") and os.geteuid() == 0
           and shutil.which("ffmpeg") and shutil.which("useradd"))


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


@unittest.skipUnless(ENABLED, "needs root in the Docker test image (BS_DOCKER_PERMS=1)")
class PreviewBridgePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for user in ("bluestream-relay", "bluestream-web"):
            if sh(["id", user]).returncode != 0:
                assert sh(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", user]).returncode == 0
        cls.base = tempfile.mkdtemp(prefix="perm-", dir="/srv")
        os.chmod(cls.base, 0o755)
        cls.media, cls.hls, cls.studio, cls.rundir, cls.etc = (cls.base + "/" + d for d in ("media", "hls", "studio", "run", "etc"))
        for d in (cls.media, cls.hls + "/relay/cam", cls.studio, cls.rundir, cls.etc):
            os.makedirs(d, exist_ok=True)
        sh(["chown", "root:bluestream-relay", cls.media])
        os.chmod(cls.media, 0o750)
        ff = ["ffmpeg", "-v", "error", "-y"]
        sh(ff + ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 cls.media + "/clip.mp4"])
        sh(ff + ["-f", "lavfi", "-i", "color=c=red@0.5:size=1600x900:duration=1,format=rgba", "-frames:v", "1", cls.media + "/logo.png"])
        sh(ff + ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=3", "-c:v", "libx264", "-f", "mpegts",
                 cls.hls + "/relay/cam/1.ts"])
        for f in ("clip.mp4", "logo.png"):                                    # exactly what media.sh does for imports
            sh(["chown", "root:bluestream-relay", cls.media + "/" + f])
            os.chmod(cls.media + "/" + f, 0o640)
        sh(["chown", "-R", "www-data:www-data", cls.hls])
        sh(["chmod", "-R", "u=rwX,g=,o=", cls.hls])
        os.chmod(cls.hls, 0o750)
        sh(["chown", "www-data:www-data", cls.hls])
        # a project written by the console user (bluestream-web) into the Studio store (setgid dir, o+rx)
        sh(["chown", "root:bluestream-web", cls.studio])
        os.chmod(cls.studio, 0o2775)
        cls.env = dict(os.environ, PYTHONPATH=cls.base + "/pylib", BLUESTREAM_STUDIO_DIR=cls.studio, BLUESTREAM_RUN_DIR=cls.rundir,
                       BLUESTREAM_MEDIA_DIR=cls.media, BLUESTREAM_HLS_ROOT=cls.hls, BLUESTREAM_ETC_DIR=cls.etc)
        os.makedirs(cls.base + "/pylib")
        os.symlink(str(REPO / "webapp" / "studio"), cls.base + "/pylib/studio")
        os.chmod(str(cls.base + "/pylib"), 0o755)
        sys.path.insert(0, str(REPO))
        from webapp.studio import store
        from webapp.studio.model import Project
        os.environ["BLUESTREAM_STUDIO_DIR"] = cls.studio
        for name, kind, ref in (("pm", "media", "clip.mp4"), ("pr", "relay", "cam")):
            store.save_draft(Project(name, kind, ref).validate())
        sh(["chmod", "-R", "a+rX", cls.studio])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def as_user(self, user, *cmd):
        return subprocess.run(["runuser", "-u", user, "--"] + list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def cli(self, *args, user=None):
        cmd = [sys.executable, "-m", "studio.cli"] + list(args)
        env = ["env"] + ["%s=%s" % (k, v) for k, v in self.env.items() if k.startswith(("BLUESTREAM_", "PYTHONPATH"))]
        if user:
            return sh(["runuser", "-u", user, "--"] + env + cmd)
        return sh(env + cmd)

    # ---- the premise: the console user really cannot read the data ---------------------------
    def test_console_user_cannot_read_media_or_hls_directly(self):
        self.assertNotEqual(self.as_user("bluestream-web", "cat", self.media + "/clip.mp4").returncode, 0)
        self.assertNotEqual(self.as_user("bluestream-web", "ls", self.media).returncode, 0)
        self.assertNotEqual(self.as_user("bluestream-web", "ls", self.hls + "/relay/cam").returncode, 0)
        self.assertNotEqual(self.as_user("bluestream-web", "cat", self.hls + "/relay/cam/1.ts").returncode, 0)

    def test_service_accounts_read_only_what_they_own(self):
        self.assertEqual(self.as_user("bluestream-relay", "cat", self.media + "/clip.mp4").returncode, 0)
        self.assertNotEqual(self.as_user("bluestream-relay", "ls", self.hls + "/relay/cam").returncode, 0)     # HLS is nginx's
        self.assertEqual(self.as_user("www-data", "ls", self.hls + "/relay/cam").returncode, 0)
        self.assertNotEqual(self.as_user("www-data", "cat", self.media + "/clip.mp4").returncode, 0)

    # ---- the bridge: root drops to the owner and returns a small image -------------------------------
    def decode(self, proc):
        parts = proc.stdout.encode("latin-1", "ignore").split(b"\0") if proc.stdout else []
        return parts

    def test_media_frame_via_the_bridge_as_root(self):
        r = self.cli("frame", "pm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("image/jpeg", r.stdout)
        self.assertGreater(len(r.stdout), 1000)

    def test_hls_frame_via_the_bridge_as_root(self):
        r = self.cli("frame", "pr")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("image/jpeg", r.stdout)

    def test_thumbnail_via_the_bridge_as_root(self):
        r = self.cli("thumb", "logo.png")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("image/png", r.stdout)

    def test_the_helper_process_really_runs_unprivileged(self):
        """ffmpeg must run as the owning account, never as root (checked via a wrapper that records its uid)."""
        wrap = self.base + "/bin"
        os.makedirs(wrap, exist_ok=True)
        os.chmod(wrap, 0o755)
        real = shutil.which("ffmpeg")
        script = '#!/bin/sh\necho "$(id -u):$(id -gn)" >> %s/uids\nexec %s "$@"\n' % (self.base, real)
        open(wrap + "/ffmpeg", "w").write(script)
        os.chmod(wrap + "/ffmpeg", 0o755)
        env = ["env", "PATH=%s:%s" % (wrap, os.environ["PATH"])] + ["%s=%s" % (k, v) for k, v in self.env.items()
                                                                  if k.startswith(("BLUESTREAM_", "PYTHONPATH"))]
        open(self.base + "/uids", "w").close()
        os.chmod(self.base + "/uids", 0o666)
        for args in (("frame", "pm"), ("frame", "pr"), ("thumb", "logo.png")):
            self.assertEqual(sh(env + [sys.executable, "-m", "studio.cli"] + list(args)).returncode, 0)
        seen = open(self.base + "/uids").read().split()
        allowed = {"%s:bluestream-relay" % self._uid("bluestream-relay"), "%s:www-data" % self._uid("www-data")}
        self.assertGreaterEqual(len(seen), 3, seen)               # (an HLS frame may legitimately retry once)
        self.assertTrue(set(seen) <= allowed, seen)                # never root, never the console user
        self.assertEqual(set(seen), allowed, seen)                 # media -> relay account, HLS -> nginx account

    def _uid(self, user):
        return sh(["id", "-u", user]).stdout.strip()

    def test_unmanaged_paths_are_refused_even_as_root(self):
        os.symlink("/etc/shadow", self.media + "/evil.png")
        r = self.cli("thumb", "evil.png")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("image/", r.stdout)
        r = self.cli("thumb", "../../etc/passwd")
        self.assertEqual(r.returncode, 2)
        r = self.cli("frame", "../x")
        self.assertEqual(r.returncode, 2)

    def test_console_user_calling_the_cli_directly_gets_nothing(self):
        """Defence in depth: even if the console user ran the CLI itself, the files stay unreadable."""
        r = self.cli("frame", "pm", user="bluestream-web")
        self.assertNotIn("image/jpeg", r.stdout)
        r = self.cli("frame", "pr", user="bluestream-web")
        self.assertNotIn("image/jpeg", r.stdout)

    def test_no_preview_means_empty_values_not_an_error_document(self):
        from webapp.studio import store
        from webapp.studio.model import Project
        store.save_draft(Project("none", "media", "missing.mp4").validate())
        sh(["chmod", "-R", "a+rX", self.studio])
        r = self.cli("frame", "none")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.split("\0")[:4], ["mime", "", "data", ""])


if __name__ == "__main__":
    unittest.main()
