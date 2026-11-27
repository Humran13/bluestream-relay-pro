"""JSON endpoints behind the Studio workspace (save, apply, media browser, uploads, stream controls, settings).

Same rules as the classic form routes in views.py: console session required, CSRF enforced by the
app-wide hook (the workspace sends it in the X-CSRF-Token header), backend entitlement check, every value
validated by the model before anything is stored. Nothing here starts a second streaming engine: Start/Stop
call the existing relay lifecycle operations of the engine bridge, and destinations use the existing
destination attachment operations. Stream keys are never read or returned.
"""
import dataclasses
import math
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from flask import Response, current_app, jsonify, request, session, stream_with_context

from . import browsercap, capabilities, entitlement, media, preview, settings, store, templates_lib, widgets
from . import overlay as ovl
from .model import KINDS, NAME_RE, OutputProfile, Overlay, Project, ValidationError

SOURCE_KINDS = ("relay", "playlist", "media", "slideshow", "audio")
MAX_JSON_BYTES = 512 * 1024
_OVERLAY_TYPES = {f.name: type(f.default) for f in dataclasses.fields(Overlay) if f.name != "kind"}
_PROFILE_FIELDS = ("width", "height", "fps", "video_kbps", "audio_kbps", "preset", "scaler", "encoder")
_PROFILE_TYPES = {f.name: type(f.default) for f in dataclasses.fields(OutputProfile)}
_PROJECT_KEYS = {"source_kind", "source_ref", "slide_seconds", "slide_shuffle", "background", "visualizer",
                 "profile", "overlays", "target_relay"}
_thumb_cache = {}  # (media name) -> (expires, bytes): bridge thumbnails are cheap to serve, dear to make


# ---------------------------------------------------------------------------
# strict JSON -> model conversion
# ---------------------------------------------------------------------------
def _coerce(value, typ, key):
    if typ is bool:
        if isinstance(value, bool):
            return value
    elif typ is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and math.isfinite(value) and value == int(value):
            return int(value)
    elif typ is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    elif typ is str:
        if isinstance(value, str):
            return value
    raise ValidationError("'%s' has the wrong type" % key)


def overlay_from_json(doc):
    if not isinstance(doc, dict):
        raise ValidationError("a layer must be an object")
    kind = doc.get("kind")
    if kind not in KINDS:
        raise ValidationError("unknown layer type")
    kw = {"kind": kind}
    for key, value in doc.items():
        if key == "kind":
            continue
        if key not in _OVERLAY_TYPES:
            raise ValidationError("unknown layer field '%s'" % key)
        kw[key] = _coerce(value, _OVERLAY_TYPES[key], key)
    return Overlay(**kw)


def profile_from_json(doc, old):
    if not isinstance(doc, dict):
        raise ValidationError("output settings must be an object")
    kw = dict(old.__dict__)
    for key, value in doc.items():
        if key not in _PROFILE_FIELDS:
            raise ValidationError("unknown output setting '%s'" % key)
        kw[key] = _coerce(value, _PROFILE_TYPES[key], key)
    return OutputProfile(**kw)


def project_from_json(old, doc):
    if not isinstance(doc, dict):
        raise ValidationError("project must be an object")
    extra = set(doc) - _PROJECT_KEYS
    if extra:
        raise ValidationError("unknown project field '%s'" % sorted(extra)[0])
    kind = _coerce(doc.get("source_kind", old.source_kind), str, "source_kind")
    if kind not in SOURCE_KINDS:
        raise ValidationError("choose a source type")
    overlays = doc.get("overlays", None)
    if overlays is None:
        overlays = [dict(o.__dict__) for o in old.overlays]
    if not isinstance(overlays, list):
        raise ValidationError("layers must be a list")
    if "target_relay" in doc and doc["target_relay"] != old.target_relay:
        raise ValidationError("the stream a project edits cannot be changed")
    if old.target_relay and (kind != old.source_kind or doc.get("source_ref", old.source_ref) != old.source_ref):
        raise ValidationError("the source of an existing stream cannot be changed from Studio")
    proj = Project(
        old.name, kind,
        _coerce(doc.get("source_ref", old.source_ref), str, "source_ref").strip(),
        [overlay_from_json(o) for o in overlays],
        profile_from_json(doc["profile"], old.profile) if "profile" in doc else old.profile,
        revision=old.revision,
        slide_seconds=_coerce(doc.get("slide_seconds", old.slide_seconds), int, "slide_seconds"),
        slide_shuffle=_coerce(doc.get("slide_shuffle", old.slide_shuffle), bool, "slide_shuffle"),
        background=_coerce(doc.get("background", old.background), str, "background").strip(),
        visualizer=_coerce(doc.get("visualizer", old.visualizer), bool, "visualizer"), target_relay=old.target_relay)
    if any(o.kind == "browser" for o in proj.overlays):
        ok, why = browsercap.usable()
        if not ok:
            raise ValidationError("browser source is not available: %s" % why)
    return proj.validate()


def project_to_json(project):
    return {
        "source_kind": project.source_kind, "source_ref": project.source_ref,
        "slide_seconds": project.slide_seconds, "slide_shuffle": project.slide_shuffle,
        "background": project.background, "visualizer": project.visualizer,
        "profile": {k: getattr(project.profile, k) for k in _PROFILE_FIELDS},
        "overlays": [dict(o.__dict__) for o in project.overlays],
        "target_relay": project.target_relay,
        "revision": project.revision,
    }


