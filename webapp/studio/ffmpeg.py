"""FFmpeg argv construction for Studio. Returns argument LISTS only; callers must
exec them without a shell. Every interpolated value is validated first."""
import re

from .model import ValidationError

SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,200}$")
URL_RE = re.compile(r"^(rtmps?://127\.0\.0\.1(:\d+)?/[A-Za-z0-9/_-]{1,100}|https?://127\.0\.0\.1(:\d+)?/[A-Za-z0-9/_.-]{1,150})$")


def _px(frac, total):
    return int(round(frac * total / 2.0)) * 2  # even pixel values for yuv420p


def _color(hexcolor, opacity):
    return "0x%s@%.2f" % (hexcolor[1:], opacity)


def text_file_path(workdir, index):
    return "%s/ov%d.txt" % (workdir, index)


def build_live_args(project, input_url, output_url, workdir, assets_dir,
                    fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    has_audio=True, ffmpeg="ffmpeg"):
    """One decode/composite/encode chain for the whole Studio output."""
    project.validate()
    p = project.profile
    for path in (workdir, assets_dir, fontfile):
        if not SAFE_PATH_RE.match(path) or ".." in path.split("/"):
            raise ValidationError("unsafe path")
    for url in (input_url, output_url):
        if not URL_RE.match(url):
            raise ValidationError("Studio only reads/writes the private local bus")

    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-progress", "pipe:2",
            "-re", "-i", input_url]
    extra_inputs = 0
    chain = ["[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
             "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,format=yuv420p[b0]"
             % (p.width, p.height, p.width, p.height, p.fps)]
    last = "b0"
    n = 0
    for i, o in enumerate(project.visible_overlays()):
        nxt = "b%d" % (n + 1)
        x, y = _px(o.x, p.width), _px(o.y, p.height)
        if o.kind == "rect":
            f = "[%s]drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill[%s]" % (
                last, x, y, _px(o.w, p.width), _px(o.h, p.height), _color(o.color, o.opacity), nxt)
        elif o.kind == "image":
            args += ["-loop", "1", "-i", "%s/%s" % (assets_dir, o.asset)]
            extra_inputs += 1
            idx = extra_inputs
            chain.append("[%d:v]scale=%d:-2,format=rgba,colorchannelmixer=aa=%.2f[im%d]"
                         % (idx, _px(o.w, p.width), o.opacity, idx))
            f = "[%s][im%d]overlay=%d:%d:shortest=0[%s]" % (last, idx, x, y, nxt)
        else:  # text, clock, countdown, ticker: all read an atomically replaced file
            tf = text_file_path(workdir, i)
            if o.kind == "ticker":
                xexpr = "w-mod(t*120,w+text_w)"
            else:
                xexpr = str(x)
            f = ("[%s]drawtext=fontfile=%s:textfile=%s:reload=1:fontsize=%d:fontcolor=%s:x='%s':y=%d[%s]"
                 % (last, fontfile, tf, o.size, _color(o.color, o.opacity), xexpr, y, nxt))
        chain.append(f)
        last = nxt
        n += 1
    graph = ";".join(chain)
    args += ["-filter_complex", graph, "-map", "[%s]" % last]
    if has_audio:
        args += ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "%d:a" % (extra_inputs + 1)]
    args += ["-c:v", "libx264", "-preset", p.preset, "-profile:v", "high",
             "-b:v", "%dk" % p.video_kbps, "-maxrate", "%dk" % p.video_kbps,
             "-bufsize", "%dk" % (p.video_kbps * 2),
             "-g", str(p.gop), "-keyint_min", str(p.gop), "-sc_threshold", "0",
             "-pix_fmt", "yuv420p", "-r", str(p.fps),
             "-c:a", "aac", "-b:a", "%dk" % p.audio_kbps, "-ar", "44100", "-ac", "2",
             "-f", "flv", output_url]
    return args
