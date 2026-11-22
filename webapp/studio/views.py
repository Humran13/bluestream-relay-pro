"""Studio console views. Registered ONLY when the feature flag is on
(config STUDIO_ENABLED or env BLUESTREAM_STUDIO_ENABLED=1), so a default
install exposes no Studio endpoints at all.

Every mutating route (a) requires the console session, (b) is CSRF-protected by
the app-wide hook, and (c) requires a valid entitlement on the BACKEND.
"""
from datetime import datetime, timezone

from flask import Response, abort, current_app, flash, redirect, render_template, request, send_file, session, url_for

from . import ffmpeg as ffmpeg_mod
from . import api, browsercap, capabilities, entitlement, feedback, preview, store, templates_lib, widgets
from . import settings as ui_settings
from .model import NAME_RE, KINDS, Overlay, Project, ValidationError

SOURCE_KINDS = ("relay", "playlist", "media", "slideshow", "audio")

# Browser-rendered overlays are deliberately NOT enabled in this release: they need a
# sandboxed, egress-controlled, resource-capped browser that has not been built or
# benchmarked. Shown honestly in the UI; use Data overlays instead.
BROWSER_SOURCE_GATE = "Browser source is disabled in this release (sandbox and resource limits not yet validated). Use a Data overlay."


def enabled(app):
    import os
    return bool(app.config.get("STUDIO_ENABLED")) or os.environ.get("BLUESTREAM_STUDIO_ENABLED") == "1"


def _float(form, key, default):
    try:
        return float(form.get(key, default))
    except (TypeError, ValueError):
        return default


def _int(form, key, default):
    try:
        return int(form.get(key, default))
    except (TypeError, ValueError):
        return default


def overlay_from_form(form, i):
    """Build one Overlay from fields suffixed with the index; validation happens in Project."""
    g = lambda k, d="": form.get("%s_%d" % (k, i), d)  # noqa: E731
    return Overlay(
        kind=g("kind"), x=_float(form, "x_%d" % i, 0.0), y=_float(form, "y_%d" % i, 0.0),
        w=_float(form, "w_%d" % i, 0.2), h=_float(form, "h_%d" % i, 0.1),
        opacity=_float(form, "opacity_%d" % i, 1.0), visible=bool(form.get("visible_%d" % i)),
        text=g("text"), asset=g("asset"), color=g("color", "#ffffff"), font=g("font", "DejaVu Sans"),
        size=_int(form, "size_%d" % i, 48), tz=g("tz", "UTC") or "UTC", target=g("target"),
        work_min=_int(form, "work_min_%d" % i, 25), break_min=_int(form, "break_min_%d" % i, 5),
        finished_text=g("finished_text", "LIVE NOW"),
        selector=g("selector"), view_w=_int(form, "view_w_%d" % i, 1280), view_h=_int(form, "view_h_%d" % i, 720),
        allowed_hosts=g("allowed_hosts"),
        url=g("url"), json_path=g("json_path"), refresh_s=_int(form, "refresh_s_%d" % i, 60),
        crop_x=_float(form, "crop_x_%d" % i, 0.0), crop_y=_float(form, "crop_y_%d" % i, 0.0),
        crop_w=_float(form, "crop_w_%d" % i, 1.0), crop_h=_float(form, "crop_h_%d" % i, 1.0),
        value_format=g("value_format", "raw") or "raw")


def preview_items(project, now=None):
    now = now or datetime.now(timezone.utc)
    items = []
    for i, o in enumerate(project.overlays):  # ALL overlays; index == form field index == layer order
        try:
            text = "" if o.kind in ("rect", "image") else widgets.render_text(o, now)
        except ValidationError:
            text = "(invalid time)"
        items.append({"i": i, "o": o, "text": text})
    return items


def plan_hint(project):
    """Pipeline explanation from the project alone (final choice is confirmed with
    ffprobe when the stream starts)."""
    vis = project.visible_overlays()
    if any(o.dynamic for o in vis):
        return ("Live composition", "Changing overlays need one live encode on your VPS "
                "(roughly 1.5-2.5 CPU cores at 720p30).")
    if vis:
        return ("Pre-render, then stream copy", "Static overlays are rendered once and cached; "
                "ongoing CPU is near zero.")
    return ("Direct copy (if compatible)", "No overlays: a compatible source is copied without "
            "re-encoding; otherwise it is normalised once.")


