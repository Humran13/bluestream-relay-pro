"""FFmpeg argv construction for Studio. Returns argument LISTS only; callers must
exec them without a shell. Every interpolated value is validated first."""
import re

from .model import ValidationError

SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,200}$")
BUS_RE = re.compile(r"^rtmp://127\.0\.0\.1(:\d{2,5})?/[A-Za-z0-9_-]{1,64}/[a-z0-9_-]{1,48}$")
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
# Fonts a layer may choose. Only files that really exist on the server are used; anything else falls back to the default.
FONT_FILES = {
    "DejaVu Sans": DEFAULT_FONT,
    "DejaVu Sans Bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVu Serif": "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "DejaVu Sans Mono": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
}


def available_fonts():
    import os
    return [name for name, path in FONT_FILES.items() if os.path.isfile(path)]


def font_for(name, default):
    import os
    path = FONT_FILES.get(name)
    return path if path and os.path.isfile(path) else default


def _px(frac, total):
    return int(round(frac * total / 2.0)) * 2  # even pixel values for yuv420p


def _color(hexcolor, opacity):
    return "0x%s@%.2f" % (hexcolor[1:], opacity)


def text_file_path(workdir, index):
    return "%s/ov%d.txt" % (workdir, index)


def relay_for_browser(workdir):
    """workdir is <run>/studio-<relay>.work (see cli._workdir)."""
    base = workdir.rstrip("/").rsplit("/", 1)[-1]
    return base[len("studio-"):-len(".work")] if base.startswith("studio-") and base.endswith(".work") else "x"


def run_dir_for_browser(workdir):
    return workdir.rstrip("/").rsplit("/", 1)[0]


def _check_path(path):
    if not SAFE_PATH_RE.match(path) or ".." in path.split("/"):
        raise ValidationError("unsafe path")


def _check_bus(url):
    if not BUS_RE.match(url):
        raise ValidationError("Studio only reads/writes the private local bus")


def _sf(p):
    """Scaling-filter flags. The default (bicubic) adds nothing, so existing graphs stay byte-identical."""
    return "" if p.scaler == "bicubic" else ":flags=%s" % p.scaler


def _video_encode_opts(p):
    if p.encoder == "h264_nvenc":
        # Constant-bitrate low-latency H.264 on the GPU; the filter graph itself still runs on the CPU.
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr", "-profile:v", "high",
                "-b:v", "%dk" % p.video_kbps, "-maxrate", "%dk" % p.video_kbps, "-bufsize", "%dk" % (p.video_kbps * 2),
                "-g", str(p.gop), "-bf", "0", "-pix_fmt", "yuv420p", "-r", str(p.fps)]
    return ["-c:v", "libx264", "-preset", p.preset, "-profile:v", "high",
            "-b:v", "%dk" % p.video_kbps, "-maxrate", "%dk" % p.video_kbps,
            "-bufsize", "%dk" % (p.video_kbps * 2),
            "-g", str(p.gop), "-keyint_min", str(p.gop), "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", "-r", str(p.fps)]


def _encode_opts(p):
    return _video_encode_opts(p) + ["-c:a", "aac", "-b:a", "%dk" % p.audio_kbps, "-ar", "44100", "-ac", "2"]


def _source_input(project, source, loop_file):
    """-i for a bus URL (live) or a media file (looped when requested)."""
    if source.startswith("rtmp://"):
        _check_bus(source)
        return ["-re", "-i", source]
    _check_path(source)
    pre = ["-re", "-stream_loop", "-1"] if loop_file else ["-re"]
    return pre + ["-i", source]


