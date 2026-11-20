"""Studio runtime CLI, called by run-relay.sh for relays of TYPE=studio.

    python3 -m studio.cli argv    <relay> <project>   NUL-separated FFmpeg argv on stdout
    python3 -m studio.cli widgets <relay> <project>   text-file updater loop (sidecar)
    python3 -m studio.cli plan    <project>           human-readable plan (diagnostics)

Exit codes for `argv`: 0 ok, 2 invalid/missing project or source, 3 refused by
admission control (never silently degraded), 4 pre-render failed, 5 no valid licence.
"""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import ffmpeg as sff
from . import browsercap, entitlement, health, netguard, nowplaying, planner, runtime, store, widgets
from .model import NAME_RE, ValidationError


def _workdir(relay):
    d = os.path.join(runtime.run_dir(), "studio-%s.work" % relay)
    os.makedirs(d, mode=0o750, exist_ok=True)
    return d


def _out_url(relay):
    return "%s/%s/%s" % (runtime.BUS, runtime.APP_RELAY, relay)


def make_plan(project):
    src_in, live = runtime.resolve_source(project)
    info = runtime.probe_source(src_in, live)
    if project.source_kind == "slideshow":
        # We generated this clip with the exact profile and keyframe interval.
        import dataclasses
        info = dataclasses.replace(info, gop_seconds=float(project.profile.gop_seconds))
    return src_in, live, info, planner.plan_output(project, info)


