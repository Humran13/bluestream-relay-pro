"""Tests for the BlueStream Studio core (planner, model, argv builder, widgets)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from webapp.studio import ffmpeg as sff
from webapp.studio import planner as pl
from webapp.studio import widgets as wd
from webapp.studio.model import Overlay, OutputProfile, Project, ValidationError

try:
    from zoneinfo import ZoneInfo
    ZoneInfo("Europe/London")
    HAVE_TZ = True
except Exception:  # tzdata missing (e.g. bare Windows dev box)
    HAVE_TZ = False

GOOD = pl.SourceInfo(vcodec="h264", pix_fmt="yuv420p", width=1280, height=720, fps=30.0,
                     acodec="aac", gop_seconds=2.0, duration=600.0, content_id="a1")
BUS_IN = "rtmp://127.0.0.1/live/src"
BUS_OUT = "rtmp://127.0.0.1/live/studio"


def proj(overlays=(), **kw):
    return Project("demo", "relay", "dino", list(overlays), **kw).validate()


class PlannerTests(unittest.TestCase):
    def test_direct_copy_when_compatible_and_no_overlays(self):
        plan = pl.plan_output(proj(), GOOD)
        self.assertEqual(plan.path, pl.DIRECT)
        self.assertFalse(plan.encodes)

    def test_static_overlay_prerenders_then_copies(self):
        plan = pl.plan_output(proj([Overlay("text", text="Hi")]), GOOD)
        self.assertEqual(plan.path, pl.PRERENDER)
        self.assertTrue(plan.cache_key)

    def test_dynamic_overlay_needs_live_encode(self):
        for kind in ("clock", "ticker"):
            plan = pl.plan_output(proj([Overlay(kind)]), GOOD)
            self.assertEqual(plan.path, pl.LIVE, kind)
        plan = pl.plan_output(proj([Overlay("countdown", target="2030-01-01T00:00:00")]), GOOD)
        self.assertEqual(plan.path, pl.LIVE)

    def test_incompatible_finite_source_is_normalised_once(self):
        bad = pl.SourceInfo(**{**GOOD.__dict__, "vcodec": "hevc", "fps": 25.0})
        plan = pl.plan_output(proj(), bad)
        self.assertEqual(plan.path, pl.PRERENDER)
        self.assertTrue(any("hevc" in r for r in plan.reasons))

    def test_live_incompatible_source_uses_live_path(self):
        live = pl.SourceInfo(**{**GOOD.__dict__, "live": True, "duration": None, "acodec": "mp3"})
        self.assertEqual(pl.plan_output(proj(), live).path, pl.LIVE)

    def test_unknown_gop_is_never_assumed_copyable(self):
        unk = pl.SourceInfo(**{**GOOD.__dict__, "gop_seconds": None})
        self.assertNotEqual(pl.plan_output(proj(), unk).path, pl.DIRECT)

    def test_too_long_source_not_prerendered(self):
        long_ = pl.SourceInfo(**{**GOOD.__dict__, "duration": 10 * 3600.0})
        self.assertEqual(pl.plan_output(proj([Overlay("text", text="x")]), long_).path, pl.LIVE)

    def test_hidden_overlay_ignored(self):
        plan = pl.plan_output(proj([Overlay("clock", visible=False)]), GOOD)
        self.assertEqual(plan.path, pl.DIRECT)

    def test_cache_key_tracks_content_and_overlay(self):
        a = pl.plan_output(proj([Overlay("text", text="A")]), GOOD).cache_key
        b = pl.plan_output(proj([Overlay("text", text="B")]), GOOD).cache_key
        c = pl.plan_output(proj([Overlay("text", text="A")]),
                           pl.SourceInfo(**{**GOOD.__dict__, "content_id": "zz"})).cache_key
        again = pl.plan_output(proj([Overlay("text", text="A")]), GOOD).cache_key
        self.assertEqual(a, again)
        self.assertEqual(len({a, b, c}), 3)

    def test_admission_control(self):
        live = pl.Plan(pl.LIVE, ())
        self.assertTrue(pl.admit(live, 0)[0])
        self.assertFalse(pl.admit(live, 1)[0])
        self.assertFalse(pl.admit(live, 0, load_avg=3.5, cpu_cores=4)[0])
        self.assertTrue(pl.admit(pl.Plan(pl.DIRECT, ()), 5)[0])


class ModelTests(unittest.TestCase):
    def test_validation_rejects_bad_input(self):
        bad = [Overlay("bogus"), Overlay("text", x=0.9, w=0.5), Overlay("text", color="red"),
               Overlay("text", font="a;rm -rf"), Overlay("image", asset="../etc/passwd"),
               Overlay("countdown"), Overlay("text", size=2), Overlay("text", opacity=2)]
        for o in bad:
            with self.assertRaises(ValidationError, msg=repr(o)):
                proj([o])
        with self.assertRaises(ValidationError):
            Project("Bad Name", "relay", "x").validate()
        with self.assertRaises(ValidationError):
            proj([Overlay("text")] * 25)

    def test_bandwidth_estimate_matches_documented_figures(self):
        self.assertAlmostEqual(OutputProfile(video_kbps=4000).monthly_terabytes(), 1.34, places=2)
        self.assertAlmostEqual(OutputProfile(video_kbps=8000).monthly_terabytes(), 2.63, places=2)


class FfmpegArgvTests(unittest.TestCase):
    def build(self, overlays, **kw):
        return sff.build_live_args(proj(overlays), BUS_IN, BUS_OUT, "/run/bs/work",
                                   "/var/lib/bs/assets", **kw)

    def test_is_argv_list_with_expected_encode_settings(self):
        a = self.build([Overlay("clock", x=0.1, y=0.1)])
        self.assertIsInstance(a, list)
        self.assertTrue(all(isinstance(x, str) for x in a))
        joined = " ".join(a)
        for need in ("libx264", "-g 60", "-keyint_min 60", "-pix_fmt yuv420p", "-b:v 4000k",
                     "-c:a aac", "-b:a 128k", "-f flv", BUS_OUT):
            self.assertIn(need, joined)
        self.assertEqual(a.count("-filter_complex"), 1)  # single chain

    def test_layered_graph(self):
        a = self.build([Overlay("rect", w=0.3, h=0.1), Overlay("image", asset="logo.png", w=0.1),
                        Overlay("ticker", y=0.9, h=0.08), Overlay("countdown", target="2030-01-01T00:00:00")])
        graph = a[a.index("-filter_complex") + 1]
        for need in ("drawbox", "overlay=", "drawtext", "reload=1", "mod(t*120,w+text_w)"):
            self.assertIn(need, graph)
        self.assertIn("/var/lib/bs/assets/logo.png", a)

    def test_missing_audio_gets_silence(self):
        a = self.build([], has_audio=False)
        self.assertIn("anullsrc=r=44100:cl=stereo", a)

    def test_text_never_enters_argv(self):
        a = self.build([Overlay("text", text="$(reboot); `x` \"'")])
        self.assertNotIn("reboot", " ".join(a))

    def test_rejects_non_local_urls_and_unsafe_paths(self):
        p = proj([])
        for bad in ("rtmp://evil.example/live/x", "http://169.254.169.254/x", "file:///etc/passwd",
                    "rtmp://127.0.0.1.evil.com/live/x", "rtmp://127.0.0.1/live/x;rm"):
            with self.assertRaises(ValidationError, msg=bad):
                sff.build_live_args(p, bad, BUS_OUT, "/run/bs/work", "/a")
        for path in ("relative/dir", "/run/bs/../etc", "/run/bs/work;x", "/a b"):
            with self.assertRaises(ValidationError, msg=path):
                sff.build_live_args(p, BUS_IN, BUS_OUT, path, "/a")


class WidgetTests(unittest.TestCase):
    NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_countdown_format_and_finish(self):
        t = (self.NOW + timedelta(days=2, hours=3, minutes=4, seconds=5)).isoformat()
        self.assertEqual(wd.format_countdown(t, "UTC", self.NOW), "2d 03:04:05")
        t2 = (self.NOW + timedelta(seconds=75)).isoformat()
        self.assertEqual(wd.format_countdown(t2, "UTC", self.NOW), "00:01:15")
        self.assertEqual(wd.format_countdown(self.NOW.isoformat(), "UTC", self.NOW), "LIVE NOW")

    def test_invalid_target_rejected(self):
        with self.assertRaises(ValidationError):
            wd.format_countdown("not-a-date", "UTC", self.NOW)

    @unittest.skipUnless(HAVE_TZ, "tzdata not installed")
    def test_timezone_and_dst(self):
        # London is UTC+0 in winter, UTC+1 in summer: same wall time, different instants.
        w = wd.parse_target("2030-01-15T12:00:00", "Europe/London")
        s = wd.parse_target("2030-07-15T12:00:00", "Europe/London")
        self.assertEqual(w.hour, 12)
        self.assertEqual(s.hour, 11)
        self.assertEqual(wd.format_clock("Asia/Tokyo", self.NOW), "21:00:00")
        with self.assertRaises(ValidationError):
            wd.format_clock("Not/AZone", self.NOW)

    def test_pomodoro_resumes_from_absolute_anchor(self):
        anchor = self.NOW
        self.assertEqual(wd.pomodoro_phase(anchor, anchor + timedelta(minutes=10)), ("FOCUS", 15 * 60))
        self.assertEqual(wd.pomodoro_phase(anchor, anchor + timedelta(minutes=27)), ("BREAK", 3 * 60))
        # after a "restart" (any later instant) phase is still derived, not remembered
        self.assertEqual(wd.pomodoro_phase(anchor, anchor + timedelta(minutes=31))[0], "FOCUS")

    def test_atomic_write_replaces_whole_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "ov0.txt")
            wd.atomic_write(f, "one")
            wd.atomic_write(f, "two")
            self.assertEqual(open(f, encoding="utf-8").read(), "two")
            self.assertEqual(os.listdir(d), ["ov0.txt"])


if __name__ == "__main__":
    unittest.main()


class StoreRuntimeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.env = {"BLUESTREAM_STUDIO_DIR": d + "/studio", "BLUESTREAM_RUN_DIR": d + "/run",
                    "BLUESTREAM_MEDIA_DIR": d + "/media", "BLUESTREAM_STUDIO_CACHE": d + "/cache"}
        for k, v in self.env.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        for sub in ("run", "media", "cache"):
            os.makedirs(d + "/" + sub)

    def test_draft_publish_rollback_and_history(self):
        from webapp.studio import store
        p = proj([Overlay("text", text="v1")])
        store.save_draft(p)
        self.assertTrue(store.has_unpublished_changes("demo"))
        self.assertEqual(store.publish("demo"), 1)
        self.assertFalse(store.has_unpublished_changes("demo"))
        p.overlays = [Overlay("text", text="v2")]
        store.save_draft(p)
        self.assertEqual(store.publish("demo"), 2)
        self.assertEqual(store.load_published("demo").overlays[0].text, "v2")
        store.rollback("demo", 1)
        self.assertEqual(store.load_draft("demo").overlays[0].text, "v1")
        self.assertEqual(store.load_published("demo").overlays[0].text, "v2")  # live untouched
        for i in range(12):
            store.save_draft(proj([Overlay("text", text="r%d" % i)]))
            store.publish("demo")
        self.assertLessEqual(len(store.revisions("demo")), store.KEEP_REVISIONS)
        with self.assertRaises(ValidationError):
            store.load_draft("../evil")
        self.assertEqual(store.list_projects(), ["demo"])

    def test_corrupt_project_file_is_rejected_not_crashing(self):
        from webapp.studio import store
        os.makedirs(os.environ["BLUESTREAM_STUDIO_DIR"])
        open(os.environ["BLUESTREAM_STUDIO_DIR"] + "/bad.json", "w").write("{not json")
        with self.assertRaises(ValueError):
            store.load_published("bad")

    def test_cache_eviction_lru_and_stale_tmp(self):
        from webapp.studio import runtime
        d = os.environ["BLUESTREAM_STUDIO_CACHE"]
        for i, age in enumerate((300, 200, 100)):
            f = "%s/k%d.mp4" % (d, i)
            open(f, "wb").write(b"x" * 1000)
            os.utime(f, (__import__("time").time() - age,) * 2)
        removed = runtime.evict_cache(limit=2000)
        self.assertEqual(removed, ["k0.mp4"])  # least recently used first

    def test_progress_parsing_and_health(self):
        from webapp.studio import runtime
        blk = "frame=90\nfps=30.0\ndrop_frames=1\nspeed=1.02x\nprogress=continue\n"
        cur = runtime.parse_progress(blk)
        self.assertEqual((cur["frame"], cur["drop_frames"]), (90, 1))
        self.assertEqual(runtime.health_verdict(None, cur, 0)[0], "HEALTHY")
        self.assertEqual(runtime.health_verdict(cur, cur, 30)[0], "STALLED")
        slow = dict(cur, speed=0.8, frame=120)
        self.assertEqual(runtime.health_verdict(cur, slow, 5)[0], "DEGRADED")
        self.assertEqual(runtime.health_verdict(None, {}, 0)[0], "STARTING")

    def test_live_lease_counts_and_clears_stale(self):
        from webapp.studio import runtime
        runtime.take_live_lease("a")  # this process is alive
        self.assertEqual(runtime.running_live_encodes(), 1)
        self.assertEqual(runtime.running_live_encodes(exclude="a"), 0)
        open(os.environ["BLUESTREAM_RUN_DIR"] + "/studio-dead.live", "w").write("999999")
        self.assertEqual(runtime.running_live_encodes(), 1)  # dead lease ignored & removed
        runtime.release_live_lease("a")
        self.assertEqual(runtime.running_live_encodes(), 0)

    def _argv(self, relay, name):
        import io
        from unittest import mock
        from webapp.studio import cli
        buf = io.BytesIO()
        out = mock.Mock(buffer=buf)
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()) as err:
            rc = cli.cmd_argv(relay, name)
        return rc, [a.decode() for a in buf.getvalue().split(b"\0") if a], err.getvalue()

    @unittest.skipIf(os.name == "nt", "POSIX paths only (Linux CI covers this)")
    def test_cli_argv_live_plan_and_admission_refusal(self):
        from webapp.studio import runtime, store
        store.save_draft(Project("demo", "relay", "dino", [Overlay("clock")]).validate())
        store.publish("demo")
        rc, args, err = self._argv("demo", "demo")
        self.assertEqual(rc, 0, err)
        self.assertIn("libx264", args)
        self.assertEqual(args[-1], "rtmp://127.0.0.1:1935/bluestream-relay/demo")
        self.assertIn("plan=live-composition", err)
        # another live encoder holds the only slot -> refused, nothing emitted
        runtime.take_live_lease("other")
        rc, args, err = self._argv("demo", "demo")
        self.assertEqual((rc, args), (3, []))
        self.assertIn("refused", err)

    def test_cli_missing_project_or_media_fails_closed(self):
        self.assertEqual(self._argv("demo", "nope")[0], 2)
        from webapp.studio import store
        store.save_draft(Project("m", "media", "gone.mp4").validate())
        store.publish("m")
        self.assertEqual(self._argv("m", "m")[0], 2)

    def test_render_text_all_kinds(self):
        now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t = (now + timedelta(seconds=90)).isoformat()
        self.assertEqual(wd.render_text(Overlay("text", text="hi"), now), "hi")
        self.assertEqual(wd.render_text(Overlay("clock"), now), "12:00:00")
        self.assertEqual(wd.render_text(Overlay("countdown", target=t), now), "00:01:30")
        self.assertEqual(wd.render_text(Overlay("countup", target=(now - timedelta(seconds=75)).isoformat()), now), "00:01:15")
        self.assertEqual(wd.render_text(Overlay("pomodoro", target=now.isoformat(), work_min=25), now + timedelta(minutes=1)), "FOCUS 24:00")
        self.assertEqual(wd.render_text(Overlay("nowplaying", text="Now:"), now, "Song"), "Now: Song")
