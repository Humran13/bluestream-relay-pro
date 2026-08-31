"""BlueStream Relay Pro - authenticated web console (GUI-1A.2 / 1B.1 / 1C.1 / 1D.1).

Local development entrypoint::

    python -m webapp.app

Binds to 127.0.0.1 only. Debug mode is never enabled. Production runs behind
nginx/Gunicorn as the unprivileged bluestream-web user.

Routes (all under ``/console``):
    GET  /console/login   - login form
    POST /console/login   - login (CSRF + rate limited)
    POST /console/logout  - logout (CSRF)
    GET  /console/        - authenticated dashboard (overview)
    GET  /console         - redirect to /console/

GUI-1B.1 lifecycle actions (POST only, CSRF + auth required, operation fixed
by the route, target validated server-side, Post/Redirect/Get):
    POST /console/relays/<name>/start|stop|restart
    POST /console/playlists/<name>/start|stop|restart

GUI-1C.1 create-stream / media-library workflows (auth + CSRF on all POSTs):
    GET  /console/streams
    GET/POST /console/streams/create
    GET  /console/media
    GET/POST /console/media/upload
    GET/POST /console/media/<name>/create-stream
    GET  /console/playlists

GUI-1D.1 playlist builder (auth + CSRF on all POSTs, friendly names normalized
at the web boundary, media selected from the Media Library only):
    GET/POST /console/playlists/create
"""

from __future__ import annotations

import os
import re
import sys
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp.engine import (
    ALLOWED_UPLOAD_EXTENSIONS,
    MAX_PLAYLIST_ITEMS,
    EngineClient,
    EngineError,
    valid_media_name,
    valid_source_url,
    valid_target_name,
)
from webapp.security import (
    ADMIN_USERNAME,
    LoginRateLimiter,
    default_state_dir,
    ensure_secret_key,
    generate_csrf_token,
    load_admin_record,
    verify_csrf_token,
    verify_password,
)

# Production web state lives here (created by the installer in GUI-1A.3B).
# It holds only web-console state (admin.json, secret_key, web.conf); engine
# configs stay root-only in /etc/bluestream behind web-ctl.
PRODUCTION_STATE_DIR = "/var/lib/bluestream/web"

# GUI-1C.1 upload staging directory (matches BLUESTREAM_WEB_UPLOAD_DIR in
# lib/common.sh). ONLY this directory is writable by the web user; staged files
# are imported into managed media by the root web-ctl bridge.
PRODUCTION_UPLOAD_DIR = "/var/lib/bluestream/web/upload"

# Default upload cap (matches client_max_body_size in the nginx console
# location). Configurable via create_app(config={"MAX_UPLOAD_SIZE": N}).
DEFAULT_MAX_UPLOAD_SIZE = 1024 * 1024 * 1024  # 1 GiB


# ---------------------------------------------------------------------------
# GUI-1C.1 helpers
# ---------------------------------------------------------------------------

# Characters permitted in the SAFE INTERNAL media basename stem (lowercase
# letters, digits, dot, dash, underscore). Everything else in a human-friendly
# display name is normalized to '-' at the web boundary.
_INTERNAL_MEDIA_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
# Characters permitted in the SAFE INTERNAL stream ID (engine grammar).
_INTERNAL_STREAM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def sanitize_upload_filename(filename) -> str | None:
    """Return the safe, normalized managed-media basename for a browser upload.

    Accepts normal human-friendly names (e.g. ``"5 Minute Timer.mp4"``) and
    deterministically normalizes them to the engine's safe stored form
    (``"5-minute-timer.mp4"``): basename only, lowercase stem, every
    unsupported character becomes ``-`` (spaces/punctuation/unicode), repeated
    ``-`` collapsed, leading/trailing separators trimmed, extension lowercased
    and checked against the allowlist. Returns None (fail closed) on any
    traversal marker, path separator escape, hidden/control character,
    unsupported extension, or a normalization that produces an empty/invalid
    name. The destination is always ``<upload dir>/<result>`` - the browser
    never picks a path.
    """
    if not filename:
        return None
    # Fail closed on any traversal marker anywhere in the submitted name,
    # before stripping directory components (defense in depth).
    if ".." in filename:
        return None
    base = filename.replace("\\", "/").split("/")[-1]
    if not base or base.startswith(".") or ".." in base:
        return None
    # Control characters (0x00-0x1F, 0x7F) are rejected; ordinary spaces are
    # allowed because they are normalized to '-' below.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in base):
        return None
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return None
    stem = "".join(c if c in _INTERNAL_MEDIA_CHARS else "-" for c in stem.lower())
    stem = re.sub(r"-{2,}", "-", stem)
    stem = stem.strip("-_.")
    if not stem:
        return None
    result = stem + ext
    if not valid_media_name(result) or len(result) > 255:
        return None
    return result