def _render_cache(project, src_in, info, plan, relay):
    """Build the one-time pre-render cache (atomic: temp file then rename)."""
    final = runtime.cache_path(plan.cache_key)
    if runtime.cache_valid(final):
        os.utime(final)  # LRU touch
        return final
    os.makedirs(runtime.cache_dir(), exist_ok=True)
    need = int((info.duration or 0) * project.profile.video_kbps * 1000 / 8 * 1.3) + 50 * 1024 ** 2
    runtime.evict_cache(keep=())
    if runtime.disk_free_bytes(runtime.cache_dir()) < need:
        raise RuntimeError("not enough free disk for pre-render cache")
    tmp = final[:-4] + ".tmp"
    args = sff.build_prerender_args(project, src_in, tmp, _workdir(relay), runtime.media_dir(),
                                    has_audio=bool(info.acodec))
    # build_prerender_args pins -f mp4, so the .tmp name does not matter.
    _write_static_text(project, _workdir(relay))
    r = subprocess.run(args, capture_output=True, text=True, timeout=max(600, int((info.duration or 0) * 4)))
    if r.returncode != 0 or not runtime.cache_valid(tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise RuntimeError("pre-render failed (ffmpeg exit %d)" % r.returncode)
    os.replace(tmp, final)
    return final


def _now_playing(project):
    """Current playlist item title ("" when it cannot be known honestly)."""
    if project.source_kind != "playlist":
        return ""
    return nowplaying.current_title(runtime.run_dir(), project.source_ref)


def _write_static_text(project, workdir, now=None, data=None):
    now = now or datetime.now(timezone.utc)
    data = data or {}
    playing = _now_playing(project)
    for i, o in enumerate(project.visible_overlays()):
        if o.kind in ("rect", "image"):
            continue
        widgets.atomic_write(sff.text_file_path(workdir, i),
                             widgets.render_text(o, now, playing, data.get(i, "")))


def _refresh_data(project, data, last):
    """Fetch each data overlay on its own cadence; keep the last good value on failure."""
    t = time.monotonic()
    for i, o in enumerate(project.visible_overlays()):
        if o.kind != "data" or t - last.get(i, -1e9) < o.refresh_s:
            continue
        last[i] = t
        try:
            data[i] = netguard.fetch_value(o.url, o.json_path)[:100]
        except (netguard.BlockedError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            sys.stderr.write("studio: data source %d failed (%s); keeping last value\n" % (i, type(exc).__name__))


def _attach_progress(args, relay, wait=5.0):
    """Point FFmpeg's -progress at the sidecar's loopback listener (if it is up).
    Falls back to the journal (pipe:2) so a missing sidecar never blocks streaming."""
    path = health.port_path(runtime.run_dir(), relay)
    deadline = time.monotonic() + wait
    port = None
    while time.monotonic() < deadline:
        try:
            with open(path) as fh:
                port = int(fh.read().strip())
            break
        except (OSError, ValueError):
            time.sleep(0.2)
    if port and "-progress" in args and 1024 <= port <= 65535:
        args = list(args)
        args[args.index("-progress") + 1] = "tcp://127.0.0.1:%d" % port
    return args


def cmd_argv(relay, name):
    # Backend enforcement: a Studio stream cannot start without a valid
    # entitlement, whatever the UI shows. (Free Relay/Playlist never reach here.)
    verdict = entitlement.current()
    if not verdict.ok:
        sys.stderr.write("studio: premium licence required: %s\n" % verdict.reason)
        return 5
    try:
        project = store.load_published(name)
        if any(o.kind == "browser" for o in project.overlays):
            ok_b, why_b = browsercap.usable()
            if not ok_b:
                sys.stderr.write("studio: browser source unavailable: %s\n" % why_b)
                return 2
        src_in, live, info, plan = make_plan(project)
    except (ValidationError, OSError, ValueError) as exc:
        sys.stderr.write("studio: cannot prepare project: %s\n" % exc)
        return 2
    ok, why = planner.admit(plan, runtime.running_live_encodes(exclude=relay),
                            max_live_encodes=int(os.environ.get("BLUESTREAM_STUDIO_MAX_LIVE", "1")),
                            cpu_cores=os.cpu_count() or 4, load_avg=os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0)
    if not ok:
        sys.stderr.write("studio: refused: %s\n" % why)
        return 3
    wd = _workdir(relay)
    out = _out_url(relay)
    if plan.path == planner.DIRECT:
        args = sff.build_copy_args(src_in, out, loop_file=not live)
    elif plan.path == planner.PRERENDER:
        try:
            cached = _render_cache(project, src_in, info, plan, relay)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            sys.stderr.write("studio: %s\n" % exc)
            return 4
        args = sff.build_copy_args(cached, out, loop_file=True)
    else:
        _write_static_text(project, wd)
        args = sff.build_live_args(project, src_in, out, wd, runtime.media_dir(),
                                   has_audio=True if live else bool(info.acodec),
                                   loop_file=not live)
    args = _attach_progress(args, relay)
    sys.stderr.write("studio: plan=%s (%s)\n" % (plan.path, "; ".join(plan.reasons)))
    sys.stdout.buffer.write(b"\0".join(a.encode() for a in args) + b"\0")
    return 0


def _supervisor_status(relay):
    try:
        with open(os.path.join(runtime.run_dir(), "relay-%s.status" % relay), encoding="utf-8") as fh:
            kv = dict(line.rstrip("\n").split("=", 1) for line in fh if "=" in line)
    except OSError:
        return {}
    return {"state": kv.get("STATE", ""), "detail": kv.get("DETAIL", ""), "attempt": kv.get("ATTEMPT", "")}


def cmd_widgets(relay, name):
    """Sidecar: refresh live text ~1/s, hold the live-slot lease, and publish real
    health (progress listener + process stats + supervisor state)."""
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    project = store.load_published(name)
    wd = _workdir(relay)
    run = runtime.run_dir()
    dynamic = any(o.dynamic for o in project.visible_overlays())
    if dynamic:
        runtime.take_live_lease(relay)
    tracker = health.Tracker()
    server = health.ProgressServer(tracker)
    server.start()
    with open(health.port_path(run, relay), "w") as fh:
        fh.write(str(server.port))
    sampler, out_url = health.ProcSampler(), _out_url(relay)
    try:
        data, last = {}, {}
        tick = 0
        while not stop:
            _refresh_data(project, data, last)
            _write_static_text(project, wd, data=data)
            if tick % 2 == 0:
                pid = health.find_ffmpeg_pid(out_url)
                health.write_health(run, relay, tracker.snapshot(sampler.sample(pid), _supervisor_status(relay)))
            tick += 1
            # monotonic sleep aligned to wall-clock seconds so timers don't drift
            time.sleep(1.0 - (time.time() % 1.0))
    finally:
        server.stop()
        runtime.release_live_lease(relay)
        for p in (health.port_path(run, relay), health.health_path(run, relay)):
            try:
                os.unlink(p)
            except OSError:
                pass
    return 0


HEALTH_KEYS = ("state", "detail", "speed", "fps", "frames", "dropped_frames", "restarts",
               "cpu_cores", "rss_mb", "last_failure", "supervisor_attempt", "output_seconds")


def health_pairs(relay):
    """key/value pairs for web-ctl (every value bounded and stripped of control chars)."""
    doc = health.read_health(runtime.run_dir(), relay)
    pairs = []
    for k in HEALTH_KEYS:
        v = doc.get(k)
        v = "" if v is None else str(v)
        pairs.append((k, "".join(ch for ch in v if ch >= " ")[:200]))
    return pairs


def cmd_health(relay):
    """NUL-separated key/value pairs on stdout, as `wc_json object` expects."""
    out = b""
    for k, v in health_pairs(relay):
        out += k.encode() + bytes([0]) + v.encode() + bytes([0])
    sys.stdout.buffer.write(out)
    return 0


def cmd_browser(relay, name):
    """Isolated worker (bluestream-browser@<relay>.service): keeps the capture PNGs fresh."""
    ok, why = browsercap.usable()
    if not ok:
        sys.stderr.write("studio-browser: refusing to run: %s\n" % why)
        return 3
    project = store.load_published(name)
    overlays = [(i, o) for i, o in enumerate(project.visible_overlays()) if o.kind == "browser"]
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    browsercap.worker(relay, overlays, runtime.run_dir(), lambda: bool(stop))
    return 0


def cmd_browser_unit(relay):
    """Entry point of bluestream-browser@<relay>.service: the root phase of run-relay.sh
    recorded which published project this relay runs."""
    try:
        with open(os.path.join(runtime.run_dir(), "studio-%s.project" % relay), encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        sys.stderr.write("studio-browser: no project recorded for relay\n")
        return 2
    if not NAME_RE.match(name):
        return 2
    return cmd_browser(relay, name)


def cmd_plan(name):
    project = store.load_published(name)
    src_in, live, info, plan = make_plan(project)
    print("path=%s live_source=%s" % (plan.path, live))
    for r in plan.reasons:
        print("reason: " + r)
    return 0


def main(argv):
    try:
        cmd = argv[1]
        if cmd in ("argv", "widgets"):
            relay, name = argv[2], argv[3]
            if not (NAME_RE.match(relay) and NAME_RE.match(name)):
                return 2
            return cmd_argv(relay, name) if cmd == "argv" else cmd_widgets(relay, name)
        if cmd == "plan" and NAME_RE.match(argv[2]):
            return cmd_plan(argv[2])
        if cmd == "browser-unit" and NAME_RE.match(argv[2]):
            return cmd_browser_unit(argv[2])
        if cmd == "browser" and NAME_RE.match(argv[2]) and NAME_RE.match(argv[3]):
            return cmd_browser(argv[2], argv[3])
        if cmd == "health" and NAME_RE.match(argv[2]):
            return cmd_health(argv[2])
    except IndexError:
        pass
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
