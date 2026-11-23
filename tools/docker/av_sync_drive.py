#!/usr/bin/env python3
"""Container probe driver: encode the sync-marker clip (a) as plain stream copy and (b) through the in-place compositor (`overlay-argv`)
to files, then print the A/V offsets of each (see av_sync_probe.py)."""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
t = tempfile.mkdtemp(prefix="avsync-")
for d in ("media", "run", "studio"):
    os.makedirs(t + "/" + d)
os.symlink(REPO + "/webapp/studio", t + "/studio_pkg")
env = dict(os.environ, PYTHONPATH=REPO + "/webapp", BLUESTREAM_STUDIO_DIR=t + "/studio", BLUESTREAM_RUN_DIR=t + "/run", BLUESTREAM_MEDIA_DIR=t + "/media",
           BLUESTREAM_STUDIO_TEST_LOAD="0")
os.environ.update({k: v for k, v in env.items() if k.startswith("BLUESTREAM_")})
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=0x101020:s=1280x720:r=25,drawbox=x=0:y=0:w=64:h=64:color=white:t=fill:enable='lt(mod(t,1),0.12)'",
                "-f", "lavfi", "-i", "aevalsrc='0.8*sin(2*PI*1000*t)*lt(mod(t,1),0.12)':s=44100:d=20", "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-g", "50",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", t + "/media/src.mp4"], check=True)
src = t + "/media/src.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-re", "-stream_loop", "-1", "-i", src, "-t", "10", "-c:v", "copy", "-c:a", "copy", "-f", "flv", t + "/plain.flv"], check=True)
from webapp.studio import overlay as ovl, store  # noqa: E402
from webapp.studio.model import OutputProfile, Overlay, Project  # noqa: E402
p = Project("dino-overlay", "relay", "dino", [Overlay("rect", x=0.7, y=0.15, w=0.2, h=0.1, color="#ff0000"), Overlay("text", text="ON AIR", x=0.05, y=0.8, w=0.4, h=0.12, size=60)],
            OutputProfile(width=0, height=0, fps=0, preset=sys.argv[1] if len(sys.argv) > 1 else "veryfast", video_kbps=3000), target_relay="dino").validate()
store.save_draft(p)
store.publish("dino-overlay")
ovl.write_marker("dino", "dino-overlay", True)
r = subprocess.run([sys.executable, "-m", "studio.cli", "overlay-argv", "dino", "--", "-re", "-stream_loop", "-1", "-i", src], cwd=REPO + "/webapp", env=env, capture_output=True)
sys.stderr.write(r.stderr.decode(errors="replace"))
args = [a.decode() for a in r.stdout.split(b"\0") if a]
args = [a if not a.startswith("rtmp://") else t + "/comp.flv" for a in args]
i = len(args) - 1 - args[::-1].index("-f")
args = args[:i] + ["-t", "10"] + args[i:]
subprocess.run(["ffmpeg"] + args, env=env, check=True, capture_output=True)
for name in ("plain.flv", "comp.flv"):
    print(name, subprocess.run([sys.executable, REPO + "/tools/docker/av_sync_probe.py", t + "/" + name], capture_output=True, text=True).stdout.strip())
shutil.rmtree(t, ignore_errors=True)
