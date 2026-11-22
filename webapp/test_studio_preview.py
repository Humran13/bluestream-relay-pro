"""Editor previews on a hardened host: narrowly scoped, read-only, no permission widening.

Context: managed media is root:bluestream-relay 0640 in a 0750 dir and HLS output is nginx 0750.
bluestream-web (the console) can read neither, and must not be given access. Previews therefore
come from two READ-ONLY bridge operations that drop privileges to the owning account."""
import base64
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webapp import engine as engine_mod
from webapp.studio import cli, preview, store
from webapp.studio.model import Project
from webapp.test_studio_console import Base

REPO = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
JPG = b"\xff\xd8\xff" + b"x" * 64


def read(p):
    return Path(p).read_text(encoding="utf-8", errors="replace")


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hls, self.media = self.tmp.name + "/hls", self.tmp.name + "/media"
        os.makedirs(self.hls + "/relay/cam")
        os.makedirs(self.media)

    def test_hls_is_read_as_the_nginx_account_and_media_as_the_relay_account(self):
        self.assertEqual(preview.choose_identity(self.hls + "/relay/cam/1.ts", self.hls, self.media), "nginx")
        self.assertEqual(preview.choose_identity(self.media + "/a.mp4", self.hls, self.media), "relay")

    def test_anything_else_has_no_identity_and_symlink_escapes_are_caught(self):
        self.assertIsNone(preview.choose_identity("/etc/shadow", self.hls, self.media))
        self.assertIsNone(preview.choose_identity(self.media + "/../secret", self.hls, self.media))
        self.assertIsNone(preview.choose_identity(self.media + "-evil/a.mp4", self.hls, self.media))
        if os.name != "nt":
            os.symlink("/etc/passwd", self.media + "/link.png")
            self.assertIsNone(preview.choose_identity(self.media + "/link.png", self.hls, self.media))
            with mock.patch.dict(os.environ, {"BLUESTREAM_MEDIA_DIR": self.media}):
                self.assertIsNone(preview.safe_media_path("link.png"))      # symlinks are never followed

    def test_root_refuses_unmanaged_paths_and_missing_service_accounts(self):
        with mock.patch("os.geteuid", return_value=0, create=True):
            with self.assertRaises(PermissionError):
                preview._run(["true"], "/etc/shadow", 5)
            with mock.patch.object(preview, "_ids", return_value=None):
                with self.assertRaises(PermissionError):
                    preview._dropper("relay")

    def test_unprivileged_runs_without_any_drop(self):
        with mock.patch("os.geteuid", return_value=1000, create=True):
            self.assertIsNone(preview._dropper("relay"))

    def test_source_selection_per_project_kind(self):
        with mock.patch.dict(os.environ, {"BLUESTREAM_MEDIA_DIR": self.media, "BLUESTREAM_HLS_ROOT": self.hls}):
            for f in ("a.mp4", "s1.png", "bg.jpg"):
                open(os.path.join(self.media, f), "wb").write(b"x")
            open(self.hls + "/relay/cam/9.ts", "wb").write(b"x")
            os.makedirs(self.hls + "/playlist/pl")
            open(self.hls + "/playlist/pl/3.ts", "wb").write(b"x")
            sp = preview.source_path
            self.assertTrue(sp(Project("p", "media", "a.mp4")).endswith("a.mp4"))
            self.assertTrue(sp(Project("p", "slideshow", "s1.png,s2.png")).endswith("s1.png"))
            self.assertTrue(sp(Project("p", "audio", "t.mp3", background="bg.jpg")).endswith("bg.jpg"))
            self.assertIsNone(sp(Project("p", "audio", "t.mp3", background="#102030")))
            self.assertTrue(sp(Project("p", "relay", "cam")).endswith("9.ts"))
            self.assertTrue(sp(Project("p", "playlist", "pl")).endswith("3.ts"))