def _compose(project, workdir, assets_dir, fontfile, has_audio, src_inputs, first_extra, pre_chain=None):
    """Shared filter graph. Returns (extra_input_args, filter_graph, last_label,
    extra_input_count)."""
    p = project.profile
    extra_args = []
    extra = 0
    chain = ["[0:v]scale=%d:%d%s:force_original_aspect_ratio=decrease,"
             "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,fps=%d,format=yuv420p[b0]"
             % (p.width, p.height, _sf(p), p.width, p.height, p.fps)]
    last = "b0"
    chain += pre_chain or []
    if pre_chain:
        last = "bv"
    for i, o in enumerate(project.visible_overlays()):
        nxt = "b%d" % (i + 1)
        x, y = _px(o.x, p.width), _px(o.y, p.height)
        if o.kind == "rect":
            f = "[%s]drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill[%s]" % (
                last, x, y, _px(o.w, p.width), _px(o.h, p.height), _color(o.color, o.opacity), nxt)
        elif o.kind == "browser":
            png = "%s/studio-%s-br%d.png" % (run_dir_for_browser(workdir), relay_for_browser(workdir), i)
            _check_path(png)
            # Measured with real FFmpeg (test_png_reload_input_variants_report): image2 re-reads the
            # file on every loop ONLY when it is consumed in real time. Without `-re` it reads ahead and
            # keeps showing a stale frame, and `-stream_loop -1` does not help. So: -re + -loop 1.
            extra_args += ["-re", "-f", "image2", "-framerate", "2", "-loop", "1", "-i", png]
            extra += 1
            idx = first_extra + extra - 1
            chain.append("[%d:v]scale=%d:-2,format=rgba,colorchannelmixer=aa=%.2f[im%d]"
                         % (idx, _px(o.w, p.width), o.opacity, idx))
            f = "[%s][im%d]overlay=%d:%d:shortest=1[%s]" % (last, idx, x, y, nxt)
        elif o.kind in ("image", "video"):
            if o.kind == "video":  # picture-in-picture: looped, muted (audio never mapped)
                extra_args += ["-stream_loop", "-1", "-i", "%s/%s" % (assets_dir, o.asset)]
            else:
                extra_args += ["-loop", "1", "-i", "%s/%s" % (assets_dir, o.asset)]
            extra += 1
            idx = first_extra + extra - 1
            crop = ""
            if o.cropped:
                crop = "crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f," % (o.crop_w, o.crop_h, o.crop_x, o.crop_y)
            chain.append("[%d:v]%sscale=%d:-2%s,format=rgba,colorchannelmixer=aa=%.2f[im%d]"
                         % (idx, crop, _px(o.w, p.width), _sf(p), o.opacity, idx))
            f = "[%s][im%d]overlay=%d:%d:shortest=1[%s]" % (last, idx, x, y, nxt)
        else:  # text, clock, countdown, ticker: all read an atomically replaced file
            tf = text_file_path(workdir, i)
            ff = font_for(o.font, fontfile)
            _check_path(ff)
            bw = _px(o.w, p.width)
            xexpr = {"center": "%d+(%d-text_w)/2" % (x, bw), "right": "%d+%d-text_w" % (x, bw)}.get(o.align, str(x))
            if o.kind == "ticker":
                xexpr = "w-mod(t*120,w+text_w)"
            box = (":box=1:boxcolor=%s:boxborderw=10" % _color(o.bg_color, o.bg_opacity)) if o.bg_opacity > 0 else ""
            f = ("[%s]drawtext=fontfile=%s:textfile=%s:reload=1:fontsize=%d:fontcolor=%s:x='%s':y=%d%s[%s]"
                 % (last, ff, tf, o.size, _color(o.color, o.opacity), xexpr, y, box, nxt))
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
    p = project.profile
    pre_chain, audio_label = None, None
    if project.source_kind == "audio":
        # Input 0 = synthetic background (colour or still image), input 1 = the audio file.
        if project.background.startswith("#"):
            args += ["-f", "lavfi", "-i", "color=c=0x%s:s=%dx%d:r=%d" % (project.background[1:], p.width, p.height, p.fps)]
        else:
            bg = "%s/%s" % (assets_dir, project.background)
            _check_path(bg)
            args += ["-loop", "1", "-framerate", str(p.fps), "-i", bg]
        args += _source_input(project, source, loop_file)
        n_src, has_audio = 2, True
        if project.visualizer:
            wave_h = _px(0.25, p.height)
            pre_chain = [
                "[1:a]asplit[aviz][aout0]",
                "[aviz]showwaves=s=%dx%d:mode=cline:rate=%d:colors=0x4da3ff,format=rgba[wave]" % (p.width, wave_h, p.fps),
                "[b0][wave]overlay=0:%d:shortest=1[bv]" % (p.height - wave_h),
                "[aout0]aresample=async=1:first_pts=0[aout]",
            ]
            audio_label = "[aout]"
    else:
        args += _source_input(project, source, loop_file)
        n_src = 1
    extra_args, graph, last, extra = _compose(project, workdir, assets_dir, fontfile,
                                              has_audio, n_src, n_src, pre_chain)
    args += extra_args
    if project.source_kind == "audio":
        amap, ain = (["-map", audio_label] if audio_label else ["-map", "1:a:0", "-af", "aresample=async=1:first_pts=0"]), []
    else:
        amap, ain = _audio_args(has_audio, extra + n_src)
    args += ain
    args += ["-filter_complex", graph, "-map", "[%s]" % last] + amap
    if not has_audio or project.source_kind == "audio":
        args += ["-shortest"]  # synthetic silence / background are endless; end with the real stream
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