def normalize_stream_name(value) -> str | None:
    """Normalize a human-friendly stream name to the engine's safe internal ID.

    e.g. ``"My Promo Stream"`` -> ``"my-promo-stream"``. Deterministic policy:
    trim whitespace, lowercase, every unsupported character (spaces,
    punctuation, unicode) becomes ``-``, repeated ``-`` collapsed, leading and
    trailing ``-``/``_`` trimmed, then the result must pass the authoritative
    engine internal-name validator (``[a-z0-9][a-z0-9_-]{0,47}``). Returns None
    when no safe ID can be derived (empty, only punctuation, traversal marker,
    or too long). The privileged bridge only ever receives this validated ID.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    # Fail closed on traversal markers in the human-entered value.
    if ".." in s:
        return None
    normalized = "".join(c if c in _INTERNAL_STREAM_CHARS else "-" for c in s)
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized.strip("-_")
    if not normalized:
        return None
    if not valid_target_name(normalized):
        return None
    return normalized


def human_size(value) -> str:
    """Human-readable byte size for the media library table."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "?"
    if n < 1024:
        return "%d B" % n
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
    return "?"


TYPE_LABELS = {
    "local-file": "Local File",
    "remote-hls": "Remote HLS",
    "http-file": "HTTP Media",
    "rtmp": "RTMP",
    "rtmps": "RTMPS",
    "rtsp": "RTSP",
}

_CREATE_ERROR_MESSAGES = {
    "INVALID_NAME": "Invalid stream name. Use [a-z0-9_-], start with a letter or digit, max 48 chars.",
    "INVALID_URL": "Unsupported or malformed source URL.",
    "UNSUPPORTED_URL": "Unsupported source URL scheme. Supported: http(s), rtmp(s), rtsp.",
    "MISSING_URL": "A source URL is required.",
    "ALREADY_EXISTS": "A stream with that name already exists.",
    "CREATION_FAILED": "Stream creation failed.",
    "MISSING_MEDIA": "A media file must be selected.",
    "INVALID_MEDIA": "Invalid media file name.",
    "MEDIA_NOT_FOUND": "The selected media file was not found.",
}

_UPLOAD_ERROR_MESSAGES = {
    "MEDIA_EXISTS": "A file with that name already exists in the media library.",
    "NOT_MEDIA": "The uploaded file is not recognized media.",
    "NO_FFPROBE": "Media validation is unavailable on this server.",
    "NOT_REGULAR": "The uploaded file was rejected (not a regular file).",
    "IMPORT_FAILED": "Media import failed. Please try again.",
    "STAGING_NOT_FOUND": "The uploaded file could not be found for import.",
    "INVALID_STAGING": "The upload was rejected.",
}


def _create_error_message(exc: EngineError, fallback: str) -> str:
    if isinstance(exc, EngineError) and exc.code in _CREATE_ERROR_MESSAGES:
        return _CREATE_ERROR_MESSAGES[exc.code]
    return fallback


def _upload_error_message(exc: EngineError) -> str:
    if isinstance(exc, EngineError) and exc.code in _UPLOAD_ERROR_MESSAGES:
        return _UPLOAD_ERROR_MESSAGES[exc.code]
    return "Media import failed. Please try again."


