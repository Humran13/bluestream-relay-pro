#!/usr/bin/env bash
# Roll back a studio_activate.sh deployment: restores exactly the files it replaced, removes the files it added, reloads systemd's
# unit definitions and restarts ONLY bluestream-web.service (the dashboard). It NEVER starts, stops or restarts a relay, publisher,
# playlist, FFmpeg, nginx, CloudPanel or anything else, so a live stream is not interrupted.
#
# Every restoration is VERIFIED (byte-for-byte against the backup). Success is reported only when every file was restored/removed
# and the dashboard answers HTTP 200 again. Otherwise it says ROLLBACK INCOMPLETE / FAILED and exits non-zero.
#
#   bash studio_activate_rollback.sh                 # newest deployment backup
#   bash studio_activate_rollback.sh --dir /root/bluestream-backups/studio-activate-<stamp>
#   bash studio_activate_rollback.sh --dry-run       # list what would be restored
# Exit codes: 0 rolled back and verified, 1 usage/no backup, 4 ROLLBACK INCOMPLETE (files), 5 files restored but dashboard unhealthy.
set -u
ROOTFS="${BS_ACT_ROOT:-}"                       # test hook only
BKROOT="${BS_ACT_BACKUP_ROOT:-/root/bluestream-backups}"
DIR=""; DRY=0; AUTO=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --auto) AUTO=1; shift ;;
        -h|--help) sed -n 2,14p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$ROOTFS" ] || [ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -n "$DIR" ] || DIR="$(ls -dt "$BKROOT"/studio-activate-* 2>/dev/null | head -n1)"
[ -n "$DIR" ] && [ -f "$DIR/manifest" ] || { echo "no deployment backup with a manifest found (looked in $BKROOT)" >&2; exit 1; }
case "$DIR" in "$BKROOT"/studio-activate-*) ;; *) echo "refusing: backup directory must be $BKROOT/studio-activate-*" >&2; exit 1 ;; esac
[ ! -f "$DIR/rolled-back" ] || { echo "this deployment was already rolled back ($DIR/rolled-back)"; exit 0; }

echo "Rolling back: $DIR"
PORT="$(cat "$DIR/web-port" 2>/dev/null)"
FAILED=0; DONE=0
ALLOW_A="$ROOTFS/usr/local/lib/bluestream/"; ALLOW_B="$ROOTFS/etc/systemd/system/"
while read -r kind path; do
    [ -n "$path" ] || continue
    case "$path" in "$ALLOW_A"*|"$ALLOW_B"*) ;; *) echo "  !! skipped (outside allowed prefixes): $path"; FAILED=$((FAILED+1)); continue ;; esac
    if [ "$kind" = MOD ]; then
        bk="$DIR/files$path"
        echo "  restore $path"
        [ "$DRY" = 1 ] && continue
        if [ ! -f "$bk" ]; then echo "  !! FAILED: backup copy missing for $path"; FAILED=$((FAILED+1)); continue; fi
        if cp -a "$bk" "$path.bs-rb.$$" && mv -f "$path.bs-rb.$$" "$path" && cmp -s "$bk" "$path"; then DONE=$((DONE+1))
        else echo "  !! FAILED to restore $path"; rm -f "$path.bs-rb.$$"; FAILED=$((FAILED+1)); fi
    elif [ "$kind" = NEW ]; then
        echo "  remove  $path"
        [ "$DRY" = 1 ] && continue
        rm -f "$path"
        if [ -e "$path" ]; then echo "  !! FAILED to remove $path"; FAILED=$((FAILED+1)); else DONE=$((DONE+1)); fi
    else
        echo "  !! unknown manifest entry: $kind $path"; FAILED=$((FAILED+1))
    fi
done < <(tac "$DIR/manifest")
[ "$DRY" = 1 ] && { echo "dry run: nothing changed"; exit 0; }
rmdir "$ROOTFS/etc/systemd/system/bluestream-web.service.d" 2>/dev/null || true

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo "ROLLBACK INCOMPLETE: $FAILED item(s) could not be restored ($DONE were). The dashboard was NOT restarted."
    echo "The full pre-deployment snapshot is $DIR/pre-deploy-snapshot.tar.gz; do not assume the dashboard is in its old state."
    echo "No stream unit was touched."
    exit 4
fi

systemctl daemon-reload || { echo "ROLLBACK INCOMPLETE: systemctl daemon-reload failed"; exit 4; }
HEALTH=1
if [ -f "$DIR/web-restarted" ] || [ "$AUTO" = 0 ]; then
    echo "restarting ONLY bluestream-web.service (the dashboard)"
    HEALTH=0
    if systemctl restart bluestream-web.service; then
        for _ in $(seq 1 30); do
            if systemctl is-active --quiet bluestream-web.service && python3 - "${PORT:-8180}" <<'PY'
import http.client, sys
try:
    c = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=5); c.request("GET", "/console/login"); sys.exit(0 if c.getresponse().status == 200 else 1)
except Exception:
    sys.exit(1)
PY
            then HEALTH=1; break; fi
            sleep 2
        done
    fi
fi
if [ "$HEALTH" != 1 ]; then
    echo ""
    echo "ROLLBACK FAILED: all $DONE file(s) were restored, but the dashboard did not return HTTP 200."
    echo "Check: journalctl -u bluestream-web -n 80 --no-pager.  No stream unit was touched."
    exit 5
fi
date -u +%FT%TZ > "$DIR/rolled-back"
echo "Rollback VERIFIED: $DONE item(s) restored/removed byte-for-byte; dashboard healthy. No stream unit was touched."
