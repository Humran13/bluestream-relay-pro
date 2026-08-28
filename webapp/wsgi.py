"""Production WSGI entrypoint for the BlueStream web console (Gunicorn).

GUI-1A.3A source of the production application. The GUI-1A.3B installer will
copy the ``webapp`` package to ``/usr/local/lib/bluestream/webapp`` (matching
the systemd unit's WorkingDirectory) and create the state directory with
appropriate ownership before the service starts.

Production mode is explicit and fail-safe:
* the state directory must be provided (never derived from cwd),
* Secure session cookies default to on (override for HTTP-only installs),
* the EngineClient runs the installed ``web-ctl`` through ``sudo -n``.
"""

from webapp.app import PRODUCTION_STATE_DIR, create_app

application = create_app(
    production=True,
    state_dir=PRODUCTION_STATE_DIR,
)
