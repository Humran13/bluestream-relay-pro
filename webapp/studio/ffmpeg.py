"""FFmpeg argv construction for Studio. Returns argument LISTS only; callers must
exec them without a shell. Every interpolated value is validated first."""
import re

from .model import ValidationError

SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,200}$")
BUS_RE = re.compile(r"^rtmp://127\.0\.0\.1(:\d{2,5})?/[A-Za-z0-9_-]{1,64}/[a-z0-9_-]{1,48}$")
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _px(frac, total):
    return int(round(frac * total / 2.0)) * 2  # even pixel values for yuv420p


def _color(hexcolor, opacity):
    return "0x%s@%.2f" % (hexcolor[1:], opacity)


def text_file_path(workdir, index):
    return "%s/ov%d.txt" % (workdir, index)


def _check_path(path):
    if not SAFE_PATH_RE.match(path) or ".." in path.split("/"):
        raise ValidationError("unsafe path")


def _check_bus(url):
    if not BUS_RE.match(url):
        raise ValidationError("Studio only reads/writes the private local bus")


def _encode_opts(p):
    return ["-c:v", "libx264", "-preset", p.preset, "-profile:v", "high",
            "-b:v", "%dk" % p.video_kbps, "-maxrate", "%dk" % p.video_kbps,
            "-bufsize", "%dk" % (p.video_kbps * 2),
            "-g", str(p.gop), "-keyint_min", str(p.gop), "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", "-r", str(p.fps),
            "-c:a", "aac", "-b:a", "%dk" % p.audio_kbps, "-ar", "44100", "-ac", "2"]


def _source_input(project, source, loop_file):
    """-i for a bus URL (live) or a media file (looped when requested)."""
    if source.startswith("rtmp://"):
        _check_bus(source)
        return ["-re", "-i", source]
    _check_path(source)
    pre = ["-re", "-stream_loop", "-1"] if loop_file else ["-re"]
    return pre + ["-i", source]


def _compose(project, workdir, assets_dir, fontfile, has_audio, src_inputs, first_extra):
    """Shared filter graph. Returns (extra_input_args, filter_graph, last_label,
    extra_input_count)."""
    p = project.profile
    extra_args = []
    extra = 0
    chain = ["[0:v]scale=%d:%d:force_original_aspect_ratio=decrease,"
             "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,format=yuv420p[b0]"
             % (p.width, p.height, p.width, p.height, p.fps)]
    last = "b0"
    for i, o in enumerate(project.visible_overlays()):
        nxt = "b%d" % (i + 1)
        x, y = _px(o.x, p.width), _px(o.y, p.height)
        if o.kind == "rect":
            f = "[%s]drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill[%s]" % (
                last, x, y, _px(o.w, p.width), _px(o.h, p.height), _color(o.color, o.opacity), nxt)
        elif o.kind == "image":
            extra_args += ["-loop", "1", "-i", "%s/%s" % (assets_dir, o.asset)]
            extra += 1
            idx = first_extra + extra - 1
            chain.append("[%d:v]scale=%d:-2,format=rgba,colorchannelmixer=aa=%.2f[im%d]"
                         % (idx, _px(o.w, p.width), o.opacity, idx))
            f = "[%s][im%d]overlay=%d:%d:shortest=0[%s]" % (last, idx, x, y, nxt)
        else:  # text, clock, countdown, ticker: all read an atomically replaced file
            tf = text_file_path(workdir, i)
            xexpr = "w-mod(t*120,w+text_w)" if o.kind == "ticker" else str(x)
            f = ("[%s]drawtext=fontfile=%s:textfile=%s:reload=1:fontsize=%d:fontcolor=%s:x='%s':y=%d[%s]"
                 % (last, fontfile, tf, o.size, _color(o.color, o.opacity), xexpr, y, nxt))
        chain.append(f)
        last = nxt
    return extra_args, ";".join(chain), last, extra


def _audio_args(has_audio, silence_index):
    if has_audio:
        return ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0"], []
    return ["-map", "%d:a" % silence_index], ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]


def _build(project, source, output, workdir, assets_dir, fontfile, has_audio,
           loop_file, ffmpeg, fmt, extra_out):
    project.validate()
    for path in (workdir, assets_dir, fontfile):
        _check_path(path)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:2"]
    args += _source_input(project, source, loop_file)
    extra_args, graph, last, extra = _compose(project, workdir, assets_dir, fontfile,
                                              has_audio, 1, 1)
    args += extra_args
    amap, ain = _audio_args(has_audio, extra + 1)
    args += ain
    args += ["-filter_complex", graph, "-map", "[%s]" % last] + amap
    args += _encode_opts(project.profile) + extra_out + ["-f", fmt, output]
    return args


def build_live_args(project, input_source, output_url, workdir, assets_dir,
                    fontfile=DEFAULT_FONT, has_audio=True, loop_file=False, ffmpeg="ffmpeg"):
    """One decode/composite/encode chain for the whole Studio output (bus out)."""
    _check_bus(output_url)
    return _build(project, input_source, output_url, workdir, assets_dir, fontfile,
                  has_audio, loop_file, ffmpeg, "flv", [])


def build_prerender_args(project, input_file, output_file, workdir, assets_dir,
                         fontfile=DEFAULT_FONT, has_audio=True, ffmpeg="ffmpeg"):
    """One-time render of a finite source with STATIC overlays into a cache file
    that is then stream-copied forever. Not realtime (-re is dropped)."""
    _check_path(output_file)
    _check_path(input_file)
    a = _build(project, input_file, output_file, workdir, assets_dir, fontfile,
               has_audio, False, ffmpeg, "mp4", ["-movflags", "+faststart"])
    a.remove("-re")
    return a


def build_copy_args(source, output_url, loop_file=False, ffmpeg="ffmpeg"):
    """Cheap path: stream-copy a compatible source straight onto the Studio bus."""
    _check_bus(output_url)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:2", "-fflags", "+genpts"]
    args += _source_input(None, source, loop_file)
    return args + ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "copy",
                   "-f", "flv", output_url]