class CliImageTests(unittest.TestCase):
    def run_cmd(self, fn, arg, data):
        buf = io.BytesIO()
        out = mock.Mock(buffer=buf)
        target = "frame_jpeg" if fn == "frame" else "thumb_png"
        with mock.patch.object(preview, target, return_value=data), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", io.StringIO()), mock.patch.object(store, "load_draft", return_value=Project("p", "media", "a.mp4")):
            rc = cli.cmd_frame(arg) if fn == "frame" else cli.cmd_thumb(arg)
        return rc, buf.getvalue()

    def test_output_is_key_value_pairs_with_base64(self):
        rc, raw = self.run_cmd("frame", "p", JPG)
        parts = raw.split(bytes([0]))
        self.assertEqual((rc, parts[0], parts[1], parts[2]), (0, b"mime", b"image/jpeg", b"data"))
        self.assertEqual(base64.b64decode(parts[3]), JPG)
        rc, raw = self.run_cmd("thumb", "logo.png", PNG)
        self.assertEqual(raw.split(bytes([0]))[1], b"image/png")

    def test_nothing_available_is_a_clean_failure(self):
        empty = b"mime" + bytes([0, 0]) + b"data" + bytes([0, 0])      # empty values: "no preview"
        self.assertEqual(self.run_cmd("frame", "p", None), (1, empty))
        self.assertEqual(self.run_cmd("thumb", "logo.png", None), (1, empty))

    def test_cli_rejects_bad_names_before_touching_files(self):
        for argv in (["x", "thumb", "../etc/passwd"], ["x", "thumb", "a/b.png"], ["x", "frame", "../p"], ["x", "frame", "A B"]):
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(argv), 2, argv)


class EngineTests(unittest.TestCase):
    def test_read_validation(self):
        v = engine_mod.EngineClient._validate_read_values
        self.assertEqual(v("studio_frame", ("demo",)), ["demo"])
        self.assertEqual(v("studio_thumb", ("logo.png",)), ["logo.png"])
        for op, bad in (("studio_frame", "../x"), ("studio_frame", "A B"), ("studio_thumb", "../x.png"),
                        ("studio_thumb", "a/b.png"), ("studio_thumb", ".hidden"), ("studio_thumb", "")):
            with self.assertRaises(engine_mod.EngineError, msg=bad):
                v(op, (bad,))
        with self.assertRaises(engine_mod.EngineError):
            v("studio_frame", ("a", "b"))

    def test_both_operations_are_read_only_members_only(self):
        self.assertIn("studio_frame", engine_mod.ALLOWED_READ_OPERATIONS)
        self.assertIn("studio_thumb", engine_mod.ALLOWED_READ_OPERATIONS)
        self.assertNotIn("studio_frame", engine_mod.ALLOWED_MUTATION_OPERATIONS)
        self.assertNotIn("studio_thumb", engine_mod.ALLOWED_MUTATION_OPERATIONS)

    def test_decode_is_strict(self):
        d = engine_mod.EngineClient._decode_image
        good = {"mime": "image/png", "data": base64.b64encode(PNG).decode()}
        self.assertEqual(d(good, "image/png"), PNG)
        self.assertIsNone(d(good, "image/jpeg"))                                       # wrong type
        self.assertIsNone(d({"mime": "image/png", "data": "!!notbase64"}, "image/png"))
        self.assertIsNone(d({"mime": "image/png", "data": ""}, "image/png"))
        self.assertIsNone(d({"mime": "image/png", "data": base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()}, "image/png"))
        self.assertIsNone(d(None, "image/png"))


class RoutesViaBridgeTests(Base):
    """The console cannot read media itself (nothing in the media dir); previews come from the bridge."""

    def setUp(self):
        super().setUp()
        d = self._tmp.name
        os.environ["BLUESTREAM_MEDIA_DIR"] = d + "/empty-media"      # unreadable/absent for this user
        os.makedirs(d + "/empty-media", exist_ok=True)
        self.addCleanup(os.environ.pop, "BLUESTREAM_MEDIA_DIR", None)
        os.environ["BLUESTREAM_HLS_ROOT"] = d + "/empty-hls"
        self.addCleanup(os.environ.pop, "BLUESTREAM_HLS_ROOT", None)
        store.save_draft(Project("pv", "media", "loop.mp4").validate())
        self.engine.frames, self.engine.thumbs, self.engine.fail = {}, {}, False

        def studio_frame(name):
            if self.engine.fail:
                raise engine_mod.EngineError("bridge down")
            return self.engine.frames.get(name)

        def studio_thumb(name):
            if self.engine.fail:
                raise engine_mod.EngineError("bridge down")
            return self.engine.thumbs.get(name)
        self.engine.studio_frame, self.engine.studio_thumb = studio_frame, studio_thumb

    def test_frame_comes_from_the_bridge_when_the_console_cannot_read_the_source(self):
        self.engine.frames["pv"] = JPG
        r = self.client.get("/console/studio/pv/frame.jpg")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/jpeg", JPG))

    def test_overlay_image_thumbnail_comes_from_the_bridge(self):
        self.engine.thumbs["logo.png"] = PNG
        r = self.client.get("/console/studio/asset/logo.png")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/png", PNG))

    def test_missing_or_failing_bridge_degrades_to_404_not_an_error_page(self):
        self.assertEqual(self.client.get("/console/studio/pv/frame.jpg").status_code, 404)
        self.engine.fail = True
        self.assertEqual(self.client.get("/console/studio/pv/frame.jpg").status_code, 404)
        self.assertEqual(self.client.get("/console/studio/asset/logo.png").status_code, 404)

    def test_previews_require_login_and_valid_names(self):
        self.engine.frames["pv"], self.engine.thumbs["logo.png"] = JPG, PNG
        anon = self.app.test_client()
        self.assertEqual(anon.get("/console/studio/pv/frame.jpg").status_code, 302)
        self.assertEqual(anon.get("/console/studio/asset/logo.png").status_code, 302)
        for bad in ("..%2Fx.png", ".hidden.png", "a%2Fb.png"):
            self.assertEqual(self.client.get("/console/studio/asset/" + bad).status_code, 404, bad)
        self.assertEqual(self.client.get("/console/studio/Bad Name/frame.jpg").status_code, 404)


