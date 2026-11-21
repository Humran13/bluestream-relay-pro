"""Studio health: honest verdicts from FFmpeg progress, not process existence."""
import json
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from webapp.studio import cli, health


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def block(frame, speed="1.00x", drop=0):
    return {"frame": str(frame), "fps": "30.0", "speed": speed, "drop_frames": str(drop),
            "out_time_us": str(frame * 33333), "progress": "continue"}


class TrackerVerdictTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tr = health.Tracker(clock=self.clock, wall=lambda: 5000)

    def test_starting_until_first_progress(self):
        self.assertEqual(self.tr.snapshot()["state"], "STARTING")
        self.tr.on_connect()
        self.assertEqual(self.tr.snapshot()["state"], "STARTING")

    def test_healthy_when_frames_advance_at_real_time(self):
        self.tr.on_connect()
        self.tr.on_block(block(30))
        self.clock.t += 2
        self.tr.on_block(block(90))
        snap = self.tr.snapshot({"cpu_cores": 1.8, "rss_mb": 300.0})
        self.assertEqual(snap["state"], "HEALTHY")
        self.assertEqual((snap["speed"], snap["frames"], snap["cpu_cores"]), (1.0, 90, 1.8))

    def test_process_alive_but_frames_frozen_is_stalled_not_healthy(self):
        self.tr.on_connect()
        self.tr.on_block(block(100))
        for _ in range(6):                      # encoder keeps reporting the SAME frame
            self.clock.t += 5
            self.tr.on_block(block(100))
        snap = self.tr.snapshot()
        self.assertEqual(snap["state"], "STALLED")
        self.assertIn("no new frames", snap["detail"])

    def test_no_reports_at_all_is_stalled(self):
        self.tr.on_connect()
        self.tr.on_block(block(10))
        self.clock.t += 60
        self.assertEqual(self.tr.snapshot()["state"], "STALLED")

    def test_slow_encoder_is_degraded_with_speed(self):
        self.tr.on_connect()
        self.tr.on_block(block(10))
        self.clock.t += 1
        self.tr.on_block(block(40, speed="0.82x"))
        snap = self.tr.snapshot()
        self.assertEqual(snap["state"], "DEGRADED")
        self.assertIn("0.82", snap["detail"])

    def test_restart_count_drops_and_failure_text(self):
        self.tr.on_connect()
        self.tr.on_block(block(10, drop=3))
        self.tr.on_connect()                     # ffmpeg restarted
        self.tr.on_connect()
        self.tr.on_block(block(5, drop=1))
        snap = self.tr.snapshot(supervisor={"state": "RECONNECTING", "detail": "unexpected end (exit=1)", "attempt": "2"})
        self.assertEqual(snap["restarts"], 2)
        self.assertEqual(snap["dropped_frames"], 3)
        self.assertIn("unexpected end", snap["last_failure"])
        self.assertEqual(snap["supervisor_attempt"], 2)

    def test_supervisor_failed_or_stopped_overrides(self):
        self.tr.on_connect()
        self.tr.on_block(block(10))
        self.assertEqual(self.tr.snapshot(supervisor={"state": "FAILED", "detail": "gave up"})["state"], "FAILED")
        self.assertEqual(self.tr.snapshot(supervisor={"state": "STOPPED"})["state"], "STOPPED")


class ParseTests(unittest.TestCase):
    def test_blocks_and_hostile_stream_is_bounded(self):
        lines = ["frame=1", "speed=1x", "progress=continue", "garbage", "frame=2", "progress=end"]
        blocks = list(health.parse_stream(lines))
        self.assertEqual([b["frame"] for b in blocks], ["1", "2"])
        flood = ["k%d=v" % i for i in range(500)] + ["progress=continue"]
        out = list(health.parse_stream(flood))
        self.assertTrue(all(len(b) <= 66 for b in out))


class ListenerTests(unittest.TestCase):
    def test_real_loopback_listener_feeds_tracker_and_survives_reconnect(self):
        tr = health.Tracker()
        srv = health.ProgressServer(tr)
        srv.start()
        self.addCleanup(srv.stop)
        self.assertGreater(srv.port, 1023)
        for frames in (30, 60):                  # two FFmpeg lifetimes
            with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as c:
                c.sendall(("frame=%d\nfps=30\nspeed=1.01x\ndrop_frames=0\nprogress=continue\n" % frames).encode())
            for _ in range(50):
                if tr.block.get("frame") == str(frames):
                    break
                time.sleep(0.05)
            self.assertEqual(tr.block.get("frame"), str(frames))
        self.assertEqual(tr.connections, 2)
        self.assertEqual(tr.snapshot()["restarts"], 1)

    def test_listener_binds_loopback_only(self):
        srv = health.ProgressServer(health.Tracker())
        self.addCleanup(srv.stop)
        self.assertEqual(srv.sock.getsockname()[0], "127.0.0.1")


class ProcAndFilesTests(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir("/proc/self"), "Linux /proc only")
    def test_sampler_reads_own_process(self):
        s = health.ProcSampler()
        a = s.sample(os.getpid(), now=1.0)
        self.assertGreater(a["rss_mb"], 1)
        sum(i * i for i in range(300000))
        b = s.sample(os.getpid(), now=2.0)
        self.assertIsNotNone(b["cpu_cores"])
        self.assertEqual(s.sample(0)["cpu_cores"], None)

    def test_health_file_roundtrip_and_staleness(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(health.read_health(d, "x")["state"], "UNKNOWN")
            health.write_health(d, "x", {"state": "HEALTHY", "updated": 1000})
            self.assertEqual(health.read_health(d, "x", now=1005)["state"], "HEALTHY")
            self.assertEqual(health.read_health(d, "x", now=1100)["state"], "STALE")
            with open(health.health_path(d, "x"), "w") as fh:
                fh.write("{broken")
            self.assertEqual(health.read_health(d, "x")["state"], "UNKNOWN")


class CliWiringTests(unittest.TestCase):
    def test_progress_is_pointed_at_the_sidecar_port_only_when_available(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_RUN_DIR": d}):
            args = ["ffmpeg", "-progress", "pipe:2", "-i", "x"]
            self.assertEqual(cli._attach_progress(args, "s", wait=0.1)[2], "pipe:2")   # no sidecar -> journal
            with open(health.port_path(d, "s"), "w") as fh:
                fh.write("45678")
            self.assertEqual(cli._attach_progress(args, "s", wait=0.1)[2], "tcp://127.0.0.1:45678")
            with open(health.port_path(d, "s"), "w") as fh:
                fh.write("80")                                                          # privileged port: refused
            self.assertEqual(cli._attach_progress(args, "s", wait=0.1)[2], "pipe:2")

    def test_health_pairs_are_bounded_and_clean(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BLUESTREAM_RUN_DIR": d}):
            health.write_health(d, "s", {"state": "DEGRADED", "detail": "x\x00\x1b[31m" + "a" * 500,
                                         "speed": 0.5, "updated": int(time.time())})
            pairs = dict(cli.health_pairs("s"))
            self.assertEqual(pairs["state"], "DEGRADED")
            self.assertLessEqual(len(pairs["detail"]), 200)
            self.assertNotIn("\x00", pairs["detail"])
            self.assertEqual(pairs["speed"], "0.5")
            self.assertEqual(pairs["cpu_cores"], "")


if __name__ == "__main__":
    unittest.main()