# ---------------------------------------------------------------------------
# GUI-2A: media upload progress. The XHR progress UI posts to the SAME route
# through the same authenticated pipeline (auth, CSRF, filename sanitization,
# staging, empty/size checks, engine media_import_staged). Only the response
# format differs: classic form posts keep the existing flash + Post/Redirect/
# Get behavior unchanged; the progress UI receives the same safe outcome
# message as JSON so it can report success/failure in place.
# ---------------------------------------------------------------------------
def _is_ajax_upload() -> bool:
    """True when the request comes from the GUI-2A XHR progress upload."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _upload_respond(message: str, category: str, target):
    """Return the upload outcome in the format the client expects.

    * Classic multipart form posts: flash the safe message and redirect to
      ``target`` (existing GUI-1C.1 behavior, unchanged).
    * GUI-2A XHR progress uploads: return the same safe message as JSON. The
      success flash is still set so the Media Library page shows the usual
      confirmation after the progress UI navigates there; error flashes are
      intentionally NOT set for XHR (the error is shown in place and the form
      is re-enabled, so no stale flash is left in the session).
    """
    if _is_ajax_upload():
        if category == "success":
            flash(message, "success")
        return jsonify(ok=category == "success", message=message)
    flash(message, category)
    return redirect(target)


# ---------------------------------------------------------------------------
# GUI-1D.1 playlist helpers
# ---------------------------------------------------------------------------
_PLAYLIST_ERROR_MESSAGES = {
    "INVALID_NAME": "Invalid playlist name.",
    "ALREADY_EXISTS": "A playlist with that name already exists.",
    "TOO_FEW_ITEMS": "Select at least two media files.",
    "TOO_MANY_ITEMS": "Too many media files selected.",
    "INVALID_MEDIA": "One or more selected media files are not valid.",
    "MEDIA_NOT_FOUND": "One or more selected media files are no longer available.",
    "DUPLICATE_ITEM": "The same media file cannot be used more than once in a playlist.",
    "CREATION_FAILED": "Playlist creation failed. Please try again.",
    "MISSING_ARGUMENT": "Playlist creation failed. Please try again.",
}


def _playlist_error_message(exc: EngineError, fallback: str) -> str:
    if isinstance(exc, EngineError) and exc.code in _PLAYLIST_ERROR_MESSAGES:
        return _PLAYLIST_ERROR_MESSAGES[exc.code]
    return fallback


def _parse_order(value) -> int | None:
    """Return a positive integer for a submitted order field, else None."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or not s.isdigit():
        return None
    n = int(s)
    return n if n >= 1 else None


def _validate_playlist_items(form) -> tuple:
    """Validate the submitted media selection/order server-side.

    The browser submits one ``items`` checkbox value per selected basename plus
    one integer ``order_<basename>`` field per media row.  This function
    revalidates EVERY basename with the strict media-name rule (so arbitrary
    paths, traversal, absolute paths and Windows-style separators are rejected),
    requires unique orders forming exactly ``1..N`` (deterministic, no silent
    reordering), and returns ``(True, [ordered basenames], None)`` or
    ``(False, None, user-safe error message)``.
    """
    selected = form.getlist("items") or []
    if not isinstance(selected, list):
        selected = []
    seen = set()
    entries = []  # (order, basename)
    for raw in selected:
        item = (raw or "").strip()
        if not valid_media_name(item):
            return False, None, "One or more selected media files are not valid."
        if item in seen:
            return False, None, "The same media file was selected more than once."
        seen.add(item)
        order = _parse_order(form.get("order_" + item))
        if order is None:
            return (
                False,
                None,
                "One or more selected files are missing a valid play order.",
            )
        entries.append((order, item))
    if len(entries) < 2:
        return False, None, "Select at least two media files."
    if len(entries) > MAX_PLAYLIST_ITEMS:
        return False, None, "Select at most %d media files." % MAX_PLAYLIST_ITEMS
    orders = [o for o, _ in entries]
    if len(set(orders)) != len(orders):
        return False, None, "Each selected file needs a unique play order number."
    if sorted(orders) != list(range(1, len(entries) + 1)):
        return (
            False,
            None,
            "Play order numbers must be 1 to %d in sequence." % len(entries),
        )
    entries.sort(key=lambda pair: pair[0])
    return True, [name for _, name in entries], None


def playlist_hls_url(snapshot, name: str) -> str | None:
    """Public playlist HLS URL derived from the engine's trusted server config.

    ``snapshot`` is the root-derived web-ctl snapshot (``domain`` +
    ``https_configured`` read from /etc/bluestream/server.conf); the Host
    header is never trusted.  Mirrors ``bs_playlist_m3u8_url`` in the engine.
    """
    domain = ((snapshot or {}).get("domain") or "").strip()
    if not domain:
        return None
    scheme = "https" if (snapshot or {}).get("https_configured") == "yes" else "http"
    return "%s://%s/hls/playlist/%s/index.m3u8" % (scheme, domain, name)



