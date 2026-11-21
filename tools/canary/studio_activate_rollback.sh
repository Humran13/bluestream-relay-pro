#!/usr/bin/env bash
# Roll back a studio_activate.sh deployment: restores exactly the files it replaced, removes the files it added, reloads systemd's
# unit definitions and restarts ONLY bluestream-web.service (the dashboard). It NEVER starts, stops or restarts a relay, publisher,
# playlist, FFmpeg, nginx, CloudPanel or anything else, so a live stream is not interrupted.
#
#   bash studio_activate_rollback.sh                 # newest deployment backup
#   bash studio_activate_rollback.sh --dir /root/bluestream-backups/studio-activate-<stamp>
#   bash studio_activate_rollback.sh --dry-run       # list what would be restored
# Also invoked automatically by studio_activate.sh when a deployment or its health checks fail.
set -u
BKROOT="${BS_ACT_BACKUP_ROOT:-/root/bluestream-backups}"
DIR=""; DRY=0; AUTO=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --auto) AUTO=1; shift ;;
        -h|--help) sed -n 2,10p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -n "$DIR" ] || DIR="$(ls -dt "$BKROOT"/studio-activate-* 2>/dev/null | head -n1)"
[ -n "$DIR" ] && [ -f "$DIR/manifest" ] || { echo "no deployment backup with a manifest found (looked in $BKROOT)" >&2; exit 1; }
case "$DIR" in "$BKROOT"/studio-activate-*) ;; *) echo "refusing: backup directory must be $BKROOT/studio-activate-*" >&2; exit 1 ;; esac
[ ! -f "$DIR/rolled-back" ] || { echo "this deployment was already rolled back ($DIR/rolled-back)"; exit 0; }

echo "Rolling back: $DIR"
PORT="$(cat "$DIR/web-port" 2>/dev/null)"
n=0
# reverse order so later changes are undone first
tac "$DIR/manifest" | while read -r kind path; do
    [ -n "$path" ] || continue
    case "$path" in /usr/local/lib/bluestream/*|/etc/systemd/system/*) ;; *) echo "skip (outside allowed prefixes): $path"; continue ;; esac
    if [ "$kind" = MOD ]; then
        echo "  restore $path"
        [ "$DRY" = 1 ] && continue
        cp -a "$DIR/files${path}" "$path.bs-rb.$$" && mv -f "$path.bs-rb.$$" "$path" || echo "  !! could not restore $path"
    elif [ "$kind" = NEW ]; then
        echo "  remove  $path"
        [ "$DRY" = 1 ] && continue
        rm -f "$path"
    fi
done
[ "$DRY" = 1 ] && { echo "dry run: nothing changed"; exit 0; }
rmdir /etc/systemd/system/bluestream-web.service.d 2>/dev/null || true

systemctl daemon-reload || echo "!! daemon-reload failed"
if [ -f "$DIR/web-restarted" ] || [ "$AUTO" = 0 ]; then
    echo "restarting ONLY bluestream-web.service (the dashboard)"
    systemctl restart bluestream-web.service || echo "!! restart failed: journalctl -u bluestream-web -n 50 --no-pager"
    ok=0
    for _ in $(seq 1 30); do
        if systemctl is-active --quiet bluestream-web.service; then
            code="$(python3 - "${PORT:-8180}" <<'PY'
import http.client, sys
try:
    c = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=5); c.request("GET", "/console/login"); print(c.getresponse().status)
except Exception:
    print(0)
PY
)"
            [ "$code" = 200 ] && { ok=1; break; }
        fi
        sleep 2
    done
    [ "$ok" = 1 ] && echo "dashboard is healthy again" || echo "!! dashboard did not return to HTTP 200; check: journalctl -u bluestream-web -n 80 --no-pager"
fi
date -u +%FT%TZ > "$DIR/rolled-back"
echo "Rollback finished. No stream unit was touched."
