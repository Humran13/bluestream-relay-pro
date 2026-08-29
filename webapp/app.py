"""BlueStream Relay Pro - local read-only Flask console (GUI-1A.2).

Development-only local entrypoint::

    python -m webapp.app

Binds to 127.0.0.1 only. Debug mode is never enabled. Deployment integration
(nginx, Gunicorn, systemd, sudoers) is deferred to later phases.

Routes (all under ``/console``):
    GET  /console/login   - login form
    POST /console/login   - login (CSRF + rate limited)
    POST /console/logout  - logout (CSRF)
    GET  /console/        - authenticated read-only dashboard
    GET  /console         - redirect to /console/

GUI-1B.1 lifecycle actions (POST only, CSRF + auth required, operation fixed
by the route, target validated server-side, Post/Redirect/Get):
    POST /console/relays/<name>/start|stop|restart
    POST /console/playlists/<name>/start|stop|restart
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp.engine import EngineClient, EngineError, valid_target_name
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
        MAX_CONTENT_LENGTH=16 * 1024,
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
