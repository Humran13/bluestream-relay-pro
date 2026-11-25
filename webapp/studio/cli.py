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
from . import browsercap, capabilities, entitlement, health, preview, netguard, nowplaying, planner, runtime, store, widgets
from . import model
from . import overlay as ovl
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
    tmp = "%s.%d.tmp" % (final[:-4], os.getpid())  # unique: two starts never share a temp file
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


def _load_avg():
    """1-minute load for admission control. BLUESTREAM_STUDIO_TEST_LOAD is a TEST-ONLY hook (a busy CI runner must not make
    the frame tests flaky); production never sets it."""
    forced = os.environ.get("BLUESTREAM_STUDIO_TEST_LOAD")
    if forced is not None:
        try:
            return float(forced)
        except ValueError:
            pass
    return os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0


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
    if plan.path != planner.DIRECT:
        ok_e, why_e = capabilities.encoder_usable(project.profile.encoder)
        if not ok_e:
            sys.stderr.write("studio: encoder %s is not usable: %s (it is never switched automatically)\n" % (project.profile.encoder, why_e))
            return 6
    ok, why = planner.admit(plan, runtime.running_live_encodes(exclude=relay),
                            max_live_encodes=int(os.environ.get("BLUESTREAM_STUDIO_MAX_LIVE", "1")),
                            cpu_cores=os.cpu_count() or 4, load_avg=_load_avg())
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
    try:
        with open(os.path.join(wd, "plan"), "w", encoding="utf-8") as fh:
            fh.write(plan.path)
    except OSError:
        pass
    sys.stderr.write("studio: plan=%s (%s)\n" % (plan.path, "; ".join(plan.reasons)))
    # The supervisor runs `ffmpeg "${FFMPEG_ARGS[@]}"`, so emit ARGUMENTS only (no executable name).
    # (Emitting argv[0] made a real relay exec `ffmpeg ffmpeg ...`; found by the Docker end-to-end run.)
    sys.stdout.buffer.write(b"\0".join(a.encode() for a in args[1:]) + b"\0")
    return 0


def _supervisor_status(relay):
    try:
        with open(os.path.join(runtime.run_dir(), "relay-%s.status" % relay), encoding="utf-8") as fh:
            kv = dict(line.rstrip("\n").split("=", 1) for line in fh if "=" in line)
    except OSError:
        return {}
    return {"state": kv.get("STATE", ""), "detail": kv.get("DETAIL", ""), "attempt": kv.get("ATTEMPT", "")}


