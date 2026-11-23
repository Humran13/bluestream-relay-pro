"""Overlays on an EXISTING stream: activation switch, circuit breaker, compositor argv, API flows (swap vs one-time restart, confirmation,
rollback of the switch), shell guards. The real-stack behaviour (platform session continuity, decoded frames) is in test_studio_inplace_stack.py."""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webapp.engine import EngineClient, EngineError
from webapp.studio import capabilities, cli, overlay as ovl, store
from webapp.studio.model import OutputProfile, Overlay, Project, ValidationError
from webapp.test_studio_workspace import WS

ROOT = Path(__file__).resolve().parent.parent


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inplace-unit-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("studio", "run", "media"):
            os.makedirs(os.path.join(self.tmp, d))
        p = mock.patch.dict(os.environ, {"BLUESTREAM_STUDIO_DIR": self.tmp + "/studio", "BLUESTREAM_RUN_DIR": self.tmp + "/run",
                                         "BLUESTREAM_MEDIA_DIR": self.tmp + "/media", "BLUESTREAM_STUDIO_TEST_LOAD": "0"})
        p.start()
        self.addCleanup(p.stop)

    def publish(self, overlays, relay="dino", **kw):
        prof = kw.pop("profile", OutputProfile(width=0, height=0, fps=0))
        p = Project(relay + "-overlay", "relay", relay, overlays, prof, target_relay=relay).validate()
        store.save_draft(p)
        return store.publish(p.name)


class ModelTests(unittest.TestCase):
    def test_bound_project_rules(self):
        Project("d-overlay", "relay", "d", [], OutputProfile(width=0, height=0, fps=0), target_relay="d").validate()
        for bad in (Project("x", "media", "a.mp4", [], OutputProfile(width=0, height=0, fps=0)),          # match-source only for existing streams
                    Project("d-overlay", "media", "a.mp4", [], target_relay="d"),                        # must read that stream
                    Project("d-overlay", "relay", "other", [], target_relay="d"),
                    Project("d-overlay", "relay", "d", [], target_relay="Bad Name")):
            with self.assertRaises(ValidationError):
                bad.validate()

    def test_a_bound_project_with_layers_is_a_live_encode_and_structure_includes_the_target(self):
        a = Project("d-overlay", "relay", "d", [Overlay("text")], OutputProfile(width=0, height=0, fps=0), target_relay="d")
        self.assertTrue(a.runs_live_encode)
        b = dataclasses.replace(a, target_relay="")
        self.assertNotEqual(a.structure_fingerprint(), b.structure_fingerprint())