class PermissionRegressionTests(unittest.TestCase):
    """Static guards: previews must never be solved by widening media/HLS/config access."""

    def setUp(self):
        self.install = read(REPO / "install.sh")
        self.web = read(REPO / "lib" / "webconsole.sh")
        self.media = read(REPO / "lib" / "media.sh")
        self.webctl = read(REPO / "web-ctl")

    def test_media_and_hls_modes_are_the_documented_narrow_ones_and_reapplied_on_every_run(self):
        self.assertIn('chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_MEDIA_DIR"', self.install)
        self.assertIn('chmod 0750 "$BLUESTREAM_MEDIA_DIR"', self.install)
        self.assertIn('chmod 0750 "$BLUESTREAM_HLS_ROOT" "$BLUESTREAM_HLS_RELAY_DIR" "$BLUESTREAM_HLS_PLAYLIST_DIR"', self.install)
        # existing media is re-normalised on every (upgrade) run, never loosened
        self.assertRegex(self.install, r"find \"\$BLUESTREAM_MEDIA_DIR\"[^\n]*\n\s+-exec chown root:\"\$BLUESTREAM_GROUP\"")
        self.assertRegex(self.install, r"find \"\$BLUESTREAM_MEDIA_DIR\"[^\n]*\n\s+-exec chmod 0640")

    def test_new_uploads_and_imports_are_created_root_owned_0640(self):
        self.assertGreaterEqual(len(re.findall(r'install -o root -g "\$BLUESTREAM_GROUP" -m 0640', self.media)), 2)
        self.assertIn('chmod 0640 "$snapshot"', self.media)

    def test_nothing_in_the_repo_loosens_modes(self):
        offenders = []
        for f in list((REPO / "lib").glob("*.sh")) + [REPO / "install.sh", REPO / "web-ctl", REPO / "uninstall.sh"] + \
                list((REPO / "config" / "systemd").glob("*.sh")):
            for n, line in enumerate(read(f).splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if re.search(r"chmod\s+(-R\s+)?(0?777|0?666|a\+[rwx]*w|o\+w|[ugo]*=rwx)\b", line):
                    offenders.append("%s:%d %s" % (f.name, n, line.strip()))
        self.assertEqual(offenders, [])

    def test_web_user_is_never_added_to_media_relay_or_nginx_groups(self):
        for f in list((REPO / "lib").glob("*.sh")) + [REPO / "install.sh"]:
            text = read(f)
            for m in re.finditer(r"(usermod|gpasswd|adduser|useradd)[^\n]*", text):
                line = m.group(0)
                if "BLUESTREAM_WEB_USER" in line:
                    self.assertNotRegex(line, r"-[a-zA-Z]*G\b|--groups|-a\b", line)
        self.assertIn("--gid \"$BLUESTREAM_WEB_GROUP\"", self.web)
        self.assertIn("for _grp in sudo root www-data", self.web)

    def test_sudoers_still_allows_exactly_one_command(self):
        sudoers = "\n".join(l for f in (REPO / "config" / "sudoers").glob("*") for l in read(f).splitlines()
                            if l.strip() and not l.strip().startswith("#") and not l.startswith("Defaults"))
        self.assertEqual(sudoers.strip(), "bluestream-web ALL=(root) NOPASSWD: /usr/local/lib/bluestream/web-ctl")

    def test_preview_bridge_ops_are_read_only(self):
        body = re.search(r"wc_op_studio_preview\(\) \{.*?\n\}\n", self.webctl, re.S).group(0)
        for forbidden in ("chmod", "chown", "systemctl", "install ", "mv ", "cp ", "tee ", ">> "):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertNotIn("mktemp", body)                                             # no temp files in web-ctl
        self.assertIn("studio_frame|studio_thumb)", self.webctl)

    def test_backup_restore_does_not_widen_modes_and_restore_allowlist_is_unchanged(self):
        backup = read(REPO / "lib" / "backup.sh")
        self.assertNotRegex(backup, r"chmod\s+0?[0-7]*[4567]\b[^\n]*media")
        # media is restored under the allow-listed prefix; install re-normalises ownership/mode on the next run
        self.assertIn("var/lib/bluestream/media/", backup)

    def test_studio_store_is_the_only_new_console_writable_path(self):
        unit = read(REPO / "config" / "systemd" / "bluestream-web.service.template")
        rw = re.search(r"^ReadWritePaths=(.*)$", unit, re.M).group(1).split()
        self.assertIn("-/var/lib/bluestream/studio", rw)
        self.assertNotIn("/var/lib/bluestream/media/", " ".join(rw) + "/")  # media listed as before, not widened
        self.assertEqual(sorted(p for p in rw if "studio" in p), ["-/var/lib/bluestream/studio"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and sys.platform.startswith("linux"),
                     "needs ffmpeg (CI)")
class RealPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.media, self.hls = d + "/media", d + "/hls"
        os.makedirs(self.media)
        os.makedirs(self.hls + "/relay/cam")
        os.makedirs(self.hls + "/playlist/pl")
        env = mock.patch.dict(os.environ, {"BLUESTREAM_MEDIA_DIR": self.media, "BLUESTREAM_HLS_ROOT": self.hls})
        env.start()
        self.addCleanup(env.stop)
        preview._cache.clear()
        ff = ["ffmpeg", "-v", "error", "-y"]
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=3", "-c:v", "libx264",
                             "-pix_fmt", "yuv420p", self.media + "/clip.mp4"], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=red@0.5:size=1600x900:duration=1,format=rgba", "-frames:v", "1",
                             self.media + "/logo.png"], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=3", "-c:v", "libx264",
                             "-f", "mpegts", self.hls + "/relay/cam/1.ts"], check=True)
        shutil.copy(self.hls + "/relay/cam/1.ts", self.hls + "/playlist/pl/1.ts")

    def jpeg_size(self, data):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", "-"],
                             input=data, capture_output=True).stdout.decode().strip()
        return tuple(int(x) for x in out.split("x"))

    def test_frames_for_every_source_kind(self):
        for proj in (Project("p", "media", "clip.mp4"), Project("p", "slideshow", "logo.png"),
                     Project("p", "audio", "x.mp3", background="logo.png"), Project("p", "relay", "cam"),
                     Project("p", "playlist", "pl")):
            data = preview.frame_jpeg(proj)
            self.assertTrue(data and data[:2] == b"\xff\xd8", proj.source_kind)
            self.assertLessEqual(self.jpeg_size(data)[0], 960)

    def test_thumbnail_is_a_scaled_png_that_keeps_transparency(self):
        data = preview.thumb_png("logo.png")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        w, h = self.jpeg_size(data)
        self.assertEqual((w, h), (960, 540))
        pix = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                             input=data, capture_output=True).stdout
        self.assertLess(pix[3], 255)                                    # alpha channel survived

    def test_videos_get_a_small_still_for_the_media_browser_but_other_files_are_refused(self):
        png = preview.thumb_png("clip.mp4")                              # media browser thumbnail (first second of the video)
        self.assertTrue(png and png[:4] == b"\x89PNG")
        self.assertIsNone(preview.thumb_png("notes.txt"))
        self.assertIsNone(preview.thumb_png("nope.png"))
        self.assertIsNone(preview.frame_jpeg(Project("p", "relay", "unknown")))


if __name__ == "__main__":
    unittest.main()
