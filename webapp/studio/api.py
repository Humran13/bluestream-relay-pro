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
import time
from datetime import datetime, timezone

from flask import Response, current_app, jsonify, request, session

from . import browsercap, capabilities, entitlement, media, preview, settings, store, templates_lib, widgets
from .model import KINDS, NAME_RE, OutputProfile, Overlay, Project, ValidationError

SOURCE_KINDS = ("relay", "playlist", "media", "slideshow", "audio")
MAX_JSON_BYTES = 512 * 1024
_OVERLAY_TYPES = {f.name: type(f.default) for f in dataclasses.fields(Overlay) if f.name != "kind"}
_PROFILE_FIELDS = ("width", "height", "fps", "video_kbps", "audio_kbps", "preset", "scaler")
_PROFILE_TYPES = {f.name: type(f.default) for f in dataclasses.fields(OutputProfile)}
_PROJECT_KEYS = {"source_kind", "source_ref", "slide_seconds", "slide_shuffle", "background", "visualizer",
                 "profile", "overlays"}
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
    proj = Project(
        old.name, kind,
        _coerce(doc.get("source_ref", old.source_ref), str, "source_ref").strip(),
        [overlay_from_json(o) for o in overlays],
        profile_from_json(doc["profile"], old.profile) if "profile" in doc else old.profile,
        revision=old.revision,
        slide_seconds=_coerce(doc.get("slide_seconds", old.slide_seconds), int, "slide_seconds"),
        slide_shuffle=_coerce(doc.get("slide_shuffle", old.slide_shuffle), bool, "slide_shuffle"),
        background=_coerce(doc.get("background", old.background), str, "background").strip(),
        visualizer=_coerce(doc.get("visualizer", old.visualizer), bool, "visualizer"))
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


def register_api(console, find_relay, engine_of):
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
        relay, _ = find_relay(name)
        running = bool(relay and relay.get("type") == "studio" and relay.get("active") == "yes")
        if running and not restart:
            return jerr("Confirm the controlled restart to apply changes to a running stream.", 409)
        try:
            gone = missing_media(store.load_draft(name))
        except (ValidationError, OSError, ValueError):
            return jerr("That project file is damaged; roll back to an earlier revision.", 500)
        if gone:
            return jerr("Cannot apply: %s." % "; ".join("%s uses '%s', which is not in the Media Library" % g
                                                        for g in gone[:4]), 422,
                        missing=[{"where": w, "name": n} for w, n in gone])
        try:
            rev = store.publish(name)
            if running:
                engine_of().relay_restart(name)
        except Exception as exc:
            current_app.logger.warning("studio publish '%s' failed: %s", name, exc)
            return jerr("Publish failed; the previous live revision is still in use.", 500)
        return jsonify(ok=True, revision=rev, restarted=running, unpublished=False,
                       message="Revision %d applied%s." % (rev, "; stream restarted" if running else ""))

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
    def stream_doc(name):
        relay, unavailable = find_relay(name)
        if relay is not None and relay.get("type") != "studio":
            relay = None
        doc = {"exists": relay is not None, "unavailable": bool(unavailable), "active": False, "health": "",
               "health_detail": "", "state": "", "detail": "", "speed": "", "fps": "", "cpu_cores": "",
               "restarts": "", "dropped": "", "rss_mb": "", "last_failure": "", "publishers": [], "destinations_attached": 0}
        if relay is None:
            return doc
        doc.update(active=relay.get("active") == "yes", health=relay.get("health") or "",
                   health_detail=relay.get("health_detail") or "")
        eng = engine_of()
        try:
            h = eng.read("studio_health", name) or {}
            doc.update(state=h.get("state") or "", detail=h.get("detail") or "", speed=h.get("speed") or "",
                       fps=h.get("fps") or "", cpu_cores=h.get("cpu_cores") or "", restarts=h.get("restarts") or "0",
                       dropped=h.get("dropped_frames") or "0", rss_mb=h.get("rss_mb") or "",
                       last_failure=h.get("last_failure") or "")
        except Exception:
            pass
        try:
            pubs = eng.read("relay_publisher_status", name) or []
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
        return jsonify(ok=True, stream=stream_doc(name), unpublished=store.has_unpublished_changes(name),
                       published_revision=published, capacity=capabilities.assess(draft.profile),
                       load=capabilities.load_avg())

    @console.route("/studio/<name>/stream.json", methods=["POST"])
    def studio_stream_json(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
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
        relay, _ = find_relay(name)
        if relay is not None and relay.get("type") == "studio":
            try:
                attached = list(engine_of().relay_destinations_get(name) or [])
            except Exception:
                attached = []
        dests = [{"name": r.get("name"), "display_name": r.get("display_name") or r.get("name"),
                  "platform": r.get("platform") or "", "server_url": r.get("server_url") or "",
                  "enabled": r.get("enabled") == "yes", "has_stream_key": r.get("has_stream_key") == "yes"}
                 for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]
        return jsonify(ok=True, destinations=dests, attached=attached, stream_exists=relay is not None
                       and relay.get("type") == "studio")

    @console.route("/studio/<name>/destinations.json", methods=["POST"])
    def studio_destinations_post(name):
        bad = guard(write=True, name=name, body=True)
        if bad:
            return bad
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
        return jsonify(ok=True, **capabilities.summary())


def new_layer(kind):
    """Default values for a layer of `kind` (the workspace's Add Source menu)."""
    return dict(templates_lib.default_overlay(kind).__dict__)
