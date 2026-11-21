#!/usr/bin/env bash
# Container entrypoint: first-run initialisation (per-installation secrets), then hand over to the unit manager.
# Runs as root with a minimal capability set. Creates nothing outside the BlueStream volumes and tmpfs.
set -eu
STATE=/var/lib/bluestream/web
ROOT=/usr/local/lib/bluestream

umask 077
mkdir -p "$STATE" /run/sudo /var/lib/bluestream/unit-logs /var/lib/bluestream/units/enabled /run/bsunitd
chmod 0755 /run/sudo

# 0. /etc/systemd/system is a volume (the publisher writes generated drop-ins there); refresh the pristine unit files from this
#    image on every start so an image upgrade updates the units while generated drop-ins/relay overrides are kept.
cp -f /usr/local/lib/bluestream/docker/units-image/*.service /etc/systemd/system/

# 1. console secret key (never shared between installations)
if [ ! -s "$STATE/secret_key" ]; then
    (cd "$ROOT" && python3 -c 'import sys
from webapp.security import ensure_secret_key
ensure_secret_key(sys.argv[1])' "$STATE")
    echo "bluestream: generated a new console secret key"
fi

# 2. admin account. Password: a Docker secret/file if provided, otherwise generated ONCE and kept in a root-only file
#    (never printed to logs). Read it with:  bluestream.sh admin-password
if [ ! -s "$STATE/admin.json" ]; then
    PWFILE="${BS_ADMIN_PASSWORD_FILE:-}"
    if [ -n "$PWFILE" ] && [ -r "$PWFILE" ]; then
        PW="$(head -n1 "$PWFILE")"
        GENERATED=0
    else
        PW="$(python3 -c 'import secrets,string;a=string.ascii_letters+string.digits;print("".join(secrets.choice(a) for _ in range(22)))')"
        printf '%s\n' "$PW" > "$STATE/initial-admin-password"
        chmod 0600 "$STATE/initial-admin-password"
        GENERATED=1
    fi
    [ -n "$PW" ] || { echo "bluestream: empty admin password" >&2; exit 1; }
    printf '%s\n' "$PW" | (cd "$ROOT" && python3 -m webapp.security init-admin --state-dir "$STATE" --username "${BS_ADMIN_USER:-admin}") >/dev/null
    unset PW
    if [ "$GENERATED" = 1 ]; then
        echo "bluestream: admin account created; the generated one-time password is stored in the container (run: bluestream.sh admin-password)"
    else
        echo "bluestream: admin account created from the provided secret file"
    fi
fi
# permissions exactly as the native installer sets them
chown root:bluestream-web "$STATE" "$STATE/secret_key" "$STATE/admin.json" 2>/dev/null || true
chmod 0750 "$STATE"
chmod 0640 "$STATE/secret_key" "$STATE/admin.json" 2>/dev/null || true
[ -f "$STATE/web.conf" ] && { chown root:bluestream-web "$STATE/web.conf"; chmod 0640 "$STATE/web.conf"; }

# 3. Studio: the vendor PUBLIC key (non-secret) is read from BLUESTREAM_STUDIO_PUBKEY_HEX, which the unit manager passes to the units.

exec python3 "$ROOT/docker/unitd/bsunitd.py"