def _read_secure_cookie_setting(state_dir) -> bool:
    """Read the installer-written secure_cookie deployment setting.

    The installer derives it from BlueStream's own SSL state and writes it to
    the web-side state dir as a non-secret flag. Never trusted from request
    headers. Missing/unreadable config fails SAFE (Secure cookies on).
    """
    try:
        text = (Path(state_dir) / "web.conf").read_text(encoding="utf-8")
    except OSError:
        return True  # fail-safe
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "secure_cookie":
            # Fail-safe: only an explicit "no" disables Secure cookies;
            # anything unrecognized keeps them ON.
            return value.strip().lower() not in ("no", "false", "0", "off")
    return True  # fail-safe


def create_app(state_dir=None, engine=None, config=None, production: bool = False) -> Flask:
    """Application factory.

    ``state_dir`` - runtime state (admin record + secret key). Local mode
        defaults to a temp dir; production mode REQUIRES an explicit path
        (e.g. ``/var/lib/bluestream/web``) and fails closed otherwise.
    ``engine``    - optional EngineClient/FakeEngine override (tests).
    ``config``    - optional dict merged over default Flask config.
    ``production``- enables production-only plumbing: fixed sudo-based
        EngineClient, Werkzeug ProxyFix trusting exactly one nginx hop, and
        Secure session cookies by default (fail-safe; override for HTTP-only).
        Local GUI-1A.2 behavior is unchanged when False.
    """
    if production:
        if state_dir is None:
            raise ValueError(
                "production mode requires an explicit state directory (e.g. %s)"
                % PRODUCTION_STATE_DIR
            )
        state_dir = Path(state_dir)
    else:
        state_dir = Path(state_dir) if state_dir else default_state_dir()
    # Static assets are served under /console/static so the whole app lives
    # inside the /console prefix (nginx reverse proxy friendly in a later phase).
    app = Flask(__name__, static_url_path="/console/static")
    app.config.from_mapping(
        SECRET_KEY=ensure_secret_key(state_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/console",
        # Local GUI-1A.2 is HTTP only. Production defaults to Secure (fail-safe)
        # and then honors the installer-written web.conf deployment setting
        # (derived from BlueStream's own SSL state, never from request headers).
        SESSION_COOKIE_SECURE=production,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        # GUI-1C.1: upload cap. Werkzeug rejects larger request bodies (nginx's
        # console location enforces the same cap first); the upload route also
        # checks Content-Length for a clean error.
        MAX_CONTENT_LENGTH=DEFAULT_MAX_UPLOAD_SIZE,
        MAX_UPLOAD_SIZE=DEFAULT_MAX_UPLOAD_SIZE,
        WEB_UPLOAD_DIR=PRODUCTION_UPLOAD_DIR,
    )
    if production:
        app.config["SESSION_COOKIE_SECURE"] = _read_secure_cookie_setting(state_dir)
    if config:
        # Explicit call-site config wins over web.conf (tests, HTTP-only mode).
        app.config.update(config)

    if engine is not None:
        engine_client = engine
    elif production:
        engine_client = EngineClient(
            production=True,
            timeout=app.config.get("ENGINE_TIMEOUT", 10.0),
        )
    else:
        engine_client = EngineClient(
            timeout=app.config.get("ENGINE_TIMEOUT", 10.0)
        )
    rate_limiter = LoginRateLimiter(
        max_attempts=app.config.get("RATE_LIMIT_MAX_ATTEMPTS", 5),
        window_seconds=app.config.get("RATE_LIMIT_WINDOW_SECONDS", 300.0),
        lockout_seconds=app.config.get("RATE_LIMIT_LOCKOUT_SECONDS", 300.0),
        max_keys=app.config.get("RATE_LIMIT_MAX_KEYS", 500),
    )
    app.extensions["bluestream_engine"] = engine_client
    app.extensions["bluestream_limiter"] = rate_limiter
    app.extensions["bluestream_state_dir"] = state_dir

    if production:
        # Trust exactly ONE nginx proxy hop (Gunicorn listens on loopback only,
        # nginx is the sole proxy). Never trust additional hops or client input.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=0,
            x_prefix=0,
        )

    _register_csrf_protection(app)
    _register_template_context(app)
    _register_security_headers(app)
    _register_error_handlers(app)
    _register_console_routes(app)
    return app


# ---------------------------------------------------------------------------
# CSRF protection for every POST (missing/mismatched token -> HTTP 400)
# ---------------------------------------------------------------------------
def _register_csrf_protection(app: Flask) -> None:
    @app.before_request
    def _enforce_csrf():
        if request.method != "POST":
            return None
        submitted = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        if not verify_csrf_token(session, submitted):
            abort(400)
        return None

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": lambda: generate_csrf_token(session)}