def render_texts(overlays, now=None):
    """What each text-backed layer shows right now (pure rendering, no FFmpeg)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for o in overlays:
        if o.kind in ("rect", "image", "video", "browser"):
            out.append("")
            continue
        try:
            out.append(widgets.render_text(o, now))
        except (ValidationError, ValueError, KeyError):
            out.append("(check the time settings)")
    return out


def media_refs(project):
    """[(where, file name)] for every Media Library file the project reads."""
    refs = []
    if project.source_kind == "media":
        refs.append(("Background source", project.source_ref))
    elif project.source_kind == "audio":
        refs.append(("Audio source", project.source_ref))
        if not project.background.startswith("#"):
            refs.append(("Audio background", project.background))
    elif project.source_kind == "slideshow":
        refs += [("Slideshow", n) for n in project.slides()]
    for i, o in enumerate(project.overlays):
        if o.kind in ("image", "video") and o.asset:
            refs.append(("Layer %d (%s)" % (i + 1, o.label or o.kind), o.asset))
    return refs


class _BridgeCache:
    """Short-lived cache for the READ-ONLY privileged bridge calls the editor repeats every few seconds.

    Measured: one `sudo web-ctl <op>` costs ~150-250 ms of CPU even on a fast machine (it loads the whole bash engine), and an open
    editor polls 3-5 of them every 5 s; two open tabs double it. A few seconds of sharing removes almost all of that load without
    making the picture stale. Any MUTATING engine call clears the cache at once, so the UI never shows pre-change state.
    BLUESTREAM_STUDIO_BRIDGE_CACHE=0 disables it (tests)."""
    TTL = {"relay_list": 3.0, "playlist_list": 5.0, "snapshot": 30.0, "destination_list": 10.0, "media_list": 5.0,
           "studio_health": 4.0, "relay_publisher_status": 4.0, "relay_swap_ready": 15.0, "relay_destinations_get": 10.0}

    def __init__(self):
        import threading
        self.lock, self.items = threading.Lock(), {}
        self.on = os.environ.get("BLUESTREAM_STUDIO_BRIDGE_CACHE", "1") != "0"

    def get(self, key, ttl, fn):
        if not self.on or ttl <= 0:
            return fn()
        now = time.monotonic()
        with self.lock:
            hit = self.items.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()                              # exceptions are never cached
        with self.lock:
            if len(self.items) > 256:
                self.items.clear()
            self.items[key] = (now + ttl, value)
        return value

    def clear(self):
        with self.lock:
            self.items.clear()

    def wrap(self, eng):
        return _CachedEngine(eng, self)


class _CachedEngine:
    def __init__(self, eng, cache):
        self._e, self._c = eng, cache

    def call(self, op, *a, **k):
        return self._c.get(("call", op), _BridgeCache.TTL.get(op, 0), lambda: self._e.call(op, *a, **k))

    def read(self, op, *values, **k):
        return self._c.get(("read", op, values), _BridgeCache.TTL.get(op, 0), lambda: self._e.read(op, *values, **k))

    def __getattr__(self, name):
        from ..engine import ALLOWED_MUTATION_OPERATIONS
        attr = getattr(self._e, name)
        if not callable(attr):
            return attr
        if name in ALLOWED_MUTATION_OPERATIONS:
            def mutate(*a, **k):
                try:
                    return attr(*a, **k)
                finally:
                    self._c.clear()                # whatever happened, what the UI shows next must come from the real state
            return mutate
        if name in _BridgeCache.TTL:
            return lambda *a, **k: self._c.get(("m", name, a), _BridgeCache.TTL[name], lambda: attr(*a, **k))
        return attr


def register_api(console, find_relay_raw, engine_raw):
    cache = _BridgeCache()

    def engine_of():
        return cache.wrap(engine_raw())

    def find_relay(name):
        """(row, unavailable): same contract as the dashboard's helper, but through the shared read cache."""
        try:
            relays = engine_of().call("relay_list") or []
        except Exception as exc:                   # noqa: BLE001 - EngineError and friends
            current_app.logger.warning("relay_list unavailable: %s", exc)
            return None, True
        for row in relays:
            if isinstance(row, dict) and row.get("name") == name:
                return row, False
        return None, False

    _snap_cache = {}    # per app: the public-address snapshot, refreshed at most once a minute
    def jerr(message, code=400, **extra):
        return jsonify(ok=False, error=message, **extra), code

    def guard(write=False, name=None, body=False):
        """None when the request may proceed, else a JSON error response."""
        if not session.get("authenticated"):
            return jerr("Your session has ended. Sign in again.", 401)
        if write:
            v = entitlement.current()
            if not v.ok:
                return jerr("Studio is not available: %s." % v.reason, 403)
        if name is not None and (not NAME_RE.match(name) or not store.exists(name)):
            return jerr("That Studio project was not found.", 404)
        if body and (request.content_length or 0) > MAX_JSON_BYTES:
            return jerr("That request is too large.", 413)
        return None

    def body_json():
        doc = request.get_json(silent=True)
        if not isinstance(doc, dict):
            raise ValidationError("the request body must be a JSON object")
        return doc

    def library():
        """Media Library rows, or None when the engine cannot be reached (callers degrade, never crash)."""
        try:
            return engine_of().call("media_list") or []
        except Exception as exc:  # EngineError and friends
            current_app.logger.info("studio media list unavailable: %s", exc)
            return None

    def missing_media(project):
        rows = library()
        if rows is None:
            return []                       # cannot tell; do not block on a guess
        have = {r.get("name") for r in rows if isinstance(r, dict)}
        return [(where, n) for where, n in media_refs(project) if n not in have]

    # ---- media browser -----------------------------------------------------
    @console.route("/studio/media.json", methods=["GET"])
    def studio_media_json():
        bad = guard()
        if bad:
            return bad
        rows = library()
        if rows is None:
            return jerr("The Media Library could not be read right now.", 503)
        kind = request.args.get("kind", "")
        items = media.listing(rows, request.args.get("q", ""), kind if kind in ("image", "video", "audio") else "")
        everything = media.listing(rows)
        counts = {k: sum(1 for r in everything if r["kind"] == k) for k in ("video", "image", "audio")}
        return jsonify(ok=True, items=items, counts=counts, total=len(everything))

    @console.route("/studio/media/<asset>/thumb", methods=["GET"])
    def studio_media_thumb(asset):
        if not session.get("authenticated"):
            return Response("sign in", status=401)
        if not preview.ASSET_RE.match(asset or ""):
            return Response("not found", status=404)
        data = None
        try:
            data = preview.thumb_png(asset)           # works where this user can read the file
        except Exception:                            # noqa: BLE001 - preview must never raise
            data = None
        if not data:
            hit = _thumb_cache.get(asset)
            now = time.monotonic()
            if hit and hit[0] > now:
                data = hit[1]
            else:
                try:
                    data = engine_of().studio_thumb(asset)   # hardened VPS: read-only bridge
                except Exception as exc:
                    current_app.logger.info("studio thumbnail unavailable for '%s': %s", asset, exc)
                    data = None
                if data:
                    if len(_thumb_cache) > 128:
                        _thumb_cache.clear()
                    _thumb_cache[asset] = (now + 300, data)
        if not data:
            return Response("no preview", status=404)
        return Response(data, mimetype="image/png", headers={"Cache-Control": "private, max-age=120"})

    @console.route("/studio/media/upload", methods=["POST"])
    def studio_media_upload():
        bad = guard(write=True)
        if bad:
            return bad
        from .. import app as webapp   # lazily: app imports this package while it is being created
        cfg = current_app.config
        max_size = cfg.get("MAX_UPLOAD_SIZE", webapp.DEFAULT_MAX_UPLOAD_SIZE)
        up = request.files.get("media")
        if up is None or not up.filename:
            return jerr("No file selected.")
        safe = media.sanitize(up.filename)
        if safe is None:
            return jerr("Unsupported or unsafe file name. Allowed: video (.mp4 .mkv .mov .webm .m4v .ts), "
                        "images (.png .jpg .jpeg .webp .gif) and audio (.mp3 .wav .flac .ogg .m4a .aac .opus).")
        kind = media.classify(safe)
        limit = media.MAX_IMAGE_BYTES if kind == "image" else max_size
        if (request.content_length or 0) > limit + 1024 * 1024:
            return jerr("File exceeds the %s limit (%s)." % (kind, media.human_size(limit)), 413)
        upload_dir = cfg.get("WEB_UPLOAD_DIR", webapp.PRODUCTION_UPLOAD_DIR)
        try:
            os.makedirs(upload_dir, exist_ok=True)
        except OSError:
            pass
        staged = os.path.join(upload_dir, safe)
        if os.path.exists(staged):
            return jerr("A file with that name is already being processed.", 409)

        def discard():
            try:
                os.unlink(staged)
            except OSError:
                pass
        try:
            up.save(staged)
            size = os.stat(staged).st_size
        except OSError as exc:
            current_app.logger.warning("studio upload save failed: %s", exc)
            discard()
            return jerr("Upload could not be stored. Please try again.", 500)
        if size == 0:
            discard()
            return jerr("The uploaded file is empty.")
        if size > limit:                     # the staged size is authoritative; Content-Length can lie
            discard()
            return jerr("File exceeds the %s limit (%s)." % (kind, media.human_size(limit)), 413)
        try:
            import shutil
            free = shutil.disk_usage(os.path.dirname(staged)).free
        except OSError:
            free = None
        if free is not None and free < size + cfg.get("DISK_RESERVE_BYTES", webapp.DEFAULT_DISK_RESERVE_BYTES):
            discard()
            return jerr("Not enough free storage for this upload.", 507)
        try:
            engine_of().media_import_staged(safe)
        except Exception as exc:
            discard()
            code = getattr(exc, "code", None)
            current_app.logger.warning("studio media import '%s' failed: %s", safe, exc)
            if code == "MEDIA_EXISTS":
                return jerr("A file named '%s' already exists in the Media Library." % safe, 409)
            if code in ("NOT_MEDIA", "PROBE_FAILED", "INVALID_MEDIA"):
                return jerr("That file is not recognised as valid media.", 422)
            return jerr(webapp._upload_error_message(exc) if hasattr(exc, "code") else
                        "Media import failed. Please try again.", 500)
        return jsonify(ok=True, name=safe, kind=kind, message="'%s' added to the Media Library." % safe)

    # ---- preview text ------------------------------------------------------
    @console.route("/studio/<name>/preview-text.json", methods=["POST"])
    def studio_preview_text(name):
        bad = guard(name=name, body=True)
        if bad:
            return bad
        try:
            layers = body_json().get("layers")
            if not isinstance(layers, list) or len(layers) > 24:
                raise ValidationError("layers must be a list")
            overlays = []
            for d in layers:
                try:
                    overlays.append(overlay_from_json(d))
                except ValidationError:
                    overlays.append(Overlay("text"))
            return jsonify(ok=True, texts=render_texts(overlays))
        except ValidationError as exc:
            return jerr(str(exc))

    # ---- base-source preview (unsaved source choice) -----------------------
    @console.route("/studio/<name>/source-frame", methods=["GET"])
    def studio_source_frame(name):
        bad = guard(name=name)
        if bad:
            return Response("not found", status=404)
        saved = store.load_draft(name)
        kind, ref = request.args.get("kind", saved.source_kind), request.args.get("ref", saved.source_ref)
        bg = request.args.get("bg", saved.background)
        try:
            proj = Project(name, kind, ref, background=bg, slide_seconds=saved.slide_seconds).validate()
        except ValidationError:
            return Response("not found", status=404)
        data = preview.frame_jpeg(proj)
        same = (kind, ref, bg) == (saved.source_kind, saved.source_ref, saved.background)
        if not data and same:
            try:
                data = engine_of().studio_frame(name)
            except Exception:
                data = None
        if not data and kind in ("media", "slideshow", "audio"):
            first = (proj.slides()[0] if kind == "slideshow" else ref if kind == "media"
                     else (bg if not bg.startswith("#") else ""))
            if first:
                try:
                    data = engine_of().studio_thumb(first)
                except Exception:
                    data = None
        if not data:
            return Response("no preview", status=404)
        mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=10"})

    # ---- save / apply ------------------------------------------------------
    @console.route("/studio/<name>/save.json", methods=["POST"])
    def studio_save_json(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        try:
            old = store.load_draft(name)
            new = project_from_json(old, body_json().get("project"))
            cap = capabilities.assess(new.profile)
            if new.profile != old.profile and cap["level"] == "block":
                raise ValidationError(cap["message"])
            if new.profile.encoder != old.profile.encoder:
                ok_e, why_e = capabilities.encoder_usable(new.profile.encoder)
                if not ok_e:
                    raise ValidationError("encoder %s cannot be used on this server: %s" % (new.profile.encoder, why_e))
            store.save_draft(new)
        except (ValidationError, ValueError, OSError) as exc:
            return jerr("Not saved: %s." % exc)
        missing = missing_media(new)
        return jsonify(ok=True, message="Draft saved. The live stream is unchanged until you press Apply to Live.",
                       unpublished=store.has_unpublished_changes(name), capacity=cap,
                       missing=[{"where": w, "name": n} for w, n in missing],
                       saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @console.route("/studio/<name>/publish.json", methods=["POST"])
    def studio_publish_json(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        try:
            restart = body_json().get("restart") is True
        except ValidationError as exc:
            return jerr(str(exc))
        try:
            draft = store.load_draft(name)
            try:
                prev = store.load_published(name)
            except (ValidationError, OSError, ValueError):
                prev = None
        except (ValidationError, OSError, ValueError):
            return jerr("That project file is damaged; roll back to an earlier revision.", 500)
        target = draft.target_relay
        if target:
            relay, _ = find_relay(target)
            mk = ovl.read_marker(target)
            running = bool(relay and relay.get("active") == "yes" and mk and mk["enabled"] and mk["project"] == name)
        else:
            relay, _ = find_relay(name)
            running = bool(relay and relay.get("type") == "studio" and relay.get("active") == "yes")
        # HOT apply: the running pipeline is a live composition and only file-backed text CONTENT changed, so the
        # sidecar adopts the new values within a second and FFmpeg is never touched.
        hot = bool(running and prev is not None and prev.runs_live_encode and draft.runs_live_encode
                   and prev.structure_fingerprint() == draft.structure_fingerprint())
        kind = ""
        if running and not hot:
            kind = ("swap" if swap_state(target)["ready"] else "unit-restart") if target else "restart"
        if running and not hot and not restart:
            if kind == "swap":
                return jerr("These changes alter the picture on air. Confirm the switch: the picture source of '%s' is replaced (about 2 seconds "
                            "of pause) while its destination connections stay up." % target, 409, needs_restart=True, kind=kind)
            if kind == "unit-restart":
                return jerr("'%s' must be restarted once to switch its picture live (its running supervisor predates this update). Its "
                            "destinations reconnect with the same stream key (about 10-20 seconds)." % target, 409, needs_restart=True, kind=kind)
            return jerr("These changes alter the video pipeline. Confirm the controlled restart of this Studio stream "
                        "(other streams are not touched).", 409, needs_restart=True, kind=kind)
        gone = missing_media(draft)
        if gone:
            return jerr("Cannot apply: %s." % "; ".join("%s uses '%s', which is not in the Media Library" % g
                                                        for g in gone[:4]), 422,
                        missing=[{"where": w, "name": n} for w, n in gone])
        if running and not hot:
            ok_e, why_e = capabilities.encoder_usable(draft.profile.encoder)
            if not ok_e:
                return jerr("Cannot apply: encoder %s is not usable: %s" % (draft.profile.encoder, why_e), 422)
        try:
            rev = store.publish(name)
            if running and not hot:
                if kind == "swap":
                    engine_of().relay_swap_source(target)
                else:
                    engine_of().relay_restart(target or name)
        except Exception as exc:
            current_app.logger.warning("studio publish '%s' failed: %s", name, exc)
            return jerr("Publish failed; the previous live revision is still in use.", 500)
        mode = "hot" if hot else ((kind or "restart") if running else "stored")
        msg = {"hot": "Revision %d applied live with no interruption.",
               "restart": "Revision %d applied; this Studio stream is restarting (verifying it comes back healthy).",
               "swap": "Revision %d applied; the picture source is being replaced (destination connections stay up; verifying).",
               "unit-restart": "Revision %d applied; the stream is restarting once (verifying it comes back healthy).",
               "stored": ("Revision %d is saved as ready. It is NOT on air: press Activate to put the overlays on the stream." if target
                          else "Revision %d applied. It goes on air when the stream starts.")}[mode] % rev
        return jsonify(ok=True, revision=rev, previous=prev.revision if prev else 0, mode=mode, restarted=mode in ("restart", "unit-restart"),
                       unpublished=False, message=msg)

    @console.route("/studio/<name>/revert-live.json", methods=["POST"])
    def studio_revert_live(name):
        """Staged-apply rollback: put an earlier published revision back on air (restarts only this Studio stream)."""
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        try:
            rev = body_json().get("revision")
            if not isinstance(rev, int) or isinstance(rev, bool):
                raise ValidationError("choose a revision")
            new_rev = store.republish(name, rev)
        except ValidationError as exc:
            return jerr(str(exc))
        try:
            target = store.load_draft(name).target_relay
        except (ValidationError, OSError, ValueError):
            target = ""
        relay, _ = find_relay(target or name)
        mk = ovl.read_marker(target) if target else None
        live = bool(relay is not None and relay.get("active") == "yes" and (
            (target and mk and mk["enabled"]) or (not target and relay.get("type") == "studio")))
        if live:
            try:
                if target and swap_state(target)["ready"]:
                    engine_of().relay_swap_source(target)
                else:
                    engine_of().relay_restart(target or name)
            except Exception as exc:
                current_app.logger.warning("studio revert '%s' restart failed: %s", name, exc)
                return jerr("Reverted the stored revision but the restart failed; check the Streams page.", 500)
        return jsonify(ok=True, revision=new_rev, message="Revision %d (the earlier layout) is back on air." % new_rev)

    @console.route("/studio/assess.json", methods=["POST"])
    def studio_assess_json():
        bad = guard(body=True)
        if bad:
            return bad
        try:
            prof = profile_from_json(body_json().get("profile"), OutputProfile()).validate()
        except ValidationError as exc:
            return jerr(str(exc))
        return jsonify(ok=True, capacity=capabilities.assess(prof))

    # ---- stream status and controls ---------------------------------------
    def swap_state(relay_name):
        """Can the running relay replace its FFmpeg on demand (publishers stay connected)? Never raises."""
        try:
            doc = engine_of().relay_swap_ready(relay_name)
            return {"active": doc.get("active") == "yes", "ready": doc.get("ready") == "yes", "reason": doc.get("reason") or ""}
        except Exception:                                   # noqa: BLE001 - bridge older than this release, or down
            return {"active": None, "ready": False, "reason": "UNKNOWN"}

    def stream_doc(name, target=None):
        relay, unavailable = find_relay(target or name)
        if target is None and relay is not None and relay.get("type") != "studio":
            relay = None
        doc = {"exists": relay is not None, "unavailable": bool(unavailable), "active": False, "health": "",
               "health_detail": "", "state": "", "detail": "", "speed": "", "fps": "", "cpu_cores": "",
               "restarts": "", "dropped": "", "rss_mb": "", "last_failure": "", "revision": "", "plan": "",
               "pending_restart": False, "publishers": [], "destinations_attached": 0}
        if relay is None:
            return doc
        rname = target or name
        doc["hls_url"] = _hls_urls("relay", rname)
        if target:
            mk = ovl.read_marker(target)
            doc["inplace"] = {"relay": target, "marker": ("on" if mk and mk["enabled"] else "off") if mk else "none",
                              "type": relay.get("type") or "", "swap": swap_state(target) if relay.get("active") == "yes" else
                              {"active": False, "ready": False, "reason": "NOT_RUNNING"}}
        doc.update(active=relay.get("active") == "yes", health=relay.get("health") or "",
                   health_detail=relay.get("health_detail") or "")
        eng = engine_of()
        try:
            h = eng.read("studio_health", rname) or {}
            if target:
                doc["inplace"]["overlay"] = h.get("overlay") or ""
            doc.update(state=h.get("state") or "", detail=h.get("detail") or "", speed=h.get("speed") or "",
                       fps=h.get("fps") or "", cpu_cores=h.get("cpu_cores") or "", restarts=h.get("restarts") or "0",
                       dropped=h.get("dropped_frames") or "0", rss_mb=h.get("rss_mb") or "",
                       last_failure=h.get("last_failure") or "", revision=h.get("revision") or "", plan=h.get("plan") or "",
                       pending_restart=h.get("pending_restart") == "yes")
        except Exception:
            pass
        try:
            pubs = eng.read("relay_publisher_status", rname) or []
            doc["publishers"] = [{"id": p.get("id"), "display_name": p.get("display_name") or p.get("id"),
                                  "state": p.get("state") or "", "detail": p.get("detail") or "",
                                  "blocked_by": p.get("blocked_by") or ""} for p in pubs]
        except Exception:
            pass
        return doc

    @console.route("/studio/<name>/status.json", methods=["GET"])
    def studio_status_json(name):
        bad = guard(name=name)
        if bad:
            return bad
        try:
            draft = store.load_draft(name)
        except (ValidationError, OSError, ValueError):
            return jerr("That project file is damaged.", 500)
        try:
            published = store.load_published(name).revision
        except (ValidationError, OSError, ValueError):
            published = 0
        return jsonify(ok=True, stream=stream_doc(name, draft.target_relay or None), unpublished=store.has_unpublished_changes(name),
                       published_revision=published, capacity=capabilities.assess(draft.profile),
                       load=capabilities.load_avg())

    @console.route("/studio/<name>/stream.json", methods=["POST"])
    def studio_stream_json(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        try:
            owner = store.load_draft(name).target_relay
        except (ValidationError, OSError, ValueError):
            owner = ""
        if owner:
            return jerr("This project edits the existing stream '%s'. Start and stop it on the Streams page; use Activate here to put overlays on air." % owner, 409)
        try:
            action = body_json().get("action")
        except ValidationError as exc:
            return jerr(str(exc))
        relay, unavailable = find_relay(name)
        if relay is not None and relay.get("type") != "studio":
            return jerr("A stream named '%s' exists but it is not a Studio stream." % name, 409)
        eng = engine_of()
        try:
            if action == "create":
                if relay is not None:
                    return jerr("The stream already exists.", 409)
                try:
                    store.load_published(name)
                except (ValidationError, OSError, ValueError):
                    # Nothing applied yet: the first stream uses the saved draft, applied once, explicitly stated.
                    store.publish(name)
                eng.relay_create_studio(name, name)
                return jsonify(ok=True, message="Stream '%s' created. Attach destinations, then press Start Streaming." % name)
            if relay is None:
                return jerr("Create the stream first." if not unavailable else "The stream list is unavailable right now.", 409)
            if action == "start":
                if relay.get("active") == "yes":
                    return jerr("The stream is already running.", 409)
                eng.relay_start(name)
                return jsonify(ok=True, message="Stream '%s' is starting." % name)
            if action == "stop":
                eng.relay_stop(name)
                return jsonify(ok=True, message="Stream '%s' stopped." % name)
        except Exception as exc:
            current_app.logger.warning("studio stream %s '%s' failed: %s", action, name, exc)
            return jerr("The %s did not complete. See the Streams page for the stream's state." % action, 500)
        return jerr("Unknown action.")

    @console.route("/studio/<name>/takeover.json", methods=["POST"])
    def studio_takeover_json(name):
        """Controlled switch of ONE destination to this running stream (explicit confirmation required)."""
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        if store.load_draft(name).target_relay:
            return jerr("Overlays on an existing stream keep its destinations as they are: there is nothing to switch.", 409)
        try:
            doc = body_json()
        except ValidationError as exc:
            return jerr(str(exc))
        dest = doc.get("destination")
        if not isinstance(dest, str) or not NAME_RE.match(dest):
            return jerr("That destination is not valid.")
        if doc.get("confirm") is not True:
            return jerr("Confirm the switch: the other stream stops publishing to this destination.")
        try:
            engine_of().relay_publisher_takeover(name, dest)
        except Exception as exc:
            current_app.logger.warning("takeover '%s'/'%s' failed: %s", name, dest, exc)
            return jerr("The switch did not complete; the previous stream was kept or reinstated.", 500)
        return jsonify(ok=True, message="Destination switched to '%s'." % name)

    # ---- destinations (existing Destinations system) ----------------------
    @console.route("/studio/<name>/destinations.json", methods=["GET"])
    def studio_destinations_get(name):
        bad = guard(name=name)
        if bad:
            return bad
        try:
            rows = engine_of().destination_list() or []
        except Exception as exc:
            current_app.logger.info("destination list unavailable: %s", exc)
            return jerr("Destinations could not be read right now.", 503)
        attached, relay = [], None
        try:
            target = store.load_draft(name).target_relay
        except (ValidationError, OSError, ValueError):
            target = ""
        rname = target or name
        relay, _ = find_relay(rname)
        if relay is not None and (target or relay.get("type") == "studio"):
            try:
                attached = list(engine_of().relay_destinations_get(rname) or [])
            except Exception:
                attached = []
        dests = [{"name": r.get("name"), "display_name": r.get("display_name") or r.get("name"),
                  "platform": r.get("platform") or "", "server_url": r.get("server_url") or "",
                  "enabled": r.get("enabled") == "yes", "has_stream_key": r.get("has_stream_key") == "yes"}
                 for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]
        return jsonify(ok=True, destinations=dests, attached=attached, inplace=bool(target), stream_name=rname,
                       stream_exists=relay is not None and bool(target or relay.get("type") == "studio"))

    @console.route("/studio/<name>/destinations.json", methods=["POST"])
    def studio_destinations_post(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        if store.load_draft(name).target_relay:
            return jerr("Destinations of an existing stream are managed on the Destinations page; Studio leaves them untouched.", 409)
        try:
            ids = body_json().get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) and NAME_RE.match(i) for i in ids) or len(ids) > 16:
                raise ValidationError("choose destinations from the list")
        except ValidationError as exc:
            return jerr(str(exc))
        relay, _ = find_relay(name)
        if relay is None or relay.get("type") != "studio":
            return jerr("Create the stream before attaching destinations.", 409)
        try:
            known = {r.get("name") for r in (engine_of().destination_list() or []) if isinstance(r, dict)}
            if any(i not in known for i in ids):
                raise ValidationError("one of those destinations no longer exists")
            engine_of().relay_destinations_set(name, ids)
        except ValidationError as exc:
            return jerr(str(exc))
        except Exception as exc:
            current_app.logger.warning("studio destinations '%s' failed: %s", name, exc)
            return jerr("Destinations could not be saved.", 500)
        return jsonify(ok=True, attached=ids, message="Destinations saved.")

    # ---- overlays on an EXISTING stream ---------------------------------------------------------------------------------------
    def inplace_project(name):
        """(project, error response) for a project that edits an existing stream."""
        try:
            p = store.load_draft(name)
        except (ValidationError, OSError, ValueError):
            return None, jerr("That project file is damaged.", 500)
        if not p.target_relay:
            return None, jerr("This project is not attached to an existing stream.", 409)
        return p, None

    @console.route("/studio/overlay/new.json", methods=["POST"])
    def studio_overlay_new():
        """Open (creating it once) the overlay project of an EXISTING stream. Never touches the stream."""
        bad = guard(write=True, body=True)
        if bad:
            return bad
        try:
            relay_name = body_json().get("relay")
            if not isinstance(relay_name, str) or not NAME_RE.match(relay_name):
                raise ValidationError("choose a stream")
        except ValidationError as exc:
            return jerr(str(exc))
        relay, unavailable = find_relay(relay_name)
        if relay is None:
            return jerr("That stream was not found." if not unavailable else "The stream list is unavailable right now.", 404)
        if relay.get("type") not in ovl.SUPPORTED_RELAY_TYPES:
            return jerr("Overlays can be added to streams read from a file, URL, HLS, RTMP, RTSP or public webpage; '%s' is a %s stream." % (
                relay_name, relay.get("type") or "different"), 409)
        pname = (relay_name + "-overlay")[:48]
        if store.exists(pname):
            existing, err = inplace_project(pname)
            if err or existing.target_relay != relay_name:
                return jerr("A Studio project named '%s' already exists for something else." % pname, 409)
            return jsonify(ok=True, name=pname, existing=True)
        if find_relay(pname)[0] is not None:
            return jerr("A stream named '%s' already exists; rename it first." % pname, 409)
        try:
            proj = Project(pname, "relay", relay_name, [], OutputProfile(width=0, height=0, fps=0, preset="veryfast", video_kbps=4000),
                           target_relay=relay_name).validate()
            store.save_draft(proj)
        except (ValidationError, OSError) as exc:
            return jerr("Could not create the overlay project: %s." % exc, 500)
        return jsonify(ok=True, name=pname, existing=False)

    def overlay_switch(name, enable):
        """Shared body of activate/deactivate. Order: write the switch, then ask the relay to rebuild its picture; any failure puts the switch back."""
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
        proj, err = inplace_project(name)
        if err:
            return err
        try:
            confirm = body_json().get("confirm_restart") is True
        except ValidationError as exc:
            return jerr(str(exc))
        target = proj.target_relay
        relay, _ = find_relay(target)
        if relay is None:
            return jerr("The stream '%s' no longer exists." % target, 404)
        old = ovl.read_marker(target)
        if enable:
            try:
                pub = store.load_published(name)
            except (ValidationError, OSError, ValueError):
                return jerr("Press Apply first: nothing has been saved as ready yet.", 409)
            if not pub.visible_overlays():
                return jerr("The applied revision has no visible layers: there is nothing to put on air (the stream stays in plain copy mode).", 409)
            gone = missing_media(pub)
            if gone:
                return jerr("Cannot activate: %s." % "; ".join("%s uses '%s', which is not in the Media Library" % g for g in gone[:3]), 422)
            ok_e, why_e = capabilities.encoder_usable(pub.profile.encoder)
            if not ok_e:
                return jerr("Cannot activate: encoder %s is not usable: %s" % (pub.profile.encoder, why_e), 422)
        elif not old or not old["enabled"]:
            return jsonify(ok=True, mode="none", message="Overlays are not on air on '%s'." % target)
        running = relay.get("active") == "yes"
        sw = swap_state(target) if running else {"ready": False}
        if running and not sw["ready"] and not confirm:
            return jerr("'%s' must be restarted once to %s overlays live: its running supervisor predates this update. Its destinations "
                        "reconnect with the same stream key (about 10-20 seconds). Later changes will not need a restart." % (
                            target, "switch" if enable else "remove"), 409, needs_restart=True, kind="unit-restart")
        try:
            ovl.write_marker(target, name, enable)
        except (ValidationError, OSError) as exc:
            return jerr("Could not write the overlay switch: %s." % exc, 500)
        if not running:
            return jsonify(ok=True, mode="armed" if enable else "none",
                           message=("Overlays will be on air the next time '%s' starts." % target) if enable else "Overlays removed.")
        try:
            if sw["ready"]:
                engine_of().relay_swap_source(target)
                mode = "swap"
            else:
                engine_of().relay_restart(target)
                mode = "unit-restart"
        except Exception as exc:
            current_app.logger.warning("overlay %s '%s' failed: %s", "activation" if enable else "removal", target, exc)
            try:                                              # put the switch back: a failed activation must change nothing
                if old:
                    ovl.write_marker(target, old["project"], old["enabled"])
                else:
                    ovl.delete_marker(target)
            except (ValidationError, OSError):
                pass
            return jerr("The stream did not accept the change; the previous state was restored. The stream is untouched.", 500)
        msg = {"swap": "Switching the picture of '%s' now; its destination connections stay up (expect a pause of about 2 seconds)." % target,
               "unit-restart": "Restarting '%s' once; its destinations reconnect with the same stream key." % target}[mode]
        return jsonify(ok=True, mode=mode, message=msg, revision=store.load_published(name).revision)

    @console.route("/studio/<name>/overlay/activate.json", methods=["POST"])
    def studio_overlay_activate(name):
        return overlay_switch(name, True)

    @console.route("/studio/<name>/overlay/deactivate.json", methods=["POST"])
    def studio_overlay_deactivate(name):
        return overlay_switch(name, False)

    # ---- live preview: real playing video ---------------------------------
    try:
        n_slots = max(0, min(8, int(os.environ.get("BLUESTREAM_STUDIO_PREVIEW_SLOTS", "2"))))
    except ValueError:
        n_slots = 2
    play_slots = threading.BoundedSemaphore(max(n_slots, 1)) if n_slots else threading.Semaphore(0)   # a preview must never starve a broadcast

    @console.route("/studio/media/<asset>/play.mp4", methods=["GET"])
    def studio_media_play(asset):
        """Authenticated, on-demand playable video of one Media Library file for the editor canvas.
        Stream copy when the browser can play the file as is, else ONE 540p ultrafast transcode while it is being watched.
        Closing the tab kills the process. Never touches a broadcast or any other process."""
        if not session.get("authenticated"):
            return Response("sign in", status=401)
        if not preview.ASSET_RE.match(asset or "") or media.classify(asset) != "video":
            return Response("not found", status=404)
        rows = library()
        if rows is not None and asset not in {row.get("name") for row in rows if isinstance(row, dict)}:
            # The web account cannot stat the production media directory.  The
            # engine's privileged listing is therefore the safe preflight for
            # a definitely absent asset; an unavailable listing is not treated
            # as absence and the checked proxy bridge remains authoritative.
            return Response("not found", status=404)
        if not play_slots.acquire(blocking=False):
            return Response("Too many previews are open. Close another Studio tab and retry.", status=429)
        proc, startup = None, b""
        cleanup_lock, cleaned = threading.Lock(), [False]

        def cleanup():
            with cleanup_lock:
                if cleaned[0]:
                    return
                cleaned[0] = True
            preview.terminate_proxy(proc)
            play_slots.release()

        try:
            started = preview.open_proxy(asset)           # works where this user can read the file
            if started:
                proc = started[0]
            else:
                try:                                       # hardened VPS: read-only privileged bridge, raw stdout
                    proc = subprocess.Popen(engine_of().proxy_argv(asset), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            close_fds=True, start_new_session=(os.name == "posix"))
                except Exception as exc:                  # EngineError, AttributeError (no bridge), OSError
                    current_app.logger.info("studio play unavailable for '%s': %s", asset, exc)
                    raise preview.proxy_error("unavailable") from exc
            startup = preview.await_proxy_ready(proc)
        except preview.ProxyError as exc:
            cleanup()
            current_app.logger.info("studio play refused for '%s': %s", asset, exc.kind)
            return Response(str(exc), status=exc.http_status)
        except Exception:
            cleanup()
            raise

        def gen():
            deadline = time.monotonic() + 6 * 3600
            try:
                yield startup
                while time.monotonic() < deadline:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:                                        # also runs when the browser disconnects
                cleanup()
        response = Response(stream_with_context(gen()), mimetype="video/mp4",
                            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        response.call_on_close(cleanup)
        return response

    def _hls_urls(kind, name):
        """Public HLS URL of a relay/playlist from the engine's trusted server config (never the Host header)."""
        from .. import app as webapp
        now = time.monotonic()
        if _snap_cache.get("t", -1e9) + 60 < now:
            try:
                _snap_cache.update(t=now, snap=engine_of().call("snapshot"))
            except Exception:
                _snap_cache.update(t=now - 50, snap=None)      # retry soon, never hammer the bridge
        snap = _snap_cache.get("snap")
        if snap is None:
            return None
        return (webapp.relay_hls_url if kind == "relay" else webapp.playlist_hls_url)(snap, name)

    @console.route("/studio/sources.json", methods=["GET"])
    def studio_sources_json():
        """Existing relays and playlists a project can monitor / read from (read-only listing)."""
        bad = guard()
        if bad:
            return bad
        out = {"relays": [], "playlists": []}
        try:
            for r in engine_of().call("relay_list") or []:
                if isinstance(r, dict) and r.get("type") != "studio" and isinstance(r.get("name"), str):
                    out["relays"].append({"name": r["name"], "type": r.get("type") or "", "active": r.get("active") == "yes",
                                          "health": r.get("health") or "", "hls_url": _hls_urls("relay", r["name"])})
            for p in engine_of().call("playlist_list") or []:
                if isinstance(p, dict) and isinstance(p.get("name"), str):
                    out["playlists"].append({"name": p["name"], "active": p.get("active") == "yes", "health": p.get("health") or "",
                                             "count": p.get("item_count") or p.get("items") or "", "hls_url": _hls_urls("playlist", p["name"])})
        except Exception as exc:
            current_app.logger.info("studio sources unavailable: %s", exc)
            return jerr("Streams could not be listed right now.", 503)
        out["relays"].sort(key=lambda r: r["name"])
        out["playlists"].sort(key=lambda r: r["name"])
        return jsonify(ok=True, **out)

    @console.route("/studio/<name>/monitor.json", methods=["GET"])
    def studio_monitor_json(name):
        """What the canvas can PLAY for this project: the source's live HLS (read-only monitoring) and, when the
        Studio stream exists, its own composited output HLS."""
        bad = guard(name=name)
        if bad:
            return bad
        try:
            proj = store.load_draft(name)
        except (ValidationError, OSError, ValueError):
            return jerr("That project file is damaged.", 500)
        src = {"kind": proj.source_kind, "ref": proj.source_ref, "mode": "still", "url": None, "running": None, "message": ""}
        if proj.source_kind == "media":
            src.update(mode="file", url=url_for_play(proj.source_ref))
        elif proj.source_kind in ("relay", "playlist"):
            k = proj.source_kind
            if proj.target_relay:
                src["raw_url"] = None
                try:
                    row = next((r for r in (engine_of().call("relay_list") or []) if isinstance(r, dict) and r.get("name") == proj.target_relay), None)
                    base = os.path.basename((row or {}).get("source") or "")
                    if row and row.get("type") == "local-file" and base and media.classify(base) == "video":
                        have = {m.get("name") for m in (library() or []) if isinstance(m, dict)}
                        if base in have:
                            src["raw_url"] = url_for_play(base)       # the file the stream loops, WITHOUT any overlay burned in
                except Exception:                                    # noqa: BLE001 - the HLS preview still works
                    pass
            rows = []
            try:
                rows = engine_of().call("relay_list" if k == "relay" else "playlist_list") or []
            except Exception:
                rows = []
            row = next((r for r in rows if isinstance(r, dict) and r.get("name") == proj.source_ref), None)
            src.update(mode="hls", url=_hls_urls(k, proj.source_ref), running=None if row is None else row.get("active") == "yes")
            if row is None:
                src["message"] = "That %s no longer exists." % k
            elif not src["running"]:
                src["message"] = "That %s is stopped, so there is no live picture to show." % k
        relay, _ = find_relay(name)
        out = {"exists": False, "running": False, "url": None}
        if relay is not None and relay.get("type") == "studio":
            out = {"exists": True, "running": relay.get("active") == "yes", "url": _hls_urls("relay", name)}
        return jsonify(ok=True, source=src, output=out)

    def url_for_play(asset):
        from flask import url_for
        return url_for("console.studio_media_play", asset=asset)

    @console.route("/studio/playlist/create.json", methods=["POST"])
    def studio_playlist_create():
        """Create an ordered Playlist from Media Library videos with the EXISTING playlist engine."""
        bad = guard(write=True, body=True)
        if bad:
            return bad
        try:
            doc = body_json()
            pname, items = doc.get("name"), doc.get("items")
            if not isinstance(pname, str) or not NAME_RE.match(pname):
                raise ValidationError("choose a playlist name (letters, digits, - and _)")
            if not isinstance(items, list) or not 2 <= len(items) <= 64 or not all(isinstance(i, str) for i in items):
                raise ValidationError("a playlist needs 2 to 64 videos")
            if len(set(items)) != len(items):
                raise ValidationError("the same video cannot be used twice")
            rows = library()
            if rows is not None:
                have = {r.get("name") for r in rows if isinstance(r, dict)}
                missing = [i for i in items if i not in have or media.classify(i) != "video"]
                if missing:
                    raise ValidationError("not a video in the Media Library: %s" % ", ".join(missing[:3]))
            engine_of().playlist_create(pname, items)
        except ValidationError as exc:
            return jerr(str(exc))
        except Exception as exc:
            code = getattr(exc, "code", "") or ""
            current_app.logger.warning("studio playlist create '%s' failed: %s", doc.get("name"), exc)
            return jerr("Playlist creation failed (%s). A playlist with that name may already exist." % (code or "error"), 500)
        return jsonify(ok=True, name=pname, message="Playlist '%s' created. Start it on the Playlists page, then choose it as a source." % pname)

    # ---- workspace preferences and capabilities ---------------------------
    @console.route("/studio/settings.json", methods=["GET"])
    def studio_settings_get():
        bad = guard()
        if bad:
            return bad
        return jsonify(ok=True, settings=settings.load(), defaults=settings.DEFAULTS)

    @console.route("/studio/settings.json", methods=["POST"])
    def studio_settings_post():
        bad = guard(write=True, body=True)
        if bad:
            return bad
        try:
            saved = settings.save(body_json().get("settings"))
        except ValidationError as exc:
            return jerr("Settings not saved: %s." % exc)
        except OSError as exc:
            current_app.logger.warning("studio settings write failed: %s", exc)
            return jerr("Settings could not be written.", 500)
        return jsonify(ok=True, settings=saved, message="Settings saved.")

    @console.route("/studio/capabilities.json", methods=["GET"])
    def studio_capabilities():
        bad = guard()
        if bad:
            return bad
        return jsonify(ok=True, **capabilities.summary(probe=request.args.get("probe") == "1"))


def new_layer(kind):
    """Default values for a layer of `kind` (the workspace's Add Source menu)."""
    return dict(templates_lib.default_overlay(kind).__dict__)