def cmd_overlay_argv(relay, tokens, ts_offset=0.0, reason="start"):
    """Compose the project's overlays onto relay `relay`'s OWN source. `tokens` = the input options + `-i <source>` of the plain relay.
    Exit codes (every non-zero makes run-relay fall back to the ORIGINAL stream-copy command): 2 invalid, 3 admission refused,
    6 encoder unusable, 7 circuit breaker tripped, 8 overlays not active / nothing to draw (plain copy is cheaper)."""
    import dataclasses
    mk = ovl.read_marker(relay)
    if not mk or not mk["enabled"]:
        return 8
    try:
        project = store.load_published(mk["project"])
    except (ValidationError, OSError, ValueError) as exc:
        sys.stderr.write("studio: overlay project unavailable: %s\n" % exc)
        return 2
    if project.target_relay != relay:
        sys.stderr.write("studio: project '%s' is not bound to relay '%s'\n" % (mk["project"], relay))
        return 2
    if not project.visible_overlays():
        sys.stderr.write("studio: no visible overlays: the stream stays in plain copy mode\n")
        return 8
    ok, why = ovl.allow_start(relay, mk["nonce"], reason=reason)
    if not ok:
        sys.stderr.write("studio: %s\n" % why)
        return 7
    ok_e, why_e = capabilities.encoder_usable(project.profile.encoder)
    if not ok_e:
        sys.stderr.write("studio: encoder %s is not usable: %s (the plain stream is kept)\n" % (project.profile.encoder, why_e))
        return 6
    if any(o.kind == "browser" for o in project.overlays):
        ok_b, why_b = browsercap.usable()
        if not ok_b:
            sys.stderr.write("studio: browser source unavailable: %s\n" % why_b)
            return 2
    plan = planner.Plan(planner.LIVE, ("overlays on an existing stream",))
    okk, whyk = planner.admit(plan, runtime.running_live_encodes(exclude=relay),
                              max_live_encodes=int(os.environ.get("BLUESTREAM_STUDIO_MAX_LIVE", "1")),
                              cpu_cores=os.cpu_count() or 4, load_avg=_load_avg())
    if not okk:
        sys.stderr.write("studio: refused: %s\n" % whyk)
        return 3
    try:
        i = len(tokens) - 1 - tokens[::-1].index("-i")
        source = tokens[i + 1]
    except (ValueError, IndexError):
        return 2
    live = not os.path.isfile(source)
    info = runtime.probe_source(source, live)
    p = project.profile
    w = p.width or (((info.width or 1280) // 2) * 2)
    h = p.height or (((info.height or 720) // 2) * 2)
    f = p.fps or (int(round(info.fps)) if info.fps and 10 <= info.fps <= 60 else 30)
    prof = dataclasses.replace(p, width=w, height=h, fps=f)
    wd = _workdir(relay)
    _write_static_text(project, wd)
    try:
        args = sff.build_overlay_args(project, tokens, _out_url(relay), wd, runtime.media_dir(), prof,
                                      has_audio=bool(info.acodec) if info.vcodec else True, ts_offset=ts_offset)
    except ValidationError as exc:
        sys.stderr.write("studio: cannot compose: %s\n" % exc)
        return 2
    args = _attach_progress(args, relay, wait=2.0)
    try:
        with open(os.path.join(wd, "plan"), "w", encoding="utf-8") as fh:
            fh.write("overlay-live-composition")
    except OSError:
        pass
    sys.stderr.write("studio: overlays on '%s' (revision %d): %dx%d@%d, encoder %s\n" % (relay, project.revision, w, h, f, prof.encoder))
    sys.stdout.buffer.write(b"\0".join(a.encode() for a in args[1:]) + b"\0")
    return 0


def _published_mtime(name):
    try:
        return os.stat(os.path.join(store.studio_dir(), name + ".json")).st_mtime_ns
    except OSError:
        return None


def _read_plan(wd):
    try:
        with open(os.path.join(wd, "plan"), encoding="utf-8") as fh:
            return fh.read(40).strip()
    except OSError:
        return ""


def cmd_widgets(relay, name):
    """Sidecar: refresh live text ~1/s, hold the live-slot lease, and publish real
    health (progress listener + process stats + supervisor state)."""
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    inplace = name == "@overlay"
    if inplace:
        mk = ovl.read_marker(relay)
        if not mk:
            sys.stderr.write("studio: no overlay switch for '%s'\n" % relay)
            return 2
        name = mk["project"]
    project = store.load_published(name)
    wd = _workdir(relay)
    run = runtime.run_dir()
    dynamic = inplace or any(o.dynamic for o in project.visible_overlays())
    healthy_since = None
    if dynamic:
        runtime.take_live_lease(relay)
    struct0 = project.structure_fingerprint()
    applied_rev, pending_restart, pub_mtime = project.revision, False, _published_mtime(name)
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
            m = _published_mtime(name)
            if m != pub_mtime:
                pub_mtime = m
                try:
                    newer = store.load_published(name)
                    if newer.structure_fingerprint() == struct0:
                        project, applied_rev, pending_restart = newer, newer.revision, False   # hot: content only, no restart
                        data, last = {}, {}
                    else:
                        pending_restart = True                                                    # structure changed: needs a restart
                except (ValidationError, OSError, ValueError):
                    pass
            _refresh_data(project, data, last)
            _write_static_text(project, wd, data=data)
            if tick % 2 == 0:
                pid = health.find_ffmpeg_pid(out_url)
                snap = tracker.snapshot(sampler.sample(pid), _supervisor_status(relay))
                snap.update(revision=applied_rev, pending_restart="yes" if pending_restart else "", plan=_read_plan(wd))
                if inplace:
                    if snap.get("state") == "HEALTHY":
                        healthy_since = healthy_since or time.monotonic()
                        if time.monotonic() - healthy_since >= ovl.HEALTHY_AFTER_S:
                            ovl.mark_healthy(relay)
                    else:
                        healthy_since = None
                health.write_health(run, relay, snap)
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
               "cpu_cores", "rss_mb", "last_failure", "supervisor_attempt", "output_seconds",
               "revision", "pending_restart", "plan")


def health_pairs(relay):
    """key/value pairs for web-ctl (every value bounded and stripped of control chars)."""
    doc = health.read_health(runtime.run_dir(), relay)
    pairs = [("overlay", ovl.state(relay))]
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


def _emit_image(mime, data, rc=0):
    import base64
    out = b"mime" + bytes([0]) + mime.encode() + bytes([0]) + b"data" + bytes([0]) + base64.b64encode(data) + bytes([0])
    sys.stdout.buffer.write(out)
    return rc


def cmd_frame(name):
    """Real frame of the project's source as base64 JPEG (for the console, via web-ctl)."""
    try:
        data = preview.frame_jpeg(store.load_draft(name))
    except (ValidationError, OSError, ValueError):
        data = None
    if not data:
        sys.stderr.write("studio: no preview frame available\n")
        return _emit_image("", b"", 1)
    return _emit_image("image/jpeg", data)


def cmd_thumb(asset):
    data = preview.thumb_png(asset)
    if not data:
        sys.stderr.write("studio: no thumbnail available\n")
        return _emit_image("", b"", 1)
    return _emit_image("image/png", data)


def cmd_proxy(media):
    """Editor video preview: fragmented MP4 of one Media Library video on stdout (run by web-ctl as root; FFmpeg runs as the media owner)."""
    try:
        started = preview.start_proxy(media)
    except preview.ProxyError as exc:
        sys.stderr.write("studio: preview unavailable (%s)\n" % exc.kind)
        return exc.exit_status
    proc, _mode = started
    out = sys.stdout.buffer
    written = 0
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            written += len(chunk)
    except (BrokenPipeError, OSError):
        pass                                   # the browser went away: stop encoding at once
    finally:
        code = preview.proxy_returncode(proc)
        preview.terminate_proxy(proc)
    return 0 if written or code == 0 else preview.PROXY_EXIT_FAILED


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
        if cmd == "overlay-argv" and NAME_RE.match(argv[2]):
            rest, off, reason = argv[3:], 0.0, "start"
            while len(rest) >= 2 and rest[0] in ("--ts-offset", "--reason"):
                if rest[0] == "--ts-offset":
                    try:
                        off = float(rest[1])
                    except ValueError:
                        return 2
                    if not 0.0 <= off <= 4.0e6:
                        return 2
                else:
                    reason = rest[1]
                    if reason not in ("start", "swap", "retry"):
                        return 2
                rest = rest[2:]
            toks = rest[1:] if rest and rest[0] == "--" else None
            return cmd_overlay_argv(argv[2], toks, off, reason) if toks else 2
        if cmd in ("argv", "widgets"):
            relay, name = argv[2], argv[3]
            if not (NAME_RE.match(relay) and (NAME_RE.match(name) or (cmd == "widgets" and name == "@overlay"))):
                return 2
            return cmd_argv(relay, name) if cmd == "argv" else cmd_widgets(relay, name)
        if cmd == "plan" and NAME_RE.match(argv[2]):
            return cmd_plan(argv[2])
        if cmd == "frame" and NAME_RE.match(argv[2]):
            return cmd_frame(argv[2])
        if cmd == "thumb" and model.ASSET_RE.match(argv[2]):
            return cmd_thumb(argv[2])
        if cmd == "proxy" and model.ASSET_RE.match(argv[2]):
            return cmd_proxy(argv[2])
        if cmd == "install-id":
            print(entitlement.get_install_id(create=True))
            return 0
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
