"""Real-FFmpeg validation of Studio output. Skipped when ffmpeg/ffprobe/fonts are
absent (e.g. a bare dev box); CI installs them so these run for real."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from webapp.studio import ffmpeg as sff
from webapp.studio import widgets as wd
from webapp.studio.model import Overlay, Project

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FONT = sff.DEFAULT_FONT
READY = bool(FFMPEG and FFPROBE and os.path.exists(FONT))


def probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def keyframe_times(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout.split()
    return [float(x) for x in out if x]


@unittest.skipUnless(READY, "ffmpeg/ffprobe/DejaVu font not available")
class RealFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = cls.tmp.name.replace("\\", "/")
        if not cls.dir.startswith("/"):
            raise unittest.SkipTest("needs a POSIX path")
        cls.src = cls.dir + "/src.mp4"
        # Deliberately NOT the target profile: 640x360, 25 fps, mpeg4, no keyframe tuning.
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=640x360:rate=25:duration=4", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-c:v", "mpeg4", "-c:a", "mp2", cls.src],
                       check=True)
        cls.silent = cls.dir + "/silent.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=30:duration=3", "-c:v", "mpeg4", cls.silent],
                       check=True)
        # a tiny PNG logo
        cls.assets = cls.dir + "/assets"
        os.makedirs(cls.assets)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=red:size=64x64:duration=1", "-frames:v", "1",
                        cls.assets + "/logo.png"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def project(self, overlays):
        return Project("demo", "media", self.src, list(overlays)).validate()

    def run_ff(self, args, timeout=120):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def assert_target_profile(self, path, min_duration=2.5):
        info = probe(path)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["codec_name"], v["pix_fmt"]), ("h264", "yuv420p"))
        self.assertEqual((v["width"], v["height"]), (1280, 720))
        self.assertEqual(v["r_frame_rate"], "30/1")
        self.assertEqual(a["codec_name"], "aac")
        self.assertEqual(int(a["channels"]), 2)
        self.assertGreaterEqual(float(info["format"]["duration"]), min_duration)
        keys = keyframe_times(path)
        gaps = [b - a_ for a_, b in zip(keys, keys[1:])]
        self.assertTrue(gaps and max(gaps) <= 2.05, "GOP above 2s: %s" % gaps)
        return info

    def test_prerender_static_overlays_normalises_to_profile(self):
        proj = self.project([Overlay("rect", x=0.0, y=0.9, w=1.0, h=0.1, color="#000000", opacity=0.6),
                             Overlay("text", x=0.05, y=0.92, h=0.07, text="BlueStream", size=40),
                             Overlay("image", x=0.85, y=0.05, w=0.1, asset="logo.png")])
        wd.atomic_write(sff.text_file_path(self.dir, 1), "BlueStream")
        out = self.dir + "/pre.mp4"
        self.run_ff(sff.build_prerender_args(proj, self.src, out, self.dir, self.assets_dir()))
        self.assert_target_profile(out, 3.5)

    def assets_dir(self):
        return self.assets

    def test_dynamic_overlays_one_chain_valid_output(self):
        proj = self.project([Overlay("clock", x=0.05, y=0.05, size=48),
                             Overlay("ticker", y=0.9, h=0.08, size=36),
                             Overlay("countdown", x=0.5, y=0.5, target="2030-01-01T00:00:00")])
        for i, text in ((0, "12:00:00"), (1, "BlueStream ticker"), (2, "01:02:03")):
            wd.atomic_write(sff.text_file_path(self.dir, i), text)
        out = self.dir + "/live.mp4"
        # The live builder differs from pre-render only in output (bus/flv); render the
        # identical graph to a file so it can be probed.
        args = sff._build(proj, self.src, out, self.dir, self.assets, sff.DEFAULT_FONT,
                          True, False, FFMPEG, "mp4", [])
        args.remove("-re")
        self.run_ff(args)
        self.assert_target_profile(out, 3.5)

    def test_source_without_audio_gets_silence(self):
        proj = Project("demo", "media", self.silent, [Overlay("text", text="x")]).validate()
        wd.atomic_write(sff.text_file_path(self.dir, 0), "x")
        out = self.dir + "/silent_out.mp4"
        args = sff.build_prerender_args(proj, self.silent, out, self.dir, self.assets, has_audio=False)
        self.run_ff(args)
        self.assert_target_profile(out, 2.0)

    def test_loop_boundary_keeps_timestamps_monotonic(self):
        proj = self.project([Overlay("text", text="loop")])
        wd.atomic_write(sff.text_file_path(self.dir, 0), "loop")
        out = self.dir + "/loop.mp4"
        args = sff._build(proj, self.src, out, self.dir, self.assets, sff.DEFAULT_FONT,
                          True, True, FFMPEG, "mp4", [])
        args.remove("-re")
        args[-3:-3] = ["-t", "9"]  # three source loops (4s source -> loops past EOF)
        self.run_ff(args)
        info = self.assert_target_profile(out, 8.5)
        out_pts = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=dts_time",
             "-of", "default=nw=1:nk=1", out], capture_output=True, text=True, check=True).stdout.split()
        dts = [float(x) for x in out_pts if x]
        self.assertTrue(all(b >= a for a, b in zip(dts, dts[1:])), "non-monotonic DTS at loop")
        self.assertIn("format", info)

    def test_copy_path_does_not_reencode(self):
        # Make a compliant source, then stream-copy it (swap bus URL for a file for probing).
        good = self.dir + "/good.mp4"
        self.run_ff([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                     "testsrc=size=1280x720:rate=30:duration=3", "-f", "lavfi", "-i",
                     "sine=frequency=300:duration=3", "-c:v", "libx264", "-g", "60",
                     "-keyint_min", "60", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100",
                     "-ac", "2", good])
        args = sff.build_copy_args(good, "rtmp://127.0.0.1:1935/bluestream-relay/demo")
        self.assertIn("copy", args)
        self.assertNotIn("libx264", args)
        out = self.dir + "/copy.flv"
        args[-1] = out
        args.remove("-re")
        self.run_ff(args)
        v = next(s for s in probe(out)["streams"] if s["codec_type"] == "video")
        self.assertEqual((v["codec_name"], v["width"]), ("h264", 1280))

    def make_png(self, name, color):
        path = "%s/%s" % (self.assets, name)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=%s:size=800x450:duration=1" % color, "-frames:v", "1", path], check=True)
        return path

    def test_slideshow_clip_matches_profile_and_loops_by_copy(self):
        from webapp.studio.model import OutputProfile
        imgs = [self.make_png("a.png", "red"), self.make_png("b.png", "blue"), self.make_png("c.png", "green")]
        out = self.dir + "/slides.mp4"
        self.run_ff(sff.build_slideshow_args(imgs, 2, OutputProfile(), out))
        info = self.assert_target_profile(out, 5.5)
        self.assertAlmostEqual(float(info["format"]["duration"]), 6.0, delta=0.3)
        # the cheap path: loop the cached clip by stream copy for 14s (2+ loops)
        looped = self.dir + "/slides_loop.flv"
        args = sff.build_copy_args(out, "rtmp://127.0.0.1:1935/bluestream-relay/x", loop_file=True)
        args.remove("-re")
        args[-1:-1] = ["-t", "14"]
        args[-1] = looped
        self.run_ff(args)
        dts = [float(x) for x in subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=dts_time",
             "-of", "default=nw=1:nk=1", looped], capture_output=True, text=True, check=True).stdout.split() if x]
        self.assertTrue(all(b >= a for a, b in zip(dts, dts[1:])), "non-monotonic DTS across copy loop")
        self.assertGreaterEqual(dts[-1], 12.0)


if __name__ == "__main__":
    unittest.main()