def register(console, find_relay, engine_of):
    def auth():
        return bool(session.get("authenticated"))

    def need_login():
        return redirect(url_for("console.login"))

    def licence_ok():
        v = entitlement.current()
        return v.ok, v

    def guard_write(name=None):
        """Returns a redirect response if the request must not proceed."""
        if not auth():
            return need_login()
        ok, v = licence_ok()
        if not ok:
            flash("Studio is a Premium feature: %s." % v.reason, "error")
            return redirect(url_for("console.studio_home"))
        if name is not None and not NAME_RE.match(name):
            flash("That project name is not valid.", "error")
            return redirect(url_for("console.studio_home"))
        return None

    @console.route("/studio", methods=["GET"])
    def studio_home():
        if not auth():
            return need_login()
        ok, v = licence_ok()
        projects = []
        for n in store.list_projects():
            try:
                p = store.load_draft(n)
                projects.append({"name": n, "overlays": len(p.overlays),
                                 "unpublished": store.has_unpublished_changes(n)})
            except (ValueError, OSError):
                projects.append({"name": n, "overlays": 0, "broken": True})
        return render_template("studio_home.html", projects=projects, licence=v, licence_ok=ok, licence_enforced=entitlement.enforced(),
                               templates=templates_lib.TEMPLATES, source_choices=_source_choices(), source_lists=_source_lists(),
                               browser_gate=BROWSER_SOURCE_GATE)

    def _source_lists():
        out = {"relay": [], "playlist": []}
        try:
            eng = engine_of()
            out["relay"] = sorted(r.get("name") for r in (eng.call("relay_list") or [])
                                  if r.get("type") != "studio" and isinstance(r.get("name"), str))
            out["playlist"] = sorted(p.get("name") for p in (eng.call("playlist_list") or [])
                                     if isinstance(p.get("name"), str))
        except Exception:  # suggestions only
            pass
        return out

    def _source_choices():
        names = []
        try:
            eng = engine_of()
            names += [r.get("name") for r in (eng.call("relay_list") or []) if r.get("type") != "studio"]
            names += [m.get("name") for m in (eng.call("media_list") or [])]
            names += [p.get("name") for p in (eng.call("playlist_list") or [])]
        except Exception:  # suggestions only; the form still accepts typed names
            pass
        return sorted({n for n in names if isinstance(n, str)})

    @console.route("/studio/license", methods=["POST"])
    def studio_license_post():
        if not auth():
            return need_login()
        if not entitlement.enforced():
            flash("No licence is needed: Studio is fully available.", "success")
            return redirect(url_for("console.studio_home"))
        v = entitlement.install_token(request.form.get("token", ""))
        if v.ok:
            flash("Licence activated (%s)." % v.state, "success")
        else:
            flash("Licence rejected: %s." % v.reason, "error")
        return redirect(url_for("console.studio_home"))

    @console.route("/studio/new", methods=["POST"])
    def studio_new_post():
        bad = guard_write()
        if bad:
            return bad
        name = (request.form.get("name") or "").strip().lower()
        kind = request.form.get("source_kind", "")
        ref = (request.form.get("source_ref") or "").strip()
        tpl = request.form.get("template", "blank")
        try:
            if store.exists(name):
                raise ValidationError("a project with that name already exists")
            if kind not in SOURCE_KINDS:
                raise ValidationError("choose a source type")
            project = Project(name, kind, ref, templates_lib.build(tpl),
                              slide_seconds=_int(request.form, "slide_seconds", 10),
                              slide_shuffle=request.form.get("slide_shuffle") == "on",
                              visualizer=request.form.get("visualizer") == "on").validate()
            store.save_draft(project)
        except ValidationError as exc:
            flash("Could not create project: %s." % exc, "error")
            return redirect(url_for("console.studio_home"))
        flash("Project '%s' created. Edit it, then press Apply to Live." % name, "success")
        return redirect(url_for("console.studio_edit", name=name))

    @console.route("/studio/<name>", methods=["GET"])
    def studio_edit(name):
        if not auth():
            return need_login()
        if not NAME_RE.match(name) or not store.exists(name):
            flash("That Studio project was not found.", "error")
            return redirect(url_for("console.studio_home"))
        try:
            project = store.load_draft(name)
        except (ValueError, OSError):
            flash("That project file is damaged; roll back to an earlier revision or delete it.", "error")
            return redirect(url_for("console.studio_home"))
        relay, unavailable = find_relay(name)
        if relay is not None and relay.get("type") != "studio":
            relay = None  # same name, different stream type: not ours
        browser_ok, browser_why = browsercap.usable()
        kinds = [k for k in KINDS if k != "browser" or browser_ok]
        ok, v = licence_ok()
        try:
            published = api.project_to_json(store.load_published(name))
        except (ValueError, OSError):
            published = None
        lists = _source_lists()
        state = {
            "name": name, "project": api.project_to_json(project), "published": published,
            "texts": api.render_texts(project.overlays), "kinds": kinds, "fonts": ffmpeg_mod.available_fonts(),
            "defaults": {k: api.new_layer(k) for k in kinds}, "settings": ui_settings.load(),
            "capabilities": capabilities.summary(project.profile), "sources": lists,
            "stream_exists": relay is not None, "unavailable": bool(unavailable),
            "unpublished": store.has_unpublished_changes(name), "revisions": store.revisions(name),
            "browser_gate": None if browser_ok else browser_why, "can_edit": bool(ok),
            "licence_reason": None if ok else v.reason,
            "urls": {
                "save": url_for("console.studio_save_json", name=name),
                "publish": url_for("console.studio_publish_json", name=name),
                "status": url_for("console.studio_status_json", name=name),
                "takeover": url_for("console.studio_takeover_json", name=name),
                "revert": url_for("console.studio_revert_live", name=name),
                "play": url_for("console.studio_media_play", asset="__A__"),
                "sources": url_for("console.studio_sources_json"),
                "playlistCreate": url_for("console.studio_playlist_create"),
                "capabilities": url_for("console.studio_capabilities"),
                "stream": url_for("console.studio_stream_json", name=name),
                "destinations": url_for("console.studio_destinations_get", name=name),
                "previewText": url_for("console.studio_preview_text", name=name),
                "sourceFrame": url_for("console.studio_source_frame", name=name),
                "assess": url_for("console.studio_assess_json"),
                "settings": url_for("console.studio_settings_get"),
                "media": url_for("console.studio_media_json"),
                "upload": url_for("console.studio_media_upload"),
                "thumb": url_for("console.studio_media_thumb", asset="__A__"),
                "asset": url_for("console.studio_asset", asset="__A__"),
                "rollback": url_for("console.studio_rollback_post", name=name),
                "delete": url_for("console.studio_delete_post", name=name),
                "projects": url_for("console.studio_home"),
                "destinationsPage": url_for("console.relay_destinations", name=name),
                "destinationsList": url_for("console.destinations"),
                "streamsPage": url_for("console.streams"),
            },
        }
        return render_template("studio_edit.html", project=project, state=state)

    @console.route("/studio/<name>/frame.jpg", methods=["GET"])
    def studio_frame(name):
        """Real frame of the selected source for the editor background."""
        if not auth():
            return need_login()
        if not NAME_RE.match(name) or not store.exists(name):
            abort(404)
        data = None
        try:
            project = store.load_draft(name)
            data = preview.frame_jpeg(project)             # works wherever this user can read the source
            if not data:                                   # hardened VPS: ask the read-only bridge instead
                data = engine_of().studio_frame(name)
        except Exception as exc:  # unreadable/damaged/bridge down: neutral background, never an error page
            current_app.logger.info("studio frame unavailable for '%s': %s", name, exc)
            data = None
        if not data:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=10"})

    @console.route("/studio/asset/<asset>", methods=["GET"])
    def studio_asset(asset):
        """Media Library image used by an image overlay (preview only)."""
        if not auth():
            return need_login()
        found = preview.asset_image(asset)
        if found:
            return send_file(found[0], mimetype=found[1], max_age=30)
        if not preview.ASSET_RE.match(asset or ""):
            abort(404)
        try:                                               # hardened VPS: scaled PNG via the read-only bridge
            data = engine_of().studio_thumb(asset)
        except Exception as exc:
            current_app.logger.info("studio thumbnail unavailable for '%s': %s", asset, exc)
            data = None
        if not data:
            abort(404)
        return Response(data, mimetype="image/png", headers={"Cache-Control": "private, max-age=30"})

    @console.route("/studio/<name>/save", methods=["POST"])
    def studio_save_post(name):
        bad = guard_write(name)
        if bad:
            return bad
        try:
            old = store.load_draft(name)
            count = _int(request.form, "count", 0)
            overlays = [overlay_from_form(request.form, i) for i in range(count)
                        if not request.form.get("delete_%d" % i)]
            action = request.form.get("action", "save")
            if action.startswith("add:"):
                overlays.append(templates_lib.default_overlay(action[4:]))
            if action.startswith(("up:", "down:")):
                d, idx = action.split(":")
                i = int(idx)
                j = i - 1 if d == "up" else i + 1
                if 0 <= i < len(overlays) and 0 <= j < len(overlays):
                    overlays[i], overlays[j] = overlays[j], overlays[i]
            if any(o.kind == "browser" for o in overlays):
                ok_b, why_b = browsercap.usable()
                if not ok_b:
                    raise ValidationError("browser source is not available: %s" % why_b)
            old.overlays = overlays
            old.source_ref = (request.form.get("source_ref") or old.source_ref).strip()
            old.slide_seconds = _int(request.form, "slide_seconds", old.slide_seconds)
            old.slide_shuffle = request.form.get("slide_shuffle") == "on"
            if old.source_kind == "audio":
                old.background = (request.form.get("background") or old.background).strip()
                old.visualizer = request.form.get("visualizer") == "on"
            store.save_draft(old.validate())
        except (ValidationError, ValueError, OSError) as exc:
            flash("Not saved: %s." % exc, "error")
            return redirect(url_for("console.studio_edit", name=name))
        flash("Draft saved. The live stream is unchanged until you press Apply to Live.", "success")
        return redirect(url_for("console.studio_edit", name=name))

    @console.route("/studio/<name>/publish", methods=["POST"])
    def studio_publish_post(name):
        bad = guard_write(name)
        if bad:
            return bad
        relay, _ = find_relay(name)
        running = bool(relay and relay.get("type") == "studio" and relay.get("active") == "yes")
        if running and request.form.get("restart") != "1":
            flash("Confirm the controlled restart to apply changes to a running stream.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        engine = engine_of()
        try:
            rev = store.publish(name)
            if running:
                engine.relay_restart(name)
        except Exception as exc:  # EngineError / ValidationError / OSError
            current_app.logger.warning("studio publish '%s' failed: %s", name, exc)
            flash("Publish failed; the previous live revision is still in use.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        flash("Revision %d applied%s." % (rev, "; stream restarted" if running else ""), "success")
        return redirect(url_for("console.studio_edit", name=name))

    @console.route("/studio/<name>/rollback", methods=["POST"])
    def studio_rollback_post(name):
        bad = guard_write(name)
        if bad:
            return bad
        try:
            store.rollback(name, int(request.form.get("revision", "")))
        except (ValidationError, ValueError, OSError):
            flash("That revision is not available.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        flash("Revision loaded into the draft. Press Apply to Live to use it.", "success")
        return redirect(url_for("console.studio_edit", name=name))

    @console.route("/studio/<name>/create-stream", methods=["POST"])
    def studio_create_stream_post(name):
        bad = guard_write(name)
        if bad:
            return bad
        if not store.exists(name) or not _published(name):
            flash("Press Apply to Live first so the project has a published revision.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        try:
            engine_of().relay_create_studio(name, name)
        except Exception as exc:
            current_app.logger.warning("relay_create_studio '%s' failed: %s", name, exc)
            flash("Stream creation failed (a stream with this name may already exist).", "error")
            return redirect(url_for("console.studio_edit", name=name))
        flash("Stream '%s' created. Attach destinations, then press Start." % name, "success")
        return redirect(url_for("console.studio_edit", name=name))

    def _published(name):
        try:
            store.load_published(name)
            return True
        except (ValueError, OSError):
            return False

    @console.route("/studio/<name>/takeover/<dest>", methods=["POST"])
    def studio_takeover_post(name, dest):
        """Controlled switch of ONE destination to this running stream."""
        bad = guard_write(name)
        if bad:
            return bad
        if not NAME_RE.match(dest):
            flash("That destination is not valid.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        if request.form.get("confirm") != "1":
            flash("Confirm the switch: the other stream stops publishing to this destination.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        try:
            engine_of().relay_publisher_takeover(name, dest)
        except Exception as exc:
            current_app.logger.warning("takeover '%s'/'%s' failed: %s", name, dest, exc)
            flash("The switch did not complete; the previous stream was kept or reinstated.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        flash("Destination switched to '%s'. To go back, use Switch on the other stream." % name, "success")
        return redirect(url_for("console.studio_edit", name=name))

    @console.route("/studio/<name>/delete", methods=["POST"])
    def studio_delete_post(name):
        bad = guard_write(name)
        if bad:
            return bad
        relay, _ = find_relay(name)
        if relay is not None and relay.get("type") == "studio":
            flash("Delete the stream on the Streams page first.", "error")
            return redirect(url_for("console.studio_edit", name=name))
        store.delete(name)
        flash("Project '%s' deleted." % name, "success")
        return redirect(url_for("console.studio_home"))

    api.register_api(console, find_relay, engine_of)

    # ---- Help & Feedback (works on the free tier too) --------------------
    @console.route("/help", methods=["GET"])
    def studio_help():
        if not auth():
            return need_login()
        return render_template("studio_help.html", queue=feedback.queue_summary(),
                               endpoint=feedback.endpoint())

    @console.route("/help", methods=["POST"])
    def studio_help_post():
        if not auth():
            return need_login()
        try:
            feedback.submit(kind=request.form.get("kind", "bug"),
                            message=request.form.get("message", ""),
                            consent=request.form.get("consent") == "on",
                            include_diagnostics=request.form.get("diagnostics") == "on",
                            client=request.remote_addr or "?")
        except feedback.FeedbackError as exc:
            flash(str(exc), "error")
            return redirect(url_for("console.studio_help"))
        flash("Thanks - your report was saved to the local queue.", "success")
        return redirect(url_for("console.studio_help"))