def build_slideshow_args(image_paths, seconds, profile, output_file, ffmpeg="ffmpeg"):
    """Render still images (each shown `seconds`) into one profile-exact clip with
    silent stereo audio. Finite and static, so the result is cached and then
    stream-copied/looped. Paths must be absolute and safe."""
    if not 1 <= len(image_paths) <= 50:
        raise ValidationError("1-50 images required")
    p = profile
    _check_path(output_file)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning"]
    chain = []
    for i, path in enumerate(image_paths):
        _check_path(path)
        args += ["-loop", "1", "-framerate", str(p.fps), "-t", str(int(seconds)), "-i", path]
        chain.append("[%d:v]scale=%d:%d%s:force_original_aspect_ratio=decrease,"
                     "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%d,format=yuv420p[s%d]"
                     % (i, p.width, p.height, _sf(p), p.width, p.height, p.fps, i))
    n = len(image_paths)
    chain.append("%sconcat=n=%d:v=1:a=0[v]" % ("".join("[s%d]" % i for i in range(n)), n))
    args += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-filter_complex", ";".join(chain),
             "-map", "[v]", "-map", "%d:a" % n, "-t", str(int(seconds) * n)]
    return args + _encode_opts(p) + ["-movflags", "+faststart", "-f", "mp4", output_file]


def build_copy_args(source, output_url, loop_file=False, ffmpeg="ffmpeg"):
    """Cheap path: stream-copy a compatible source straight onto the Studio bus."""
    _check_bus(output_url)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:2", "-fflags", "+genpts"]
    args += _source_input(None, source, loop_file)
    return args + ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "copy",
                   "-f", "flv", output_url]


def build_overlay_args(project, input_tokens, output_url, workdir, assets_dir, profile, has_audio,
                       fontfile=DEFAULT_FONT, ffmpeg="ffmpeg", ts_offset=0.0):
    """Compose overlays onto the relay's OWN source: `input_tokens` are exactly the input options + `-i <source>` the plain relay would
    use (same loop / reconnect / resolver handling), so the picture is identical apart from the overlays. One decode, one encode.
    The source's audio is stream-copied (never re-encoded); a source without audio gets silent AAC so platforms accept the stream.
    `profile` is the EFFECTIVE profile (match-source already resolved to real numbers)."""
    import dataclasses
    _check_bus(output_url)
    toks = list(input_tokens)
    if not toks or len(toks) > 40 or any((not isinstance(t, str)) or "\x00" in t for t in toks) or "-i" not in toks[:-1]:
        raise ValidationError("invalid source arguments")
    if not (160 <= profile.width <= 3840 and 90 <= profile.height <= 2160 and profile.width % 2 == 0 and profile.height % 2 == 0
            and 10 <= profile.fps <= 60 and 500 <= profile.video_kbps <= 20000):
        raise ValidationError("invalid output size")
    for path in (workdir, assets_dir, fontfile):
        _check_path(path)
    for o in project.overlays:
        o.validate()
    p2 = dataclasses.replace(project, profile=profile)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-nostats", "-progress", "pipe:2"] + toks
    extra_args, graph, last, extra = _compose(p2, workdir, assets_dir, fontfile, has_audio, 1, 1, None)
    args += extra_args
    if has_audio:
        amap, ain = ["-map", "0:a:0?", "-c:a", "copy"], []
    else:
        amap = ["-map", "%d:a" % (1 + extra), "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-shortest"]
        ain = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    shift = ["-output_ts_offset", "%.3f" % ts_offset] if ts_offset > 0 else []
    args += ain + ["-filter_complex", graph, "-map", "[%s]" % last] + amap + _video_encode_opts(profile) + shift + ["-f", "flv", output_url]
    return args