# ---------------------------------------------------------------------------
# Response security headers
# ---------------------------------------------------------------------------
def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def _add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


# ---------------------------------------------------------------------------
# Error pages (never expose traces or internals to browsers)
# ---------------------------------------------------------------------------
def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(400)
    def _bad_request(error):
        return render_template("error.html", code=400, message="Bad request."), 400

    @app.errorhandler(403)
    def _forbidden(error):
        return render_template("error.html", code=403, message="Forbidden."), 403

    @app.errorhandler(404)
    def _not_found(error):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(500)
    def _server_error(error):
        app.logger.error("unhandled console error: %s", error)
        return render_template(
            "error.html", code=500, message="Internal server error."
        ), 500


# ---------------------------------------------------------------------------
# Template context: active top-level navigation section (GUI-2B). Child pages
# map to their parent section so the header highlight stays correct everywhere
# (e.g. the upload page and create-stream pages keep their parent highlighted).
# ---------------------------------------------------------------------------
_NAV_SECTIONS = {
    "console.dashboard": "dashboard",
    "console.streams": "streams",
    "console.streams_create": "streams",
    "console.streams_create_post": "streams",
    "console.media": "media",
    "console.media_upload": "media",
    "console.media_upload_post": "media",
    "console.media_create_stream": "media",
    "console.media_create_stream_post": "media",
    "console.playlists": "playlists",
    "console.playlists_create": "playlists",
    "console.playlists_create_post": "playlists",
}


def _register_template_context(app: Flask) -> None:
    @app.context_processor
    def _inject_active_section():
        return {"active_section": _NAV_SECTIONS.get(request.endpoint or "", "")}


# ---------------------------------------------------------------------------
# GUI-1B.1: controlled lifecycle actions.  Operation is fixed by the route
# (via LIFECYCLE_OPERATIONS); form data can never select an operation.
# ---------------------------------------------------------------------------
LIFECYCLE_OPERATIONS = {
    ("relay", "start"): "relay_start",
    ("relay", "stop"): "relay_stop",
    ("relay", "restart"): "relay_restart",
    ("playlist", "start"): "playlist_start",
    ("playlist", "stop"): "playlist_stop",
    ("playlist", "restart"): "playlist_restart",
}

_PAST_TENSE = {"start": "started", "stop": "stopped", "restart": "restarted"}


def _lifecycle_action(kind: str, action: str, name: str):
    """Run one authenticated lifecycle action and Post/Redirect/Get back.

    * ``kind``/``action`` are compile-time constants from the calling route.
    * The target name is validated here (and again in the engine client)
      before any bridge call.
    * Only the fixed corresponding EngineClient method is invoked; bridge
      details stay server-side in the logs.
    * Every outcome is a redirect to the dashboard (never a mutation response).
    """
    if not session.get("authenticated"):
        return redirect(url_for("console.login"))
    if not valid_target_name(name):
        current_app.logger.warning("lifecycle %s rejected invalid name", kind)
        flash("%s action rejected: invalid name" % kind.title(), "error")
        return redirect(url_for("console.dashboard"))
    engine = current_app.extensions["bluestream_engine"]
    method_name = LIFECYCLE_OPERATIONS.get((kind, action))
    method = getattr(engine, method_name, None) if method_name else None
    if method is None:
        current_app.logger.error(
            "lifecycle %s %s: engine has no %s", kind, action, method_name
        )
        flash("%s '%s' %s failed." % (kind.title(), name, action), "error")
        return redirect(url_for("console.dashboard"))
    try:
        method(name)
    except EngineError as exc:
        current_app.logger.warning(
            "lifecycle %s %s '%s' failed: %s", kind, action, name, exc
        )
        flash("%s '%s' %s failed." % (kind.title(), name, action), "error")
    else:
        flash(
            "%s '%s' %s successfully."
            % (kind.title(), name, _PAST_TENSE.get(action, action)),
            "success",
        )
    return redirect(url_for("console.dashboard"))