class MarkerAndBreakerTests(Env):
    def test_marker_round_trip_and_hardening(self):
        self.assertIsNone(ovl.read_marker("dino"))
        ovl.write_marker("dino", "dino-overlay", True)
        m = ovl.read_marker("dino")
        self.assertEqual((m["project"], m["enabled"]), ("dino-overlay", True))
        self.assertRegex(m["nonce"], r"^[0-9a-f]{16}$")
        n1 = m["nonce"]
        ovl.write_marker("dino", "dino-overlay", False)
        self.assertNotEqual(ovl.read_marker("dino")["nonce"], n1)                    # every write re-arms the breaker
        for bad in ("{not json", json.dumps({"project": "../x", "enabled": True}), json.dumps({"project": 5}), json.dumps([1])):
            open(ovl.marker_path("dino"), "w").write(bad)
            self.assertIsNone(ovl.read_marker("dino"), bad)                           # anything odd -> the relay streams as before
        with self.assertRaises(ValidationError):
            ovl.marker_path("../etc")

    def test_symlinked_marker_is_ignored(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        target = self.tmp + "/elsewhere.json"
        open(target, "w").write(json.dumps({"project": "x", "enabled": True}))
        try:
            os.symlink(target, ovl.marker_path("dino"))
        except OSError:
            self.skipTest("symlinks not permitted")
        self.assertIsNone(ovl.read_marker("dino"))

    def test_breaker_trips_only_on_a_retry_after_an_unproven_start(self):
        n = "a" * 16
        # deliberate (re)builds are always allowed, however quickly an editor applies changes
        for i in range(5):
            self.assertTrue(ovl.allow_start("dino", n, now=100 + i, reason="swap")[0])
        # the compositor ended by itself: a RETRY after a start that never proved healthy trips the breaker
        ok, why = ovl.allow_start("dino", n, now=110, reason="retry")
        self.assertFalse(ok)
        self.assertIn("switched off automatically", why)
        self.assertEqual(ovl.state_for_test("dino", n) if hasattr(ovl, "state_for_test") else ovl.tripped_reason("dino", n) != "", True)
        self.assertFalse(ovl.allow_start("dino", n, now=111, reason="swap")[0])        # stays off for THIS activation
        self.assertTrue(ovl.allow_start("dino", "b" * 16, now=112, reason="swap")[0])  # a fresh activation (new nonce) re-arms

    def test_a_healthy_run_forgets_earlier_starts(self):
        n = "c" * 16
        self.assertTrue(ovl.allow_start("dino", n, now=100, reason="start")[0])
        ovl.mark_healthy("dino")
        self.assertTrue(ovl.allow_start("dino", n, now=130, reason="retry")[0])        # one genuine crash after a proven run is retried
        self.assertFalse(ovl.allow_start("dino", n, now=140, reason="retry")[0])       # ...a second one right away is not

    def test_old_starts_expire(self):
        n = "d" * 16
        ovl.allow_start("dino", n, now=100, reason="start")
        self.assertTrue(ovl.allow_start("dino", n, now=100 + ovl.WINDOW_S + 5, reason="retry")[0])


class OverlayArgvTests(Env):
    SRC = ["-re", "-stream_loop", "-1", "-i", "/var/lib/bluestream/media/dino.mp4"]

    def info(self, **kw):
        from webapp.studio import planner
        base = dict(vcodec="h264", pix_fmt="yuv420p", width=1920, height=1080, fps=29.97, acodec="aac", live=False)
        base.update(kw)
        return planner.SourceInfo(**base)

    def argv(self, tokens=None, **kw):
        out = {}
        with mock.patch.object(cli.runtime, "probe_source", return_value=self.info(**kw)), \
                mock.patch.object(cli.sys, "stdout", mock.Mock(buffer=mock.Mock(write=lambda b: out.setdefault("b", b)))):
            rc = cli.cmd_overlay_argv("dino", tokens or self.SRC, kw.get("ts", 0.0) if False else 0.0)
        args = [a.decode() for a in out.get("b", b"").split(b"\0") if a]
        return rc, args

    def test_nothing_to_do_means_plain_copy(self):
        self.assertEqual(self.argv()[0], 8)                                          # no switch at all
        self.publish([Overlay("text")])
        self.assertEqual(self.argv()[0], 8)                                          # project exists but the switch is missing
        ovl.write_marker("dino", "dino-overlay", False)
        self.assertEqual(self.argv()[0], 8)                                          # switched off
        self.publish([Overlay("text", visible=False)])
        ovl.write_marker("dino", "dino-overlay", True)
        self.assertEqual(self.argv()[0], 8)                                          # nothing visible: copy is cheaper

    @unittest.skipUnless(sys.platform.startswith("linux"), "FFmpeg path checks need POSIX paths (runs in the Linux container/CI)")
    def test_composed_argv_keeps_the_source_resolution_rate_audio_and_bus_name(self):
        self.publish([Overlay("text", text="ON AIR", x=0.1, y=0.8, w=0.4, h=0.1), Overlay("rect", x=0.7, y=0.1, w=0.2, h=0.1)])
        ovl.write_marker("dino", "dino-overlay", True)
        rc, a = self.argv()
        self.assertEqual(rc, 0)
        self.assertEqual(a[a.index("-i") + 1], "/var/lib/bluestream/media/dino.mp4")
        self.assertEqual(a[a.index("-stream_loop") + 1], "-1")                       # the relay's own loop/reconnect handling is reused
        self.assertEqual(a[a.index("-c:a") + 1], "copy")                             # audio is never re-encoded
        self.assertNotIn("copy", a[a.index("-c:v"):a.index("-c:v") + 2])
        self.assertEqual(a[-3:], ["-f", "flv", "rtmp://127.0.0.1:1935/bluestream-relay/dino"])
        g = a[a.index("-filter_complex") + 1]
        self.assertIn("scale=1920:1080", g)                                          # match source: no resize
        self.assertIn("fps=30", g)
        self.assertNotIn("-output_ts_offset", a)

    @unittest.skipUnless(sys.platform.startswith("linux"), "FFmpeg path checks need POSIX paths (runs in the Linux container/CI)")
    def test_the_timeline_offset_is_added_only_when_given(self):
        self.publish([Overlay("text")])
        ovl.write_marker("dino", "dino-overlay", True)
        out = {}
        with mock.patch.object(cli.runtime, "probe_source", return_value=self.info()), \
                mock.patch.object(cli.sys, "stdout", mock.Mock(buffer=mock.Mock(write=lambda b: out.setdefault("b", b)))):
            self.assertEqual(cli.cmd_overlay_argv("dino", self.SRC, 123.456), 0)
        a = out["b"].decode().split("\0")
        self.assertEqual(a[a.index("-output_ts_offset") + 1], "123.456")

    def test_every_refusal_is_a_distinct_exit_code_so_the_relay_falls_back_to_plain_copy(self):
        self.publish([Overlay("text")])
        ovl.write_marker("dino", "dino-overlay", True)
        with mock.patch.object(capabilities, "encoder_usable", return_value=(False, "no GPU")):
            self.assertEqual(self.argv()[0], 6)
        with mock.patch.object(cli.planner, "admit", return_value=(False, "busy")):
            self.assertEqual(self.argv()[0], 3)
        self.assertEqual(self.argv(tokens=["-re"])[0], 2)                            # no -i: refuse rather than guess
        store.save_draft(Project("other-overlay", "relay", "other", [Overlay("text")], OutputProfile(width=0, height=0, fps=0), target_relay="other").validate())
        store.publish("other-overlay")
        ovl.write_marker("dino", "other-overlay", True)                              # a project bound to ANOTHER stream
        self.assertEqual(self.argv()[0], 2)
        open(self.tmp + "/studio/dino-overlay.json", "w").write("{broken")
        ovl.write_marker("dino", "dino-overlay", True)
        self.assertEqual(self.argv()[0], 2)

    def test_a_tripped_breaker_refuses_with_exit_7(self):
        self.publish([Overlay("text")])
        ovl.write_marker("dino", "dino-overlay", True)
        n = ovl.read_marker("dino")["nonce"]
        ovl.allow_start("dino", n, reason="start")
        ovl.allow_start("dino", n, reason="retry")
        self.assertEqual(self.argv()[0], 7)

    def test_main_parses_options_strictly(self):
        for bad in (["overlay-argv", "dino", "--ts-offset", "abc", "--", "-i", "x"], ["overlay-argv", "dino", "--ts-offset", "-1", "--", "-i", "x"],
                    ["overlay-argv", "dino", "--reason", "weird", "--", "-i", "x"], ["overlay-argv", "dino", "-i", "x"], ["overlay-argv", "../x", "--", "-i", "x"]):
            self.assertEqual(cli.main(["studio.cli"] + bad), 2, bad)

    def test_health_pairs_report_the_overlay_state(self):
        self.assertEqual(dict(cli.health_pairs("dino"))["overlay"], "")
        ovl.write_marker("dino", "dino-overlay", True)
        self.assertEqual(dict(cli.health_pairs("dino"))["overlay"], "on")
        n = ovl.read_marker("dino")["nonce"]
        ovl.allow_start("dino", n, reason="start")
        ovl.allow_start("dino", n, reason="retry")
        self.assertEqual(dict(cli.health_pairs("dino"))["overlay"], "tripped")
        ovl.write_marker("dino", "dino-overlay", False)
        self.assertEqual(dict(cli.health_pairs("dino"))["overlay"], "off")


class InPlaceEngine:
    """Adds the in-place operations to the Studio test engine."""


class ApiTests(WS):
    def setUp(self):
        super().setUp()
        self.payloads["relay_list"] = [{"name": "dino", "type": "local-file", "active": "yes", "health": "HEALTHY", "source": "/var/lib/bluestream/media/loop.mp4"},
                                       {"name": "wp", "type": "studio", "active": "yes"}, {"name": "idle", "type": "url", "active": "no"}]
        self.payloads["swap"] = {"active": "yes", "ready": "yes", "reason": ""}
        eng = self.engine
        eng.relay_swap_ready = lambda name: self.payloads["swap"]
        eng.relay_swap_source = lambda name: eng._mutation("relay_swap_source", name)
        self.name = "dino-overlay"

    def ready_project(self, overlays=None):
        self.jpost("/console/studio/overlay/new.json", {"relay": "dino"})
        p = store.load_draft(self.name)
        p.overlays = overlays if overlays is not None else [Overlay("text", text="ON AIR", x=0.1, y=0.8, w=0.4, h=0.1)]
        store.save_draft(p.validate())
        return self.jpost("/console/studio/%s/publish.json" % self.name, {}).get_json()

    def act(self, verb, **body):
        return self.jpost("/console/studio/%s/overlay/%s.json" % (self.name, verb), body)

    def test_creating_the_project_never_touches_the_stream(self):
        d = self.jpost("/console/studio/overlay/new.json", {"relay": "dino"}).get_json()
        self.assertEqual((d["ok"], d["name"], d["existing"]), (True, "dino-overlay", False))
        p = store.load_draft("dino-overlay")
        self.assertEqual((p.target_relay, p.source_kind, p.source_ref, p.profile.width), ("dino", "relay", "dino", 0))
        self.assertTrue(self.jpost("/console/studio/overlay/new.json", {"relay": "dino"}).get_json()["existing"])    # idempotent
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertIsNone(ovl.read_marker("dino"))
        for bad, code in (({"relay": "wp"}, 409), ({"relay": "ghost"}, 404), ({"relay": "../x"}, 400), ({}, 400)):
            self.assertEqual(self.jpost("/console/studio/overlay/new.json", bad).status_code, code, bad)

    def test_apply_before_activation_only_saves_the_layout_as_ready(self):
        d = self.ready_project()
        self.assertEqual(d["mode"], "stored")
        self.assertIn("NOT on air", d["message"])
        self.assertIsNone(ovl.read_marker("dino"))
        self.assertEqual(self.engine.mutation_calls, [])                              # nothing about the stream changed

    def test_activate_with_a_ready_supervisor_swaps_without_restarting_the_unit(self):
        self.ready_project()
        d = self.act("activate").get_json()
        self.assertEqual(d["mode"], "swap")
        self.assertEqual(self.engine.mutation_calls, [("relay_swap_source", "dino")])
        self.assertTrue(ovl.read_marker("dino")["enabled"])
        self.assertNotIn("relay_restart", [c[0] for c in self.engine.mutation_calls])
        self.assertNotIn("relay_destinations_set", [c[0] for c in self.engine.mutation_calls])   # destinations/keys untouched

    def test_a_supervisor_that_predates_the_update_needs_an_explicit_one_time_restart(self):
        self.payloads["swap"] = {"active": "yes", "ready": "no", "reason": "OLD_SUPERVISOR"}
        self.ready_project()
        r = self.act("activate")
        self.assertEqual(r.status_code, 409)
        j = r.get_json()
        self.assertEqual((j["needs_restart"], j["kind"]), (True, "unit-restart"))
        self.assertIn("same stream key", j["error"])
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertIsNone(ovl.read_marker("dino"))                                   # nothing was written either
        d = self.act("activate", confirm_restart=True).get_json()
        self.assertEqual(d["mode"], "unit-restart")
        self.assertEqual(self.engine.mutation_calls, [("relay_restart", "dino")])

    def test_a_stopped_stream_is_armed_not_touched(self):
        self.payloads["relay_list"][0]["active"] = "no"
        self.ready_project()
        d = self.act("activate").get_json()
        self.assertEqual(d["mode"], "armed")
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertTrue(ovl.read_marker("dino")["enabled"])

    def test_activation_refuses_when_there_is_nothing_valid_to_put_on_air(self):
        self.jpost("/console/studio/overlay/new.json", {"relay": "dino"})
        self.assertEqual(self.act("activate").status_code, 409)                       # never applied
        self.ready_project([])
        self.assertEqual(self.act("activate").status_code, 409)                       # no layers
        self.assertEqual(self.jpost("/console/studio/%s/publish.json" % self.name, {}).status_code, 200)
        p = store.load_draft(self.name)
        p.overlays = [Overlay("image", asset="ghost.png", w=0.1, h=0.1)]
        store.save_draft(p.validate())
        self.assertEqual(self.jpost("/console/studio/%s/publish.json" % self.name, {}).status_code, 422)   # apply itself refuses missing media
        store.publish(self.name)                                                       # ...but even if it got published some other way:
        self.assertEqual(self.act("activate").status_code, 422)                       # activation refuses it too
        with mock.patch.object(capabilities, "encoder_usable", return_value=(False, "no GPU")):
            self.ready_project([Overlay("text")])
            self.assertEqual(self.act("activate").status_code, 422)
        self.assertEqual(self.engine.mutation_calls, [])
        self.assertIsNone(ovl.read_marker("dino"))

    def test_a_failed_swap_puts_the_switch_back_and_leaves_the_stream_alone(self):
        self.ready_project()
        self.engine.relay_swap_source = lambda name: (_ for _ in ()).throw(EngineError("boom"))
        r = self.act("activate")
        self.assertEqual(r.status_code, 500)
        self.assertIn("previous state was restored", r.get_json()["error"])
        self.assertIsNone(ovl.read_marker("dino"))
        self.assertEqual(self.engine.mutation_calls, [])

    def test_deactivate_returns_to_plain_copy_with_a_swap_and_is_a_noop_when_not_on_air(self):
        self.ready_project()
        self.assertEqual(self.act("deactivate").get_json()["mode"], "none")
        self.act("activate")
        self.engine.mutation_calls.clear()
        d = self.act("deactivate").get_json()
        self.assertEqual(d["mode"], "swap")
        self.assertFalse(ovl.read_marker("dino")["enabled"])
        self.assertEqual(self.engine.mutation_calls, [("relay_swap_source", "dino")])

    def test_apply_while_on_air_hot_for_text_and_confirmed_swap_for_structure(self):
        self.ready_project()
        self.act("activate")
        self.engine.mutation_calls.clear()
        p = store.load_draft(self.name)
        p.overlays[0] = dataclasses.replace(p.overlays[0], text="NEW HEADLINE")
        store.save_draft(p)
        d = self.jpost("/console/studio/%s/publish.json" % self.name, {}).get_json()
        self.assertEqual(d["mode"], "hot")
        self.assertEqual(self.engine.mutation_calls, [])                              # no signal, no restart: FFmpeg untouched
        p = store.load_draft(self.name)
        p.overlays[0] = dataclasses.replace(p.overlays[0], x=0.5)
        store.save_draft(p)
        r = self.jpost("/console/studio/%s/publish.json" % self.name, {})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["kind"], "swap")
        self.assertEqual(self.engine.mutation_calls, [])
        d = self.jpost("/console/studio/%s/publish.json" % self.name, {"restart": True}).get_json()
        self.assertEqual(d["mode"], "swap")
        self.assertEqual(self.engine.mutation_calls, [("relay_swap_source", "dino")])

    def test_status_reports_the_existing_stream_and_swap_readiness(self):
        self.ready_project()
        self.act("activate")
        self.engine.read = lambda op, *v: {"studio_health": {"state": "HEALTHY", "revision": "1", "plan": "overlay-live-composition", "overlay": "on"},
                                           "relay_publisher_status": [{"id": "yt", "display_name": "YouTube", "state": "LIVE"}]}[op]
        s = self.client.get("/console/studio/%s/status.json" % self.name).get_json()["stream"]
        self.assertTrue(s["exists"] and s["active"])
        self.assertEqual((s["inplace"]["relay"], s["inplace"]["marker"], s["inplace"]["swap"]["ready"], s["inplace"]["overlay"]), ("dino", "on", True, "on"))
        self.assertEqual(s["plan"], "overlay-live-composition")
        self.assertEqual(s["publishers"][0]["state"], "LIVE")

    def test_studio_never_creates_second_publishers_or_touches_destinations_of_an_existing_stream(self):
        self.jpost("/console/studio/overlay/new.json", {"relay": "dino"})
        for url, body in (("stream.json", {"action": "create"}), ("stream.json", {"action": "start"}), ("stream.json", {"action": "stop"}),
                          ("takeover.json", {"destination": "yt", "confirm": True}), ("destinations.json", {"ids": ["yt"]})):
            self.assertEqual(self.jpost("/console/studio/%s/%s" % (self.name, url), body).status_code, 409, url)
        self.assertEqual(self.engine.mutation_calls, [])

    def test_the_stream_a_project_edits_cannot_be_redirected(self):
        self.jpost("/console/studio/overlay/new.json", {"relay": "dino"})
        for proj in ({"target_relay": "other"}, {"source_ref": "other"}, {"source_kind": "media", "source_ref": "a.mp4"}):
            self.assertEqual(self.jpost("/console/studio/%s/save.json" % self.name, {"project": proj}).status_code, 400, proj)
        self.assertEqual(self.jpost("/console/studio/%s/save.json" % self.name, {"project": {"target_relay": "dino"}}).status_code, 200)

    def test_monitoring_shows_the_raw_file_for_a_local_file_stream_so_layers_are_not_drawn_twice(self):
        self.jpost("/console/studio/overlay/new.json", {"relay": "dino"})
        self.payloads["media_list"] = [{"name": "loop.mp4", "size": 10}]
        d = self.client.get("/console/studio/%s/monitor.json" % self.name).get_json()
        self.assertIn("/studio/media/loop.mp4/play.mp4", d["source"]["raw_url"])


