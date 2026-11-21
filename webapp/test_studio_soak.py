"""The soak/benchmark harness must start, report real progress and measure real encoding."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FFMPEG = shutil.which("ffmpeg")
READY = bool(FFMPEG) and sys.platform.startswith("linux") and os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("studio_soak", REPO / "tools" / "studio_soak.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

from webapp.studio import ffmpeg as sff  # noqa: E402
from webapp.studio.model import Overlay, Project  # noqa: E402


def soak_project(work):
    return Project("soak", "media", work + "/src.mp4", [
        Overlay("rect", x=0.0, y=0.88, w=1.0, h=0.12, color="#000000", opacity=0.6),
        Overlay("clock", x=0.7, y=0.04, w=0.25, h=0.1, size=56),
        Overlay("ticker", y=0.9, w=1.0, h=0.08, size=36, text="ticker "),
        Overlay("countdown", x=0.3, y=0.4, w=0.4, h=0.15, size=96, target="2099-01-01T00:00:00"),
        Overlay("image", x=0.04, y=0.04, w=0.08, asset="logo.png")]).validate()


class InitTextFilesTests(unittest.TestCase):
    def test_every_text_backed_overlay_gets_a_file_before_ffmpeg_starts(self):
        with tempfile.TemporaryDirectory() as d:
            soak.init_text_files(soak_project(d), d)
            names = sorted(os.listdir(d))
            # visible overlay indexes: 0 rect, 1 clock, 2 ticker, 3 countdown, 4 image
            self.assertEqual(names, ["ov1.txt", "ov2.txt", "ov3.txt"])
            self.assertEqual(open(d + "/ov2.txt").read(), "ticker ")        # the ticker file was previously never written
            self.assertRegex(open(d + "/ov1.txt").read(), r"^\d\d:\d\d:\d\d$")
            self.assertRegex(open(d + "/ov3.txt").read(), r"^\d+d \d\d:\d\d:\d\d$")


@unittest.skipUnless(READY, "needs Linux + ffmpeg + DejaVu font (CI)")
class RealSoakTests(unittest.TestCase):
    def test_missing_text_files_really_do_break_filter_initialisation(self):
        """Documents WHY initialisation order matters (the reported failure mode)."""
        with tempfile.TemporaryDirectory() as d:
            proj = soak_project(d)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
                            "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libx264", "-c:a", "aac", d + "/src.mp4"], check=True)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:size=96x96:d=1",
                            "-frames:v", "1", d + "/logo.png"], check=True)
            args = sff._build(proj, d + "/src.mp4", "-", d, d, sff.DEFAULT_FONT, True, False, "ffmpeg", "null", [])
            args.remove("-re")
            args[-1] = "-"
            r = subprocess.run(args, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0, "ffmpeg started although the text files did not exist")
            soak.init_text_files(proj, d)
            r2 = subprocess.run(args, capture_output=True, text=True, timeout=120)
            self.assertEqual(r2.returncode, 0, r2.stderr[-400:])

    def test_benchmark_starts_reports_progress_and_measures_real_encoding(self):
        with tempfile.TemporaryDirectory() as out:
            rc = soak.main(["--seconds", "14", "--interval", "3", "--out", out])
            self.assertEqual(rc, 0)
            verdict = json.load(open(out + "/verdict.json"))
            rows = open(out + "/soak.csv").read().strip().splitlines()
            self.assertGreaterEqual(len(rows), 3)                        # header + >= 2 samples
            self.assertEqual(verdict["restarts"], 0)
            self.assertGreater(verdict["frames_total"], 100)              # real frames were encoded
            self.assertGreater(verdict["encoded_video_seconds"], 3)
            self.assertGreater(verdict["speed_min"], 0)
            self.assertGreater(verdict["cpu_cores_max"], 0)               # a real encode burns CPU
            self.assertGreater(verdict["rss_mb_max"], 20)
            self.assertTrue(verdict["gate"].startswith("PARTIAL"), verdict["gate"])   # never a 72h claim
            self.assertEqual(verdict["stall_samples"], 0)


if __name__ == "__main__":
    unittest.main()


class SpeedParsingTests(unittest.TestCase):
    """The first 10-minute Docker benchmark logged padded integer speeds ('speed=   1x') as 0.0."""

    def test_speed_formats_ffmpeg_really_emits(self):
        import re
        pat = r"\s*([0-9]+(?:\.[0-9]+)?)x?\s*$"
        for text, want in (("1.01x", 1.01), ("   1x", 1.0), ("0.97x", 0.97), ("2x", 2.0)):
            self.assertEqual(float(re.match(pat, text).group(1)), want)
        self.assertIsNone(re.match(pat, "N/A"))
        self.assertIsNone(re.match(pat, ""))

    def test_source_uses_the_robust_parser_and_a_local_scratch_dir(self):
        src = (REPO / "tools" / "studio_soak.py").read_text()
        self.assertIn("([0-9]+(?:\.[0-9]+)?)x?", src)
        self.assertIn("tempfile.mkdtemp(prefix=\"soak\", dir=a.workdir or None)", src)       # not under --out (bind mounts)


class ProgressTailTests(unittest.TestCase):
    def test_parse_progress_reads_only_the_tail_of_a_huge_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            for i in range(40000):                                  # ~1 MB of history
                fh.write("frame=%d\nspeed=1.0x\nprogress=continue\n" % i)
            fh.write("frame=99999\nfps=30\ndrop_frames=2\nspeed=   1x\nprogress=continue\n")
            path = fh.name
        try:
            pr = soak.parse_progress(path)
            self.assertEqual((pr["frame"], pr["drop_frames"], pr["speed"].strip()), ("99999", "2", "1x"))
        finally:
            os.unlink(path)
        self.assertEqual(soak.parse_progress("/nonexistent/path"), {})
