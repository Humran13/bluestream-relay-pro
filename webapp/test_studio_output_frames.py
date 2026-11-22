"""EVIDENCE that published overlays are in the ENCODED OUTPUT video, not just in the editor.

Runs the real production path: `studio.cli argv` (planner + FFmpeg argv) and the real `studio.cli widgets` sidecar (timers,
text files, hot reload, health), then real FFmpeg encodes the composed stream; the output file is DECODED and pixels are
measured. Only the final RTMP URL is swapped for a file (the nginx-rtmp bus is not needed to prove the picture).
Needs ffmpeg/ffprobe and Linux (load average); skipped otherwise: on a dev box without them this is UNTESTED, not passed."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from webapp.studio import health, store
from webapp.studio.model import OutputProfile, Overlay, Project

HERE = Path(__file__).resolve().parent
FF = shutil.which("ffmpeg")
READY = bool(FF and shutil.which("ffprobe")) and hasattr(os, "getloadavg")


def mean(data):
    return sum(data) / float(len(data)) if data else 0.0


@unittest.skipUnless(READY, "ffmpeg/ffprobe + Linux required (UNTESTED on this machine, not passed)")
class OutputFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="studio-frames-")
        t = cls.tmp
        for d in ("media", "run", "studio", "cache"):
            os.makedirs(os.path.join(t, d))
        cls.env = dict(os.environ, BLUESTREAM_STUDIO_DIR=t + "/studio", BLUESTREAM_RUN_DIR=t + "/run", BLUESTREAM_MEDIA_DIR=t + "/media",
                       BLUESTREAM_STUDIO_CACHE=t + "/cache", BLUESTREAM_STUDIO_MAX_LIVE="4", BLUESTREAM_STUDIO_TEST_LOAD="0", PYTHONPATH=str(HERE))
        cls._saved = {k: os.environ.get(k) for k in ("BLUESTREAM_STUDIO_DIR", "BLUESTREAM_RUN_DIR", "BLUESTREAM_MEDIA_DIR")}
        os.environ.update(BLUESTREAM_STUDIO_DIR=t + "/studio", BLUESTREAM_RUN_DIR=t + "/run", BLUESTREAM_MEDIA_DIR=t + "/media")
        cls.sh(FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=4", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-shortest", t + "/media/black.mp4")
        cls.sh(FF, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=30:duration=4", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-c:a", "aac", "-shortest", t + "/media/motion.mp4")
        cls.sh(FF, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xff0000:s=64x64", "-frames:v", "1", t + "/media/red.png")

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def sh(*argv):
        subprocess.run(list(argv), check=True, capture_output=True)

    def px(self, mp4, t, x, y, w, h, fmt="gray"):
        """Decoded pixels of a crop of the frame at time t (raw bytes)."""
        r = subprocess.run([FF, "-v", "error", "-ss", str(t), "-i", mp4, "-frames:v", "1", "-vf", "crop=%d:%d:%d:%d,format=%s" % (w, h, x, y, fmt),
                            "-f", "rawvideo", "-"], capture_output=True)
        return r.stdout

    def rgb_mean(self, mp4, t, box):
        d = self.px(mp4, t, *box, fmt="rgb24")
        return tuple(mean(d[i::3]) for i in range(3))

    def run_stream(self, project, seconds, relay="tr", during=None):
        """Real sidecar + real argv + real FFmpeg for `seconds`; `during(elapsed_hook)` may edit the published project. -> (out file, health dict)."""
        name = project.name
        store.save_draft(project)
        store.publish(name)
        out = "%s/out-%s.flv" % (self.tmp, name)
        side = subprocess.Popen([sys.executable, "-m", "studio.cli", "widgets", relay, name], cwd=str(HERE), env=self.env, stderr=subprocess.PIPE)
        try:
            r = subprocess.run([sys.executable, "-m", "studio.cli", "argv", relay, name], cwd=str(HERE), env=self.env, capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
            args = [a.decode() for a in r.stdout.split(b"\0") if a]
            args = args[:-1] + [out] if args[-1].startswith("rtmp://") else args
            i = len(args) - 1 - args[::-1].index("-f")                       # the output muxer option
            args = args[:i] + ["-t", str(seconds)] + args[i:]
            ff = subprocess.Popen([FF] + args, env=self.env, stderr=subprocess.PIPE)
            t0 = time.monotonic()
            if during:
                during(t0)
            _, err = ff.communicate(timeout=seconds * 3 + 60)
            self.assertEqual(ff.returncode, 0, err.decode(errors="replace")[-600:])
            time.sleep(1.2)
            doc = health.read_health(self.env["BLUESTREAM_RUN_DIR"], relay)
        finally:
            side.send_signal(signal.SIGTERM)
            try:
                side.wait(timeout=10)
            except subprocess.TimeoutExpired:
                side.kill()
        return out, doc

    def test_text_rect_image_countdown_and_pip_video_are_in_the_encoded_output(self):
        p = Project("frames1", "media", "black.mp4", [
            Overlay("text", text="HELLO", x=0.03, y=0.08, w=0.3, h=0.12, size=80),
            Overlay("rect", x=0.05, y=0.6, w=0.2, h=0.2, color="#0000ff", opacity=1.0),
            Overlay("image", asset="red.png", x=0.30, y=0.6, w=0.05, h=0.09),
            Overlay("countdown", target="2099-01-01T00:00:00", tz="UTC", x=0.05, y=0.35, w=0.4, h=0.12, size=80),
            Overlay("video", asset="motion.mp4", x=0.55, y=0.05, w=0.3, h=0.3),
        ], OutputProfile(preset="ultrafast"))
        out, doc = self.run_stream(p, 7, relay="tr1")
        # Baseline: an empty corner of the same frames is black.
        empty = self.rgb_mean(out, 5, (1000, 500, 200, 150))
        self.assertTrue(all(c < 12 for c in empty), empty)
        # text: white pixels where the text layer sits (black source otherwise)
        self.assertGreater(mean(self.px(out, 5, 38, 50, 360, 100)), 21)
        # rect: solid blue block
        r, g, b = self.rgb_mean(out, 5, (80, 450, 200, 100))
        self.assertTrue(b > 200 and r < 40 and g < 40, (r, g, b))
        # transparent PNG layer: red picture
        r, g, b = self.rgb_mean(out, 5, (392, 436, 40, 40))
        self.assertTrue(r > 180 and g < 60 and b < 60, (r, g, b))
        # countdown: the digits change over time IN THE OUTPUT (two frames 2.5 s apart differ)
        a, c = self.px(out, 2, 64, 240, 1100, 100), self.px(out, 4.5, 64, 240, 1100, 100)
        diff = sum(1 for x, y in zip(a, c) if abs(x - y) > 40)
        self.assertGreater(mean(a), 25)
        control = self.px(out, 2, 64, 240, 1100, 100)               # the same instant decoded twice must be identical
        self.assertEqual(sum(1 for x, y in zip(a, control) if abs(x - y) > 40), 0)
        self.assertGreater(diff, 50, "the countdown did not change between frames")
        # picture-in-picture video: moving content (frames 2 s apart differ a lot inside the PiP box)
        a, c = self.px(out, 1.5, 710, 40, 370, 200), self.px(out, 3.5, 710, 40, 370, 200)
        self.assertGreater(sum(1 for x, y in zip(a, c) if abs(x - y) > 30), 2000, "the PiP video did not move")
        # health from the real listener: real-time encode, no drops, plan reported
        self.assertEqual(doc.get("plan"), "live-composition", doc)
        self.assertGreater(float(doc.get("speed") or 0), 0.9, doc)

    def test_hot_apply_changes_the_picture_without_restarting_ffmpeg_and_structure_change_waits_for_restart(self):
        name = "frames2"
        base = Project(name, "media", "black.mp4", [Overlay("clock", x=0.6, y=0.8, w=0.3, h=0.1, size=50),
                                                    Overlay("text", text="ALPHA ALPHA", x=0.03, y=0.08, w=0.6, h=0.15, size=90)],
                       OutputProfile(preset="ultrafast"))

        def during(t0):
            time.sleep(2.2)
            # HOT: same structure, new text content, published while the stream is running
            q = store.load_draft(name)
            q.overlays[1] = Overlay("text", text="OMEGA 12", x=0.03, y=0.08, w=0.6, h=0.15, size=90)
            store.save_draft(q)
            store.publish(name)
            time.sleep(3.0)
            self.hot_doc = health.read_health(self.env["BLUESTREAM_RUN_DIR"], "tr2")
            # STRUCTURAL: moved layer -> must NOT change the running picture; the sidecar flags a pending restart instead
            q = store.load_draft(name)
            q.overlays[1] = Overlay("text", text="OMEGA 12", x=0.30, y=0.50, w=0.6, h=0.15, size=90)
            store.save_draft(q)
            store.publish(name)
            time.sleep(2.5)
        out, doc = self.run_stream(base, 9, relay="tr2", during=during)
        before = self.px(out, 1.0, 38, 50, 760, 130)
        after = self.px(out, 5.0, 38, 50, 760, 130)
        self.assertGreater(mean(before), 20)
        self.assertGreater(sum(1 for x, y in zip(before, after) if abs(x - y) > 60), 800, "the text did not change in the output after the hot apply")
        self.assertEqual(self.hot_doc.get("revision"), 2, self.hot_doc)
        self.assertEqual(self.hot_doc.get("pending_restart") or "", "")
        # the structural change did not alter the running output (text still where it was) and is reported as waiting
        late = self.px(out, 8.0, 38, 50, 760, 130)
        self.assertLess(sum(1 for x, y in zip(after, late) if abs(x - y) > 60), 600)
        self.assertEqual(doc.get("pending_restart"), "yes", doc)

    def test_software_encoder_works_and_nvenc_is_refused_when_unavailable(self):
        from webapp.studio import capabilities
        p = Project("frames3", "media", "black.mp4", [Overlay("clock", x=0.6, y=0.8, w=0.3, h=0.1)], OutputProfile(encoder="h264_nvenc"))
        store.save_draft(p)
        store.publish("frames3")
        ok, _ = capabilities.probe_nvenc(force=True)
        r = subprocess.run([sys.executable, "-m", "studio.cli", "argv", "tr3", "frames3"], cwd=str(HERE), env=self.env, capture_output=True)
        if ok:
            self.assertEqual(r.returncode, 0)                  # a GPU exists here: NVENC genuinely works
            self.assertIn(b"h264_nvenc", r.stdout)
        else:
            self.assertEqual(r.returncode, 6, r.stderr)        # refused, never silently switched to software
            self.assertIn(b"never switched automatically", r.stderr)
        # and software still produces a decodable stream
        p = Project("frames4", "media", "black.mp4", [Overlay("text", text="SOFT", x=0.03, y=0.08, w=0.3, h=0.12, size=80)], OutputProfile(preset="ultrafast"))
        out, _ = self.run_stream(p, 3, relay="tr4")
        self.assertGreater(mean(self.px(out, 2, 38, 50, 360, 100)), 21)


@unittest.skipUnless(READY and os.environ.get("BLUESTREAM_SOAK_SECONDS"), "set BLUESTREAM_SOAK_SECONDS=<seconds> to run the live-composition soak")
class LiveCompositionSoak(OutputFrameTests):
    test_text_rect_image_countdown_and_pip_video_are_in_the_encoded_output = None
    test_hot_apply_changes_the_picture_without_restarting_ffmpeg_and_structure_change_waits_for_restart = None
    test_software_encoder_works_and_nvenc_is_refused_when_unavailable = None

    """Long run of the REAL composed pipeline over a short looping source: encoder speed, CPU, RAM, drops, restarts, and output continuity
    (no timestamp gaps, no loop-boundary stalls). A run of minutes is NOT a 72 h pass; the report states the duration measured."""

    def test_soak(self):
        seconds = int(os.environ["BLUESTREAM_SOAK_SECONDS"])
        p = Project("soak1", "media", "motion.mp4", [Overlay("clock", x=0.05, y=0.05, w=0.3, h=0.1, size=60),
                                                     Overlay("text", text="SOAK", x=0.05, y=0.8, w=0.3, h=0.1, size=60)],
                    OutputProfile(preset="veryfast", video_kbps=2500))
        samples = []

        def during(t0):
            while time.monotonic() - t0 < seconds - 2:
                time.sleep(15)
                d = health.read_health(self.env["BLUESTREAM_RUN_DIR"], "soakr")
                if d:
                    samples.append(d)
        out, doc = self.run_stream(p, seconds, relay="soakr", during=during)
        pk = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", out],
                            capture_output=True, text=True).stdout.split()
        pts = [float(x) for x in pk if x not in ("N/A", "")]
        gaps = [b - a for a, b in zip(pts, pts[1:])]
        worst = max(gaps) if gaps else 99
        speeds = [float(d.get("speed") or 0) for d in samples if d.get("speed")]
        print("SOAK seconds=%d loops~%d packets=%d worst_gap=%.3fs min_speed=%.2f max_cpu_cores=%s max_rss_mb=%s dropped=%s restarts=%s plan=%s"
              % (seconds, seconds // 4, len(pts), worst, min(speeds or [0]), max([float(d.get("cpu_cores") or 0) for d in samples] or [0]),
                 max([float(d.get("rss_mb") or 0) for d in samples] or [0]), doc.get("dropped_frames"), doc.get("restarts"), doc.get("plan")))
        self.assertGreater(len(pts), seconds * 10)
        self.assertLess(worst, 0.5, "a gap in the output (stall) was found")
        self.assertGreater(min(speeds or [0]), 0.95)
        self.assertEqual(str(doc.get("restarts") or "0"), "0")
        self.assertEqual(doc.get("plan"), "live-composition")


if __name__ == "__main__":
    unittest.main()
