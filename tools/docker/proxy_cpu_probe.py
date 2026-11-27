#!/usr/bin/env python3
"""Container probe: CPU / bytes of the editor's video preview process when the browser reads as fast as the network allows.
usage: proxy_cpu_probe.py <seconds-to-read> [pace]     (pace = ffmpeg -readrate value, omit for unpaced)"""
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from webapp.studio import preview  # noqa: E402

secs = float(sys.argv[1])
pace = sys.argv[2] if len(sys.argv) > 2 else ""
d = os.environ.get("PROBE_DIR") or tempfile.mkdtemp(prefix="pcpu-")
os.environ["BLUESTREAM_MEDIA_DIR"] = d
if not os.path.exists(d + "/long264.mp4"):
  subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=120", "-f", "lavfi", "-i", "sine=frequency=440:duration=120",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "50", "-c:a", "aac", d + "/long264.mp4"], check=True)
if not os.path.exists(d + "/long.webm"):
  subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", d + "/long264.mp4", "-t", "40", "-c:v", "libvpx", "-b:v", "1M", "-deadline", "realtime", "-cpu-used", "8", d + "/long.webm"], check=True)
for name in ("long264.mp4", "long.webm"):
    path = d + "/" + name
    mode = preview.proxy_mode(preview.probe_codecs(path))
    args = preview.proxy_args(path, mode)
    if pace:
        i = args.index("-i")
        args = args[:i] + ["-readrate", pace, "-readrate_initial_burst", "10"] + args[i:]
    t0 = time.time()
    p = subprocess.Popen(args, stdout=subprocess.PIPE)
    got = 0
    c0 = None
    while time.time() - t0 < secs:
        chunk = p.stdout.read(262144)
        if not chunk:
            break
        got += len(chunk)
    ticks = sum(int(x) for x in open("/proc/%d/stat" % p.pid).read().rsplit(")", 1)[1].split()[11:13])
    wall = time.time() - t0
    p.kill()
    p.wait()
    print("%s mode=%s pace=%s read %.1f MB in %.1fs -> ffmpeg CPU %.0f%% of one core, media sent ~%.0fx realtime" % (
        name, mode, pace or "none", got / 1e6, wall, 100.0 * ticks / os.sysconf("SC_CLK_TCK") / wall, 0 if not got else (got / (1.0e6 / 8)) / 8))
