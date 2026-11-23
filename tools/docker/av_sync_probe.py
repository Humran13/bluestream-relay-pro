#!/usr/bin/env python3
"""Container probe: A/V offset (video flash vs audio beep) of a clip, using ABSOLUTE timestamps of both streams.
usage: av_sync_probe.py <file>        prints JSON {first_video_pts, first_audio_pts, offsets_ms:[...]}"""
import json
import struct
import subprocess
import sys


def pts(path, stream, n=None):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "packet=pts_time", "-of", "csv=p=0", path], capture_output=True, text=True)
    out = [float(x.strip(",")) for x in r.stdout.split() if x.strip(",") not in ("", "N/A")]
    return out


def main(path):
    vt = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "frame=pts_time", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    vt = [float(x.strip(",")) for x in vt if x.strip(",") not in ("", "N/A")]
    lum = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "crop=64:64:0:0,scale=1:1,format=gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"], capture_output=True).stdout
    flashes = [vt[i] for i in range(1, min(len(vt), len(lum))) if lum[i] > 150 and lum[i - 1] <= 150]
    ap = pts(path, "a")
    a0 = ap[0] if ap else 0.0
    r = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi", "-i", "amovie=%s,astats=metadata=1:reset=1" % path, "-show_entries",
                        "frame=pts_time:frame_tags=lavfi.astats.Overall.Peak_level", "-of", "csv=p=0"], capture_output=True, text=True)
    beeps, prev = [], False
    for ln in r.stdout.splitlines():
        q = ln.split(",")
        try:
            t, peak = float(q[0]), float(q[1])
        except (ValueError, IndexError):
            continue
        loud = peak > -8.0
        if loud and not prev:
            beeps.append(t)
        prev = loud
    offs = []
    for f in flashes:
        if beeps:
            b = min(beeps, key=lambda x: abs(x - f))
            if abs(b - f) < 0.5:
                offs.append([round(f, 1), round((f - b) * 1000)])
    print("flash_times", [round(x, 1) for x in flashes][-12:], "beep_times", [round(x, 1) for x in beeps][-12:], "vmax", vt[-1] if vt else None, "amax", ap[-1] if ap else None, file=sys.stderr)
    print(json.dumps({"first_video_pts": vt[0] if vt else None, "first_audio_pts": a0, "flashes": len(flashes), "beeps": len(beeps), "offsets_ms": offs}))


main(sys.argv[1])
