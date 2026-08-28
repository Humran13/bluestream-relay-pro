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
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from webapp.engine import EngineClient, EngineError
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


def create_app(state_dir=None, engine=None, config=None) -> Flask:
    """Application factory.

    ``state_dir`` - local runtime state (admin record + secret key).
    ``engine``    - optional EngineClient/FakeEngine override (tests).
    ``config``    - optional dict merged over default Flask config.
    """
    state_dir = Path(state_dir) if state_dir else default_state_dir()
    # Static assets are served under /console/static so the whole app lives
    # inside the /console prefix (nginx reverse proxy friendly in a later phase).
    app = Flask(__name__, static_url_path="/console/static")
    app.config.from_mapping(
        SECRET_KEY=ensure_secret_key(state_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/console",
        # GUI-1A.2 is local HTTP only; set Secure=True when HTTPS is active.
        SESSION_COOKIE_SECURE=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)

    engine_client = engine if engine is not None else EngineClient(
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
# Console routes (read-only, under /console/)
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