# ---------------------------------------------------------------------------
# Console routes (read-only + GUI-1B.1 lifecycle, under /console/)
# ---------------------------------------------------------------------------
def _register_console_routes(app: Flask) -> None:
    console = Blueprint("console", __name__, url_prefix="/console")

    @console.route("", methods=["GET"])
    def index():
        return redirect(url_for("console.dashboard"))

    @console.route("/login", methods=["GET"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("console.dashboard"))
        return render_template("login.html")

    @console.route("/login", methods=["POST"])
    def login_post():
        limiter = current_app.extensions["bluestream_limiter"]
        state_dir = current_app.extensions["bluestream_state_dir"]
        identity = request.remote_addr or "unknown"
        if limiter.is_locked(identity):
            return render_template(
                "login.html", error="Invalid username or password."
            )
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        record = load_admin_record(state_dir)
        valid = bool(
            record
            and username == record.get("username")
            and verify_password(password, record)
        )
        if not valid:
            limiter.record_failure(identity)
            return render_template(
                "login.html", error="Invalid username or password."
            )
        limiter.record_success(identity)
        # Rotate session state on success: drop any pre-login content
        # (including the CSRF token) and store minimal identity only.
        session.clear()
        session["authenticated"] = True
        session["username"] = record.get("username", ADMIN_USERNAME)
        session.permanent = True
        return redirect(url_for("console.dashboard"))

    @console.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("console.login"))

    @console.route("/", methods=["GET"])
    def dashboard():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        engine = current_app.extensions["bluestream_engine"]
        data = {}
        errors = []
        for op in ("snapshot", "relay_list", "playlist_list"):
            try:
                data[op] = engine.call(op)
            except EngineError as exc:
                current_app.logger.warning("engine %s unavailable: %s", op, exc)
                errors.append(op)
        return render_template(
            "dashboard.html",
            data=data,
            engine_unavailable=bool(errors),
        )

    # ------------------------------------------------------------------
    # GUI-1B.1 lifecycle actions: POST only, operation fixed by the route,
    # target validated server-side, CSRF enforced by the global before_request.
    # ------------------------------------------------------------------
    @console.route("/relays/<name>/start", methods=["POST"])
    def relay_start(name):
        return _lifecycle_action("relay", "start", name)

    @console.route("/relays/<name>/stop", methods=["POST"])
    def relay_stop(name):
        return _lifecycle_action("relay", "stop", name)

    @console.route("/relays/<name>/restart", methods=["POST"])
    def relay_restart(name):
        return _lifecycle_action("relay", "restart", name)

    @console.route("/playlists/<name>/start", methods=["POST"])
    def playlist_start(name):
        return _lifecycle_action("playlist", "start", name)

    @console.route("/playlists/<name>/stop", methods=["POST"])
    def playlist_stop(name):
        return _lifecycle_action("playlist", "stop", name)

    @console.route("/playlists/<name>/restart", methods=["POST"])
    def playlist_restart(name):
        return _lifecycle_action("playlist", "restart", name)

    # ------------------------------------------------------------------
    # GUI-1C.1: streams page + create stream from URL (auth + CSRF, PRG).
    # ------------------------------------------------------------------
    @console.route("/streams", methods=["GET"])
    def streams():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        engine = current_app.extensions["bluestream_engine"]
        relays = []
        errors = []
        try:
            relays = engine.call("relay_list") or []
        except EngineError as exc:
            current_app.logger.warning("engine relay_list unavailable: %s", exc)
            errors.append("relay_list")
        return render_template(
            "streams.html",
            relays=relays,
            type_labels=TYPE_LABELS,
            engine_unavailable=bool(errors),
        )

    @console.route("/streams/create", methods=["GET"])
    def streams_create():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        return render_template("streams_create.html")

    @console.route("/streams/create", methods=["POST"])
    def streams_create_post():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        # Human-friendly display names are normalized to the safe internal ID
        # here, at the web boundary; the privileged bridge only ever receives
        # an ID that passes the engine's strict internal-name rule.
        name = normalize_stream_name(request.form.get("name"))
        if name is None:
            flash("We could not create a safe stream name from that value.", "error")
            return redirect(url_for("console.streams_create"))
        url = (request.form.get("url") or "").strip()
        if not valid_source_url(url):
            flash(
                "Unsupported or malformed source URL. Supported: http(s), rtmp(s), rtsp.",
                "error",
            )
            return redirect(url_for("console.streams_create"))
        engine = current_app.extensions["bluestream_engine"]
        try:
            engine.relay_create_url(name, url)
        except EngineError as exc:
            current_app.logger.warning("relay_create_url '%s' failed: %s", name, exc)
            if exc.code == "ALREADY_EXISTS":
                flash("A stream named '%s' already exists." % name, "error")
            else:
                flash(_create_error_message(exc, "Stream creation failed."), "error")
            return redirect(url_for("console.streams_create"))
        flash(
            "Stream '%s' created. It is stopped - press Start to begin." % name,
            "success",
        )
        return redirect(url_for("console.streams"))

    # ------------------------------------------------------------------
    # GUI-1C.1: media library + upload (auth + CSRF, PRG; size capped).
    # ------------------------------------------------------------------
    @console.route("/media", methods=["GET"])
    def media():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        engine = current_app.extensions["bluestream_engine"]
        items = []
        errors = []
        try:
            items = engine.call("media_list") or []
        except EngineError as exc:
            current_app.logger.warning("engine media_list unavailable: %s", exc)
            errors.append("media_list")
        return render_template(
            "media.html",
            media=items,
            human_size=human_size,
            engine_unavailable=bool(errors),
        )

    @console.route("/media/upload", methods=["GET"])
    def media_upload():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        return render_template(
            "media_upload.html",
            max_upload=current_app.config.get("MAX_UPLOAD_SIZE", DEFAULT_MAX_UPLOAD_SIZE),
            human_size=human_size,
        )

    @console.route("/media/upload", methods=["POST"])
    def media_upload_post():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        max_size = current_app.config.get("MAX_UPLOAD_SIZE", DEFAULT_MAX_UPLOAD_SIZE)
        if (request.content_length or 0) > max_size:
            return _upload_respond(
                "File exceeds the upload size limit (%s)." % human_size(max_size),
                "error",
                url_for("console.media_upload"),
            )
        upload = request.files.get("media")
        if upload is None or not upload.filename:
            return _upload_respond(
                "No file selected.", "error", url_for("console.media_upload")
            )
        safe = sanitize_upload_filename(upload.filename)
        if safe is None:
            return _upload_respond(
                "Unsupported or unsafe file name. Allowed: .mp4 .mkv .mov .webm .m4v .ts.",
                "error",
                url_for("console.media_upload"),
            )
        upload_dir = Path(current_app.config.get("WEB_UPLOAD_DIR", PRODUCTION_UPLOAD_DIR))
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        staged = upload_dir / safe
        if staged.exists():
            return _upload_respond(
                "A file with that name is already being processed.",
                "error",
                url_for("console.media_upload"),
            )
        try:
            upload.save(str(staged))
        except OSError as exc:
            current_app.logger.warning("upload save failed: %s", exc)
            return _upload_respond(
                "Upload could not be stored. Please try again.",
                "error",
                url_for("console.media_upload"),
            )
        try:
            if staged.stat().st_size == 0:
                staged.unlink()
                return _upload_respond(
                    "The uploaded file is empty.",
                    "error",
                    url_for("console.media_upload"),
                )
        except OSError:
            return _upload_respond(
                "Upload could not be stored. Please try again.",
                "error",
                url_for("console.media_upload"),
            )
        engine = current_app.extensions["bluestream_engine"]
        try:
            engine.media_import_staged(safe)
        except EngineError as exc:
            current_app.logger.warning("media_import_staged '%s' failed: %s", safe, exc)
            if exc.code == "MEDIA_EXISTS":
                message = "A media file named '%s' already exists." % safe
            else:
                message = _upload_error_message(exc)
            return _upload_respond(message, "error", url_for("console.media_upload"))
        return _upload_respond(
            "Media '%s' added to the library." % safe,
            "success",
            url_for("console.media"),
        )

    # ------------------------------------------------------------------
    # GUI-1C.1: create a local-file stream from an uploaded media item.
    # ------------------------------------------------------------------
    @console.route("/media/<name>/create-stream", methods=["GET"])
    def media_create_stream(name):
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        if not valid_media_name(name):
            flash("Invalid media selection.", "error")
            return redirect(url_for("console.media"))
        engine = current_app.extensions["bluestream_engine"]
        try:
            items = engine.call("media_list") or []
        except EngineError as exc:
            current_app.logger.warning("engine media_list unavailable: %s", exc)
            flash("Media library is temporarily unavailable.", "error")
            return redirect(url_for("console.media"))
        if not any(item.get("name") == name for item in items):
            flash("The selected media file was not found.", "error")
            return redirect(url_for("console.media"))
        return render_template("media_create_stream.html", media=name)

    @console.route("/media/<name>/create-stream", methods=["POST"])
    def media_create_stream_post(name):
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        if not valid_media_name(name):
            flash("Invalid media selection.", "error")
            return redirect(url_for("console.media"))
        # Same friendly-name normalization as the URL create-stream form; only
        # the safe internal ID crosses the privileged boundary.
        stream_name = normalize_stream_name(request.form.get("name"))
        if stream_name is None:
            flash("We could not create a safe stream name from that value.", "error")
            return redirect(url_for("console.media_create_stream", name=name))
        engine = current_app.extensions["bluestream_engine"]
        try:
            engine.relay_create_media(stream_name, name)
        except EngineError as exc:
            current_app.logger.warning("relay_create_media '%s' failed: %s", stream_name, exc)
            if exc.code == "ALREADY_EXISTS":
                flash("A stream named '%s' already exists." % stream_name, "error")
            else:
                flash(_create_error_message(exc, "Stream creation failed."), "error")
            return redirect(url_for("console.media_create_stream", name=name))
        flash(
            "Stream '%s' created from media. It is stopped - press Start to begin." % stream_name,
            "success",
        )
        return redirect(url_for("console.streams"))

    @console.route("/playlists", methods=["GET"])
    def playlists():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        engine = current_app.extensions["bluestream_engine"]
        items = []
        errors = []
        snapshot = {}
        try:
            snapshot = engine.call("snapshot") or {}
        except EngineError as exc:
            current_app.logger.warning("engine snapshot unavailable: %s", exc)
            errors.append("snapshot")
        try:
            items = engine.call("playlist_list") or []
        except EngineError as exc:
            current_app.logger.warning("engine playlist_list unavailable: %s", exc)
            errors.append("playlist_list")
        # GUI-1D.1: public HLS URL derived from the engine's trusted server
        # config (never from the Host header).  Rendered as a copyable value.
        for item in items:
            if isinstance(item, dict):
                item["hls_url"] = playlist_hls_url(snapshot, item.get("name") or "")
        return render_template(
            "playlists.html",
            playlists=items,
            engine_unavailable=bool(errors),
        )

    # ------------------------------------------------------------------
    # GUI-1D.1: playlist builder (ordered Media Library files only).
    # ------------------------------------------------------------------
    @console.route("/playlists/create", methods=["GET"])
    def playlists_create():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        engine = current_app.extensions["bluestream_engine"]
        media = []
        errors = []
        try:
            media = engine.call("media_list") or []
        except EngineError as exc:
            current_app.logger.warning("engine media_list unavailable: %s", exc)
            errors.append("media_list")
        return render_template(
            "playlists_create.html",
            media=media,
            max_playlist_items=MAX_PLAYLIST_ITEMS,
            engine_unavailable=bool(errors),
        )

    @console.route("/playlists/create", methods=["POST"])
    def playlists_create_post():
        if not session.get("authenticated"):
            return redirect(url_for("console.login"))
        # Friendly display names are normalized to the safe internal playlist ID
        # here, at the web boundary; the privileged bridge only ever receives an
        # ID that passes the engine's strict internal-name rule.
        name = normalize_stream_name(request.form.get("name"))
        if name is None:
            flash("We could not create a safe playlist name from that value.", "error")
            return redirect(url_for("console.playlists_create"))
        ok, ordered, error = _validate_playlist_items(request.form)
        if not ok:
            flash(error, "error")
            return redirect(url_for("console.playlists_create"))
        engine = current_app.extensions["bluestream_engine"]
        try:
            engine.playlist_create(name, ordered)
        except EngineError as exc:
            current_app.logger.warning("playlist_create '%s' failed: %s", name, exc)
            if exc.code == "ALREADY_EXISTS":
                flash("A playlist named '%s' already exists." % name, "error")
            else:
                flash(_playlist_error_message(exc, "Playlist creation failed."), "error")
            return redirect(url_for("console.playlists_create"))
        flash(
            "Playlist '%s' created. It is stopped - press Start to begin." % name,
            "success",
        )
        return redirect(url_for("console.playlists"))

    app.register_blueprint(console)


# ---------------------------------------------------------------------------
# Local development entrypoint (127.0.0.1 only, debug always off)
# ---------------------------------------------------------------------------
def _entrypoint() -> None:
    state_dir = Path(default_state_dir())
    app = create_app(state_dir=state_dir)
    if load_admin_record(state_dir) is None:
        sys.stderr.write(
            "No admin account yet in %s.\n" % state_dir
        )
        sys.stderr.write(
            "Create one with:  echo 'your-password' | python -m webapp.security init-admin\n"
        )
    print("BlueStream console: http://127.0.0.1:5000/console/")
    print("State directory: %s" % state_dir)
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    _entrypoint()
