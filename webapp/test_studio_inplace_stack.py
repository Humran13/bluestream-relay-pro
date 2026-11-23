"""Overlays on an EXISTING stream, end to end against the REAL stack pieces (Linux container only):
real nginx-rtmp (the repo's own template), real lib/relay.sh + lib/supervise.sh (the relay/supervisor logic, SIGUSR1 swap included),
the real Studio runtime (`overlay-argv`, widgets sidecar), a destination-publisher emulator that uses the SAME FFmpeg argument shape as
lib/publisher.sh (pull the bus, `-c copy`, push) restarted like systemd would, and a 'platform' RTMP app that RECORDS what a platform
would receive. Everything is judged from the DECODED platform-side recording (pictures, audio beeps, timestamps), never from API replies.

Not covered: systemd, setpriv, the real publisher units, YouTube itself. Set BS_INPLACE=1 (done by the container runner) to enable."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENABLED = os.environ.get("BS_INPLACE") == "1" and shutil.which("nginx") and shutil.which("ffmpeg") and sys.platform.startswith("linux")
NGINX_MODULE = "/usr/lib/nginx/modules/ngx_rtmp_module.so"
sys.path.insert(0, str(REPO))


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def port_open():
    return sh(["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/1935"]).returncode == 0


class Publisher(threading.Thread):
    """What a destination publisher unit does: pull the bus, copy, push; if ffmpeg ends, systemd restarts it after 3 s."""

    def __init__(self, bus, sink):
        super().__init__(daemon=True)
        self.bus, self.sink, self.starts, self.stop_flag, self.p = bus, sink, [], False, None

    def run(self):
        while not self.stop_flag:
            self.p = subprocess.Popen(["ffmpeg", "-v", "error", "-fflags", "+genpts", "-rw_timeout", "20000000", "-i", self.bus, "-c:v", "copy", "-c:a", "copy",
                                       "-f", "flv", self.sink], stderr=open(os.environ["BLUESTREAM_RUN_DIR"] + "/../logs/pub.err", "a"))
            self.starts.append(time.time())
            self.p.wait()
            if not self.stop_flag:
                time.sleep(3)


@unittest.skipUnless(ENABLED, "needs the Linux container with nginx-rtmp + ffmpeg (BS_INPLACE=1)")
class InPlaceOverlayStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp.studio import overlay as ovl, store  # noqa: F401
        cls.tmp = t = tempfile.mkdtemp(prefix="inplace-")
        for d in ("hls", "media", "run", "studio", "etc", "logs", "pylib", "nginx", "sink"):
            os.makedirs("%s/%s" % (t, d), exist_ok=True)
        os.symlink(str(REPO / "webapp" / "studio"), t + "/pylib/studio")
        tpl = (REPO / "config" / "nginx" / "rtmp.conf.template").read_text()
        rtmp = tpl.replace("__RTMP_BIND__", "127.0.0.1").replace("__RTMP_PORT__", "1935").replace("__HLS_ROOT__", t + "/hls")
        rtmp = rtmp.replace("rtmp {", "rtmp {\n    access_log off;", 1)
        rtmp = rtmp.replace("application bluestream-relay {", "application sink { live on; record all; record_path %s/sink; record_unique on; record_suffix .flv; }\n"
                            "        application bluestream-relay {" % t, 1)
        conf = ("load_module %s;\nworker_processes 1;\npid %s/nginx/nginx.pid;\nerror_log %s/logs/nginx-error.log info;\nevents { worker_connections 256; }\n"
                "http { access_log off; client_body_temp_path %s/nginx/body; proxy_temp_path %s/nginx/proxy; fastcgi_temp_path %s/nginx/fcgi; "
                "uwsgi_temp_path %s/nginx/uwsgi; scgi_temp_path %s/nginx/scgi; }\n%s\n" % (NGINX_MODULE, t, t, t, t, t, t, t, rtmp))
        open(t + "/nginx/nginx.conf", "w").write(conf)
        if port_open():
            raise RuntimeError("port 1935 already in use")
        cls.nginx = subprocess.Popen(["nginx", "-c", t + "/nginx/nginx.conf", "-g", "daemon off;"], stdout=subprocess.DEVNULL,
                                     stderr=open(t + "/logs/nginx.out", "w"), start_new_session=True)
        for _ in range(50):
            if port_open():
                break
            time.sleep(0.2)
        else:
            raise unittest.SkipTest("nginx-rtmp did not start")
        cls.env = dict(os.environ, PYTHONPATH=t + "/pylib", BLUESTREAM_STUDIO_DIR=t + "/studio", BLUESTREAM_RUN_DIR=t + "/run",
                       BLUESTREAM_MEDIA_DIR=t + "/media", BLUESTREAM_ETC_DIR=t + "/etc", BLUESTREAM_HLS_ROOT=t + "/hls",
                       BLUESTREAM_STUDIO_CACHE=t + "/run/studio-cache", BLUESTREAM_STUDIO_MAX_LIVE="3", BLUESTREAM_STUDIO_TEST_LOAD="0", BLUESTREAM_STUDIO_PYPATH=t + "/pylib",
                       BS_REPO=str(REPO), BS_RELAY="dino", BS_RELAY_TYPE="local-file", BS_RELAY_URL=t + "/media/src.mp4", BS_RELAY_LOOP="yes")
        cls._saved = {k: os.environ.get(k) for k in cls.env if k.startswith("BLUESTREAM_")}
        os.environ.update({k: v for k, v in cls.env.items() if k.startswith("BLUESTREAM_")})
        # 1280x720@25 H.264/AAC, 20 s, with a SYNC MARKER: a white corner square and a 1 kHz beep at the same instants (first 0.12 s of every second)
        sh(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "color=c=0x101020:s=1280x720:r=25,drawbox=x=0:y=0:w=64:h=64:color=white:t=fill:enable='lt(mod(t,1),0.12)'",
            "-f", "lavfi", "-i", "aevalsrc='0.8*sin(2*PI*1000*t)*lt(mod(t,1),0.12)':s=44100:d=20", "-t", "20", "-c:v", "libx264", "-preset", "veryfast",
            "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", t + "/media/src.mp4"], check=True)
        sh(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:s=200x100:d=1", "-frames:v", "1", t + "/media/logo.png"], check=True)
        cls.sup = cls.pub = None
        cls.marks = []            # (label, wall time)

    @classmethod
    def tearDownClass(cls):
        if cls.pub:
            cls.pub.stop_flag = True
            if cls.pub.p and cls.pub.p.poll() is None:
                cls.pub.p.terminate()
        if cls.sup and cls.sup.poll() is None:
            try:
                os.killpg(cls.sup.pid, signal.SIGTERM)
                cls.sup.wait(timeout=15)
            except Exception:
                os.killpg(cls.sup.pid, signal.SIGKILL)
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "studio.cli"], capture_output=True)
        try:
            os.killpg(cls.nginx.pid, signal.SIGTERM)
            cls.nginx.wait(timeout=8)
        except Exception:
            pass
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        keep = os.environ.get("BS_INPLACE_KEEP")
        if keep:
            try:
                shutil.rmtree(keep, ignore_errors=True)
                shutil.copytree(cls.tmp, keep, ignore=shutil.ignore_patterns("pylib", "hls"), dirs_exist_ok=True, copy_function=shutil.copyfile)
            except (OSError, shutil.Error):
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------------------------------------------------------------
    def ffpids(self):
        out = sh(["pgrep", "-f", "bluestream-relay/dino"]).stdout.split()
        return [int(x) for x in out if int(x) != os.getpid()]

    def cmdline(self, pid):
        try:
            return open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return ""

    def bus_ffmpeg(self):
        for pid in self.ffpids():
            c = self.cmdline(pid)
            if c.startswith("ffmpeg") and "-f flv rtmp://127.0.0.1:1935/bluestream-relay/dino" in c:
                return pid, c
        return None, ""

    def mode(self):
        pid, c = self.bus_ffmpeg()
        return "none" if pid is None else ("copy" if "-c:v copy" in c else "composed")

    def wait(self, cond, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return time.time()
            time.sleep(0.1)
        tails = ""
        for f in ("pub.err", "nginx-error.log", "sup.out"):
            try:
                tails += "\n--- %s\n%s" % (f, open(self.tmp + "/logs/" + f).read()[-700:])
            except OSError:
                pass
        try:
            tails += "\n--- sink dir: %s" % [(f, os.stat(self.tmp + "/sink/" + f).st_size) for f in os.listdir(self.tmp + "/sink")]
        except OSError:
            pass
        self.fail("timed out waiting for " + what + tails)

    def sink_size(self):
        files = sorted(os.listdir(self.tmp + "/sink"))
        return sum(os.stat(self.tmp + "/sink/" + f).st_size for f in files)

    def cpu_ticks(self, pid):
        try:
            f = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()
            return int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            return None

    def rss_mb(self, pid):
        try:
            for ln in open("/proc/%d/status" % pid):
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) / 1024.0
        except OSError:
            pass
        return None

    def measure(self, seconds):
        """(avg CPU % of one core, max RSS MB) of the bus FFmpeg over `seconds`."""
        pid, _ = self.bus_ffmpeg()
        if pid is None:
            return None, None
        hz = os.sysconf("SC_CLK_TCK")
        t0, c0, rss = time.time(), self.cpu_ticks(pid), []
        while time.time() - t0 < seconds:
            time.sleep(1)
            rss.append(self.rss_mb(pid) or 0)
        c1 = self.cpu_ticks(pid)
        if c0 is None or c1 is None:
            return None, None
        return round(100.0 * (c1 - c0) / hz / (time.time() - t0), 1), round(max(rss), 0)

    def project(self, overlays, encoder="libx264", relay="dino"):
        from webapp.studio import store
        from webapp.studio.model import OutputProfile, Project
        p = Project("dino-overlay", "relay", relay, overlays, OutputProfile(width=0, height=0, fps=0, preset="veryfast", video_kbps=3000, encoder=encoder),
                    target_relay=relay).validate()
        store.save_draft(p)
        return store.publish("dino-overlay")

    def switch(self, enabled=True):
        from webapp.studio import overlay as ovl
        ovl.write_marker("dino", "dino-overlay", enabled)

    def swap(self, label):
        """What the dashboard's 'swap' does: SIGUSR1 to the relay supervisor. Returns the longest time the platform recording did not grow."""
        self.marks.append((label, time.time()))
        before_pid, _ = self.bus_ffmpeg()
        size, last_growth, worst = self.sink_size(), time.time(), 0.0
        os.kill(self.sup.pid, signal.SIGUSR1)
        end = time.time() + 14
        changed_at = None
        while time.time() < end:
            time.sleep(0.05)
            now_size = self.sink_size()
            if now_size > size:
                worst = max(worst, time.time() - last_growth)
                last_growth, size = time.time(), now_size
            pid, _ = self.bus_ffmpeg()
            if changed_at is None and pid not in (None, before_pid):
                changed_at = time.time()
            if changed_at and time.time() - changed_at > 3:
                break
        worst = max(worst, time.time() - last_growth)
        return round(worst, 2)

    # ---- decoded-output analysis -----------------------------------------------------------------------------------------
    def luma(self, path, box):
        x, y, w, h = box
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "crop=%d:%d:%d:%d,scale=1:1,format=gray" % (w, h, x, y), "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                           capture_output=True)
        return list(r.stdout)

    def luma_max(self, path, box):
        """Per frame: the brightest cell of a 16x8 grid over the box (thin text strokes survive, unlike a plain average)."""
        x, y, w, h = box
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "crop=%d:%d:%d:%d,scale=16:8:flags=area,format=gray" % (w, h, x, y), "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                           capture_output=True)
        d = r.stdout
        return [max(d[i:i + 128]) for i in range(0, len(d) - 127, 128)]

    def rgb(self, path, box):
        x, y, w, h = box
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "crop=%d:%d:%d:%d,scale=1:1,format=rgb24" % (w, h, x, y), "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
                           capture_output=True)
        d = r.stdout
        return [tuple(d[i:i + 3]) for i in range(0, len(d) - 2, 3)]

    def frame_times(self, path):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=pts_time", "-of", "csv=p=0", path], capture_output=True, text=True)
        return [float(x.strip(",")) for x in r.stdout.split() if x.strip(",") not in ("", "N/A")]

    def packet_steps(self, path, stream):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=dts_time", "-of", "csv=p=0", path], capture_output=True, text=True)
        ts = [float(x.strip(",")) for x in r.stdout.split() if x.strip(",") not in ("", "N/A")]
        return [round(b - a, 3) for a, b in zip(ts, ts[1:])]

    def packet_span(self, path, stream):
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=dts_time", "-of", "csv=p=0", path], capture_output=True, text=True)
        ts = [float(x.strip(",")) for x in r.stdout.split() if x.strip(",") not in ("", "N/A")]
        return (ts[0], ts[-1]) if ts else (0.0, 0.0)

    def beeps(self, path):
        """Start times (ABSOLUTE audio pts, s) of each 1 kHz beep, from per-frame peak levels (so a timestamp gap/overlap shows up)."""
        r = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi", "-i", "amovie=%s,astats=metadata=1:reset=1" % path, "-show_entries",
                            "frame=pts_time:frame_tags=lavfi.astats.Overall.Peak_level", "-of", "csv=p=0"], capture_output=True, text=True)
        out, prev = [], False
        for ln in r.stdout.splitlines():
            parts = ln.split(",")
            if len(parts) < 2:
                continue
            try:
                t, peak = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            loud = peak > -8.0
            if loud and not prev:
                out.append(t)
            prev = loud
        return out

    @staticmethod
    def runs(states):
        out = []
        for s in states:
            if not out or out[-1][0] != s:
                out.append([s, 1])
            else:
                out[-1][1] += 1
        return out

    # ---- the scenario ---------------------------------------------------------------------------------------------------------
    def test_existing_stream_gets_overlays_edited_and_removed_while_the_platform_session_stays_up(self):
        from webapp.studio import overlay as ovl, store
        from webapp.studio.model import Overlay
        t = self.tmp
        # the existing relay, exactly as production runs it: real relay.sh + real supervise.sh
        self.__class__.sup = subprocess.Popen(["bash", str(REPO / "tools" / "docker" / "relay_inplace_harness.sh")], env=self.env,
                                              stdout=open(t + "/logs/sup.out", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        self.wait(lambda: self.mode() == "copy", 40, "the plain relay to start (stream copy)")
        self.__class__.pub = Publisher("rtmp://127.0.0.1:1935/bluestream-relay/dino", "rtmp://127.0.0.1:1935/sink/key")
        self.pub.start()
        self.wait(lambda: self.sink_size() > 60000, 30, "the platform to receive the plain stream")
        time.sleep(6)
        cpu_copy, rss_copy = self.measure(8)
        sup_pid = self.sup.pid
        stalls = {}

        # P1: switch overlays ON (a red block and text). One swap. The publisher must NOT reconnect.
        red = Overlay("rect", x=0.7, y=0.15, w=0.2, h=0.1, color="#ff0000", opacity=1.0)
        txt = Overlay("text", text="ON AIR", x=0.05, y=0.8, w=0.4, h=0.12, size=60)
        self.project([red, txt])
        self.switch(True)
        stalls["activate"] = self.swap("activate")
        self.wait(lambda: self.mode() == "composed", 30, "the compositor to be on air")
        time.sleep(4)
        cpu_comp, rss_comp = self.measure(15)
        # P2: content-only edit: HOT, no swap, no restart
        pid_before = self.bus_ffmpeg()[0]
        self.marks.append(("hot-text", time.time()))
        self.project([red, Overlay("text", text="ON AIR NOW 12345", x=0.05, y=0.8, w=0.4, h=0.12, size=60)])
        time.sleep(8)
        self.assertEqual(self.bus_ffmpeg()[0], pid_before, "a content-only change must not restart FFmpeg")
        # P3: move + recolour the block (structure changes): one swap
        green = Overlay("rect", x=0.7, y=0.45, w=0.2, h=0.1, color="#00ff00", opacity=1.0)
        self.project([green, Overlay("text", text="ON AIR NOW 12345", x=0.05, y=0.8, w=0.4, h=0.12, size=60)])
        stalls["move"] = self.swap("move")
        time.sleep(5)
        # P4: disable the block (hidden layer): one swap
        hidden = Overlay("rect", x=0.7, y=0.45, w=0.2, h=0.1, color="#00ff00", opacity=1.0, visible=False)
        self.project([hidden, Overlay("text", text="ON AIR NOW 12345", x=0.05, y=0.8, w=0.4, h=0.12, size=60)])
        stalls["disable"] = self.swap("disable")
        time.sleep(5)
        # P5: remove every overlay: the relay must go back to cheap STREAM COPY
        self.project([])
        stalls["remove"] = self.swap("remove")
        self.wait(lambda: self.mode() == "copy", 20, "stream copy to return")
        time.sleep(6)
        marks_end = time.time()

        # ---- failure recovery: nothing here may permanently break the stream ------------------------------------------------
        starts_before_failures = len(self.pub.starts)
        # F1: an unusable (corrupt) project
        open(t + "/studio/dino-overlay.json", "w").write("{not json")
        self.switch(True)
        stalls["corrupt-project"] = self.swap("corrupt-project")
        self.wait(lambda: self.mode() == "copy", 25, "the plain stream after a corrupt project")
        # F2: NVENC requested on a machine that cannot do it -> refused, plain stream kept (never a silent software switch)
        from webapp.studio import capabilities
        if not capabilities.probe_nvenc(force=True)[0]:
            self.project([Overlay("text", text="X", x=0.1, y=0.8, w=0.3, h=0.1, size=40)], encoder="h264_nvenc")
            self.switch(True)
            stalls["nvenc-refused"] = self.swap("nvenc-refused")
            self.wait(lambda: self.mode() == "copy", 25, "the plain stream after a refused encoder")
        # F3: a compositor that dies at once (missing picture file) crash-loops -> the breaker trips and the plain stream is restored
        self.project([Overlay("image", asset="missing.png", x=0.1, y=0.1, w=0.1, h=0.1)])
        self.switch(True)
        t_fail = time.time()
        os.kill(self.sup.pid, signal.SIGUSR1)
        nonce = ovl.read_marker("dino")["nonce"]
        self.wait(lambda: ovl.tripped_reason("dino", nonce) != "" and self.mode() == "copy", 60, "the circuit breaker to restore the plain stream")
        recover_s = round(time.time() - t_fail, 1)
        time.sleep(5)
        # a healthy compositor can be activated again afterwards (fresh activation re-arms the breaker)
        self.project([Overlay("text", text="BACK", x=0.05, y=0.8, w=0.4, h=0.12, size=60)])
        self.switch(True)
        stalls["re-activate"] = self.swap("re-activate")
        self.wait(lambda: self.mode() == "composed", 30, "the compositor again")
        time.sleep(6)
        cpu_comp2, rss_comp2 = self.measure(10)
        publisher_starts = len(self.pub.starts)

        # ---- stop everything cleanly and analyse what the PLATFORM received ----------------------------------------------------
        t_stop = time.time()
        self.pub.stop_flag = True
        self.pub.p.terminate()
        time.sleep(2)
        files = sorted(os.listdir(t + "/sink"))
        report = {"platform_sessions": len(files), "publisher_starts": publisher_starts, "stall_s": stalls, "breaker_recovery_s": recover_s,
                  "cpu_pct_one_core": {"stream_copy": cpu_copy, "composed": cpu_comp, "composed_again": cpu_comp2},
                  "rss_mb": {"stream_copy": rss_copy, "composed": rss_comp, "composed_again": rss_comp2}}
        print("\nINPLACE " + json.dumps(report))
        self.assertEqual(len(files), 1, "the platform saw more than one ingest session: " + json.dumps(report))
        self.assertEqual(publisher_starts, 1, "the destination publisher reconnected: " + json.dumps(report))
        rec = t + "/sink/" + files[0]

        # timestamps: one continuous timeline, no backwards step, no gap beyond what the measured stalls explain
        vsteps, asteps = self.packet_steps(rec, "v"), self.packet_steps(rec, "a")
        report["worst_video_step_s"], report["worst_audio_step_s"] = max(vsteps), max(asteps)
        self.assertEqual([s for s in vsteps + asteps if s < 0], [], "timestamps went backwards")
        # ONE honest timeline: frames are not crammed together at (nearly) the same timestamp, and the recorded media time tracks wall time
        # (it is shorter than wall time only by the measured swap stalls)
        crammed = sum(1 for x in vsteps if x < 0.005)
        self.assertLess(crammed, max(5, len(vsteps) // 50), "video timestamps were crammed together (%d of %d steps ~0)" % (crammed, len(vsteps)))
        v0, v1 = self.packet_span(rec, "v")
        a0, a1 = self.packet_span(rec, "a")
        wall = t_stop - self.pub.starts[0]
        report["timeline_s"], report["wall_s"] = round(max(v1, a1) - min(v0, a0), 1), round(wall, 1)
        print("INPLACE timeline " + json.dumps({"timeline_s": report["timeline_s"], "wall_s": report["wall_s"]}))
        self.assertGreater(v1 - v0, wall - 12, "the recorded timeline is far shorter than wall time: timestamps were compressed")
        self.assertLess(v1 - v0, wall + 2)
        # outside the deliberate failure injection, no stall bigger than the swap stalls measured above; the breaker case is the single long one
        for steps in (vsteps, asteps):
            big = sorted(x for x in steps if x > 3.0)
            self.assertLessEqual(len(big), 3, "too many long gaps in the platform timeline: %s" % big)
            self.assertTrue(all(x < 8.0 for x in big), "a gap approached the publisher's 20 s read timeout: %s" % big)
        self.assertLess(recover_s, 15.0, "the breaker took too long to restore the plain stream")

        # pictures: the sequence of overlay states in the recording is exactly the sequence we published
        redp, grp = self.rgb(rec, (950, 120, 100, 50)), self.rgb(rec, (950, 340, 100, 50))        # red block / green block regions (1280x720 frame)
        txtp = self.luma_max(rec, (60, 590, 160, 70))                                                    # "ON AIR" part of the text
        tailp = self.luma_max(rec, (480, 590, 280, 70))                                                  # right part: only "ON AIR NOW 12345"
        n = min(len(redp), len(grp), len(txtp), len(tailp))
        states = []
        for i in range(n):
            r_on = redp[i][0] > 200 and redp[i][1] < 60
            g_on = grp[i][1] > 200 and grp[i][0] < 60
            tx = txtp[i] > 100
            tl = tailp[i] > 100
            states.append(("R" if r_on else "") + ("G" if g_on else "") + ("T" if tx else "") + ("L" if tl else ""))
        seq = [s for s, c in self.runs(states) if c >= 8]                                            # ignore single-frame transitions
        print("INPLACE states " + json.dumps(seq))
        order = []
        for s in seq:
            if not order or order[-1] != s:
                order.append(s)
        # plain -> red+short text -> red+long text (hot) -> green+long text -> long text only -> plain -> (failures keep plain) -> short text ("BACK")
        self.assertEqual(order[0], "", order)
        self.assertIn("RT", order)
        self.assertIn("RTL", order)
        self.assertIn("GTL", order)
        self.assertIn("TL", order)
        self.assertLess(order.index("RT"), order.index("RTL"))
        self.assertLess(order.index("RTL"), order.index("GTL"))
        self.assertLess(order.index("GTL"), order.index("TL"))
        self.assertEqual(order[-1], "T", "the re-activated overlay should be the last thing on screen: " + json.dumps(order))
        self.assertNotIn("RG", "".join(order), "red and green blocks were never meant to be visible together")

        # audio/video sync: the corner flash and the beep are one event in the source; the recording must keep them together through every swap
        ft, flash = self.frame_times(rec), self.luma(rec, (0, 0, 64, 64))
        flashes = [ft[i] for i in range(1, min(len(ft), len(flash))) if flash[i] > 150 and flash[i - 1] <= 150]
        beeps = self.beeps(rec)
        offsets = []
        for f in flashes:
            near = min(beeps, key=lambda b: abs(b - f)) if beeps else None
            if near is not None and abs(near - f) < 0.5:
                offsets.append(round(f - near, 3))
        report["av_offset_ms"] = {"samples": len(offsets), "min": min(offsets) * 1000, "max": max(offsets) * 1000, "median": sorted(offsets)[len(offsets) // 2] * 1000}
        print("INPLACE av " + json.dumps(report["av_offset_ms"]))
        self.assertGreater(len(offsets), 40)
        spread = max(offsets) - min(offsets)
        self.assertLess(spread, 0.15, "audio/video drifted apart across the switches (spread %.0f ms)" % (spread * 1000))
        # the composed path never re-encodes audio and the stream copy path is back after removal
        self.assertTrue(all(v is not None for v in (cpu_copy, cpu_comp)))
        self.assertLess(cpu_copy, cpu_comp)


if __name__ == "__main__":
    unittest.main()
