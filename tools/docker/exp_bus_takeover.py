#!/usr/bin/env python3
"""EXPERIMENT (run in the Linux test container, never on a VPS): what does a copy-mode destination publisher do when the
producer feeding the private RTMP bus stream is REPLACED (old producer stops, a new one starts under the same stream name)?

Real nginx-rtmp (default settings, as BlueStream ships them), real FFmpeg, a second RTMP app standing in for the platform (YouTube).
Measures: did the publisher FFmpeg survive, how many ingest sessions did the 'platform' see, and the wall-clock gap in the
platform-side recording. Usage: exp_bus_takeover.py [--splice]   (--splice = new producer starts BEFORE the old one stops)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

FF = shutil.which("ffmpeg")
NG = shutil.which("nginx")
PORT = 19350
d = tempfile.mkdtemp(prefix="bus-exp-")
os.makedirs(d + "/sink")
os.chmod(d + "/sink", 0o777)
os.chmod(d, 0o755)
conf = """
worker_processes 1; daemon off; pid %(d)s/n.pid; error_log %(d)s/err.log;
load_module /usr/lib/nginx/modules/ngx_rtmp_module.so;
events { worker_connections 64; }
rtmp { server { listen 127.0.0.1:%(port)d; chunk_size 4096;
  application live { live on; record off; }
  application sink { live on; record all; record_path %(d)s/sink; record_unique on; record_suffix .flv; }
} }
""" % {"d": d, "port": PORT}
open(d + "/n.conf", "w").write(conf)
ng = subprocess.Popen([NG, "-e", d + "/err.log", "-c", d + "/n.conf", "-p", d], stderr=open(d + "/ng.out", "w"))
time.sleep(1.0)
BUS, SINK = "rtmp://127.0.0.1:%d/live/dino" % PORT, "rtmp://127.0.0.1:%d/sink/key" % PORT


def producer(pattern, tag):
    """Re-encoding producer with fixed H.264/AAC parameters (what a composited relay would send)."""
    return subprocess.Popen([FF, "-v", "error", "-re", "-f", "lavfi", "-i", "%s=size=640x360:rate=30" % pattern, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                             "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
                             "-b:v", "1000k", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-f", "flv", BUS], stderr=subprocess.DEVNULL)


class Publisher:
    """Emulates the unit: ffmpeg pull bus -> push platform, restarted by 'systemd' (Restart=on-failure, RestartSec=3)."""

    def __init__(self):
        self.starts, self.exits, self.stop = [], [], False

    def run(self):
        import threading

        def loop():
            while not self.stop:
                p = subprocess.Popen([FF, "-v", "error", "-fflags", "+genpts", "-rw_timeout", "20000000", "-i", BUS, "-c:v", "copy", "-c:a", "copy", "-f", "flv", SINK],
                                     stderr=open(d + "/pub.err", "a"))
                self.starts.append(time.time())
                self.p = p
                p.wait()
                self.exits.append(time.time())
                if not self.stop:
                    time.sleep(3)
        self.t = threading.Thread(target=loop, daemon=True)
        self.t.start()


def main():
    splice = "--splice" in sys.argv
    n_swaps = int(sys.argv[sys.argv.index("--swaps") + 1]) if "--swaps" in sys.argv else 1
    a = producer("testsrc2", "A")
    time.sleep(2.5)
    pub = Publisher()
    pub.run()
    time.sleep(6)
    t_switch = time.time()
    cur = a
    for k in range(n_swaps):
        cur.terminate()
        cur.wait()
        cur = producer(["smptebars", "testsrc", "rgbtestsrc", "testsrc2"][k % 4], "B%d" % k)
        time.sleep(8)
    b = cur
    time.sleep(4)
    pub.stop = True
    b.terminate()
    time.sleep(1.5)
    pub.p.terminate()
    time.sleep(1)
    files = sorted(os.listdir(d + "/sink"))
    sessions = []
    for f in files:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,start_time", "-of", "json", d + "/sink/" + f], capture_output=True, text=True)
        try:
            fm = json.loads(r.stdout)["format"]
            sessions.append({"file": f, "start_time": float(fm.get("start_time", 0)), "duration": round(float(fm.get("duration", 0)), 2)})
        except (ValueError, KeyError):
            sessions.append({"file": f, "error": True})
    cont = []
    for f in files:
        for kind in ("v", "a"):
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "packet=dts_time", "-of", "csv=p=0", d + "/sink/" + f], capture_output=True, text=True)
            ts = [float(x.strip(",")) for x in r.stdout.split() if x.strip(",") not in ("N/A", "")]
            steps = [round(b - a, 2) for a, b in zip(ts, ts[1:])]
            cont.append({"file": f, "stream": kind, "packets": len(ts), "first": ts[0] if ts else None, "last": ts[-1] if ts else None,
                         "backward_steps": [x for x in steps if x < 0][:5], "gaps_over_0.5s": [x for x in steps if x > 0.5][:5], "max_step": max(steps) if steps else None})
    epochs = [int(f.split("-")[-1].split(".")[0]) for f in files]
    gaps = [round(epochs[i + 1] - (epochs[i] + sessions[i].get("duration", 0)), 1) for i in range(len(files) - 1)]
    res = {"mode": "splice" if splice else "stop-then-start", "publisher_starts": len(pub.starts), "publisher_exits": len(pub.exits),
           "publisher_exit_after_switch_s": [round(e - t_switch, 2) for e in pub.exits], "platform_sessions": len(files), "sessions": sessions, "gap_between_sessions_s": gaps, "continuity": cont, "switch_at_session_offset_s": round(t_switch - epochs[0], 1) if epochs else None}
    print(json.dumps(res))
    if os.path.exists(d + "/pub.err"):
        print("publisher stderr tail:", " | ".join(open(d + "/pub.err").read()[-600:].splitlines()))
    if os.path.exists(d + "/ng.out"):
        print("nginx out:", open(d + "/ng.out").read()[-300:])
    if os.path.exists(d + "/err.log"):
        print("nginx err tail:", " | ".join(open(d + "/err.log").read()[-400:].splitlines()))
    ng.terminate()
    shutil.rmtree(d, ignore_errors=True)


main()
