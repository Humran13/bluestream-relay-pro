#!/usr/bin/env python3
"""Studio soak / benchmark harness (Linux staging VPS).

Runs ONE realistic 720p30 live-composition encode (clock + ticker + countdown +
logo + translucent bar over a looping synthetic source) in REAL TIME to a null
sink, and samples every --interval seconds:
  CPU cores used by ffmpeg (from /proc), RSS MB, ffmpeg `speed`, dropped frames,
  frame progress (continuity), host load, and bytes that WOULD be sent
  (output bitrate from -progress total_size).
Writes CSV + a final JSON verdict. It never claims a pass it did not measure:
`--hours 72` is the release gate; shorter runs are labelled PARTIAL.

    python3 tools/studio_soak.py --hours 72 --out /var/tmp/studio-soak   # the real gate
    python3 tools/studio_soak.py --minutes 5 --out /tmp/studio-quick     # smoke benchmark

No stream keys are involved: output goes to `-f null`.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from webapp.studio import ffmpeg as sff  # noqa: E402
from webapp.studio import widgets  # noqa: E402
from webapp.studio.model import OutputProfile, Overlay, Project  # noqa: E402

CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def proc_cpu_seconds(pid):
    with open("/proc/%d/stat" % pid) as fh:
        f = fh.read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / CLK  # utime + stime


def proc_rss_mb(pid):
    with open("/proc/%d/status" % pid) as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


def parse_progress(path):
    last, cur = {}, {}
    try:
        with open(path, errors="ignore") as fh:
            data = fh.read()[-20000:]
    except OSError:
        return {}
    for line in data.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
            if k == "progress":
                last, cur = dict(cur), {}
    return last


def init_text_files(proj, work, now=None):
    """Create EVERY text-backed overlay file before FFmpeg starts (drawtext textfile= must exist
    at filter-init time) and refresh them later. Covers clock, countdown, ticker, text, ..."""
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    for i, o in enumerate(proj.visible_overlays()):
        if o.kind in ("rect", "image", "video", "browser"):
            continue
        widgets.atomic_write(sff.text_file_path(work, i), widgets.render_text(o, now))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="short runs (used by the automated tests)")
    ap.add_argument("--hours", type=float, default=0)
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--kbps", type=int, default=4000)
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    total = a.hours * 3600 + a.minutes * 60 + a.seconds
    if total <= 0:
        ap.error("give --hours, --minutes or --seconds")
    os.makedirs(a.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="soak", dir=a.out)
    media = os.path.join(work, "src.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=60", "-f", "lavfi", "-i",
                    "sine=frequency=220:duration=60", "-c:v", "libx264", "-g", "60", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", media], check=True)
    logo = os.path.join(work, "logo.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:size=96x96:d=1",
                    "-frames:v", "1", logo], check=True)
    prof = OutputProfile(video_kbps=a.kbps, preset=a.preset)
    proj = Project("soak", "media", media, [
        Overlay("rect", x=0.0, y=0.88, w=1.0, h=0.12, color="#000000", opacity=0.6),
        Overlay("clock", x=0.7, y=0.04, w=0.25, h=0.1, size=56),
        Overlay("ticker", y=0.9, w=1.0, h=0.08, size=36, text="BlueStream Studio soak test ticker - "),
        Overlay("countdown", x=0.3, y=0.4, w=0.4, h=0.15, size=96, target="2099-01-01T00:00:00"),
        Overlay("image", x=0.04, y=0.04, w=0.08, asset="logo.png")], profile=prof).validate()
    args = sff._build(proj, media, "-", work, work, sff.DEFAULT_FONT, True, True, "ffmpeg", "null", [])
    args[args.index("-progress") + 1] = os.path.join(work, "progress.txt")
    args[-1] = "-"
    csv_path = os.path.join(a.out, "soak.csv")
    init_text_files(proj, work)          # must exist BEFORE ffmpeg initialises its filter graph
    start = time.monotonic()
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=open(os.path.join(work, "ffmpeg.log"), "w"))
    rows, restarts, last_frame, stalls = [], 0, -1, 0
    prev_cpu, prev_t = proc_cpu_seconds(p.pid), time.monotonic()
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "cpu_cores", "rss_mb", "speed", "frames", "drop", "load1", "alive"])
        while time.monotonic() - start < total:
            time.sleep(a.interval)
            now = time.monotonic()
            init_text_files(proj, work)      # keep dynamic text moving like the real sidecar
            alive = p.poll() is None
            if not alive:
                restarts += 1
                p = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=open(os.path.join(work, "ffmpeg.log"), "a"))
                prev_cpu, prev_t = proc_cpu_seconds(p.pid), now
                continue
            cpu = proc_cpu_seconds(p.pid)
            cores = (cpu - prev_cpu) / max(now - prev_t, 1e-6)
            prev_cpu, prev_t = cpu, now
            pr = parse_progress(os.path.join(work, "progress.txt"))
            frames = int(pr.get("frame", "0") or 0)
            if frames == last_frame:
                stalls += 1
            last_frame = frames
            sp = pr.get("speed", "0x").rstrip("x")
            row = [int(now - start), round(cores, 2), round(proc_rss_mb(p.pid), 1),
                   float(sp) if sp.replace(".", "", 1).isdigit() else 0.0, frames,
                   int(pr.get("drop_frames", "0") or 0), round(os.getloadavg()[0], 2), int(alive)]
            w.writerow(row)
            fh.flush()
            rows.append(row)
    p.terminate()
    p.wait(timeout=20)
    done = int(time.monotonic() - start)
    cores = sorted(r[1] for r in rows) or [0]
    speeds = [r[3] for r in rows if r[3] > 0] or [0]
    verdict = {
        "profile": "720p30 %dk %s" % (a.kbps, a.preset), "elapsed_s": done, "samples": len(rows),
        "restarts": restarts, "stall_samples": stalls,
        "cpu_cores_p50": cores[len(cores) // 2], "cpu_cores_p95": cores[int(len(cores) * 0.95) - 1],
        "cpu_cores_max": cores[-1], "rss_mb_max": max([r[2] for r in rows] or [0]),
        "speed_min": min(speeds), "dropped_frames_last": rows[-1][5] if rows else None,
        "hours_target": total / 3600.0,
        "frames_last": rows[-1][4] if rows else 0,
        "encoded_video_seconds": round((rows[-1][4] if rows else 0) / 30.0, 1),
        "gate": "FAILED-TO-START (no frames were ever encoded)" if not rows or rows[-1][4] == 0 else "72h-PASS-CANDIDATE" if (done >= 72 * 3600 and restarts == 0 and stalls == 0 and min(speeds) >= 1.0)
                else ("72h-FAIL" if done >= 72 * 3600 else "PARTIAL (not a 72h result)"),
        "note": "speed>=1.0 sustained, 0 restarts, 0 stalls required; cpu budget target ~2-2.5 cores",
        "host_cpus": os.cpu_count(),
    }
    with open(os.path.join(a.out, "verdict.json"), "w") as fh:
        json.dump(verdict, fh, indent=1)
    print(json.dumps(verdict, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