class ShellGuardTests(unittest.TestCase):
    def test_supervisor_swap_and_bridge_are_safe(self):
        sup = (ROOT / "lib" / "supervise.sh").read_text(encoding="utf-8")
        self.assertIn("trap _bs_supervise_on_swap USR1", sup)
        self.assertIn('BS_SUPERVISE_REASON=retry', sup)
        wc = (ROOT / "web-ctl").read_text(encoding="utf-8")
        # a process WITHOUT a USR1 handler would be killed by the signal: the bridge must check the caught-signal mask first
        self.assertLess(wc.index("wc_pid_catches_usr1 \"$pid\" || wc_fail"), wc.index("kill -USR1"))
        self.assertIn("0x200", wc)
        self.assertNotIn("kill -9", wc.split("wc_op_relay_swap_source()")[1].split("wc_op_destination_create()")[0])

    def test_relay_hook_falls_back_to_plain_copy_and_never_touches_other_relay_types(self):
        rel = (ROOT / "lib" / "relay.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$RELAY_TYPE" != "studio" ] || return 1', rel)
        self.assertIn("relay_overlay_build \"${input[@]}\" && return 0", rel)
        # every plain relay keeps the original output: copy video, copy audio, flv to the bus
        self.assertIn('-c:v copy\n        -c:a copy\n        -f flv', rel)
        run = (ROOT / "config" / "systemd" / "run-relay.sh").read_text(encoding="utf-8")
        self.assertIn("relay_overlay_stop_sidecar", run)

    def test_verifier_accepts_nvenc_only_when_encoding_is_allowed(self):
        common = (ROOT / "lib" / "common.sh").read_text(encoding="utf-8")
        self.assertIn("libx264|aac|h264_nvenc", common)

    @unittest.skipUnless(shutil.which("bash") and sys.platform.startswith("linux"), "needs bash on Linux")
    def test_plain_relays_keep_their_exact_command_line(self):
        """No overlay switch -> byte-identical stream-copy argv (no timestamp offset, no encode)."""
        script = ('set -u; export BLUESTREAM_RUN_DIR=%(t)s/run BLUESTREAM_ETC_DIR=%(t)s/etc BLUESTREAM_STUDIO_DIR=%(t)s/studio\n'
                  '. %(r)s/lib/common.sh; . %(r)s/lib/relay.sh\n'
                  'BLUESTREAM_STUDIO_DIR=%(t)s/studio\n'
                  'RELAY_NAME=dino; RELAY_TYPE=local-file; RELAY_URL=/m/a.mp4; RELAY_LOOP=yes\n'
                  'relay_build_ffmpeg_args || exit 1\n'
                  'printf "%%s\\n" "${FFMPEG_ARGS[*]}"\n')
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(t + "/studio")
            r = subprocess.run(["bash", "-c", script % {"t": t, "r": str(ROOT)}], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "-nostdin -y -loglevel warning -re -stream_loop -1 -i /m/a.mp4 -map 0:v:0 -map 0:a:0? -c:v copy -c:a copy -f flv rtmp://127.0.0.1:1935/bluestream-relay/dino")


if __name__ == "__main__":
    unittest.main()
