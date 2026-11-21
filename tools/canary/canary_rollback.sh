#!/usr/bin/env bash
# BlueStream Studio - TARGETED canary rollback. DEFAULT IS A DRY RUN (prints the plan, changes nothing).
#
#   sudo bash tools/canary/canary_rollback.sh                                   # show the plan
#   sudo bash tools/canary/canary_rollback.sh --execute --confirm "ROLLBACK BLUESTREAM CANARY"
#   ... --purge-data   (additionally deletes /etc/bluestream, /var/lib/bluestream, /var/www/bluestream - includes stream keys + media)
#
# It stops and removes ONLY BlueStream things: bluestream-* units/drop-ins, the BlueStream sudoers file, the BlueStream
# program files and manager commands. By default it KEEPS all BlueStream data (media, projects, configs) in place.
# It never: restarts or reloads nginx, removes the nginx / libnginx-mod-rtmp / ffmpeg packages, touches Docker, firewall rules,
# SSH, certificates, CloudPanel, websites, RustDesk or WireGuard, and never uses any wildcard that could match other services.
# (The repo's uninstall.sh is NOT used here because it reloads the system nginx.)
set -u

EXECUTE=0; CONFIRM=""; PURGE=0
ROOTFS="${BS_CANARY_ROOT:-}"      # test hook only
SYSTEMCTL="${BS_CANARY_SYSTEMCTL:-systemctl}"
PHRASE="ROLLBACK BLUESTREAM CANARY"
die() { printf 'REFUSING: %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=1; shift ;;
        --confirm) CONFIRM="${2:-}"; shift 2 ;;
        --purge-data) PURGE=1; shift ;;
        -h|--help) sed -n 2,14p "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
if [ "$EXECUTE" = 1 ]; then
    [ "$(id -u)" = "0" ] || die "--execute must run as root"
    [ "$CONFIRM" = "$PHRASE" ] || die "confirmation phrase mismatch; it must be exactly: $PHRASE"
fi

run() { # print; execute only when --execute
    if [ "$EXECUTE" = 1 ]; then printf '  RUN  %s\n' "$*"; "$@" 2>/dev/null || printf '       (skipped or already absent)\n'; else printf '  plan %s\n' "$*"; fi
}
SD="$ROOTFS/etc/systemd/system"
UNITLIST="$($SYSTEMCTL list-units --all --plain --no-legend 'bluestream-*' 2>/dev/null | awk '{print $1}' | grep -E '^bluestream-[A-Za-z0-9@:_.-]+\.(service|timer)$')"

printf 'BlueStream canary rollback (%s)\n' "$([ "$EXECUTE" = 1 ] && echo EXECUTING || echo "DRY RUN - nothing is changed")"
printf '\n1. Stop BlueStream streams and services (exact unit names found on this host; publishers first)\n'
for pat in 'bluestream-publisher@' 'bluestream-browser@' 'bluestream-relay@' 'bluestream-playlist@' 'bluestream-schedule-' 'bluestream-web.service' 'bluestream-nginx.service'; do
    for u in $(printf '%s\n' "$UNITLIST" | grep -F "$pat"); do
        run "$SYSTEMCTL" stop "$u"
        run "$SYSTEMCTL" disable "$u"
    done
done
[ -n "$UNITLIST" ] || printf '  (no bluestream-* units are loaded)\n'

printf '\n2. Remove BlueStream unit files and drop-ins (only files whose names start with bluestream-)\n'
for f in "$SD"/bluestream-relay@.service "$SD"/bluestream-playlist@.service "$SD"/bluestream-playlist-start@.service \
         "$SD"/bluestream-publisher@.service "$SD"/bluestream-browser@.service "$SD"/bluestream-nginx.service "$SD"/bluestream-web.service; do
    [ -e "$f" ] && run rm -f "$f"
done
for d in "$SD"/bluestream-*.service.d; do [ -d "$d" ] && run rm -rf "$d"; done
for t in "$SD"/bluestream-schedule-*.timer; do [ -e "$t" ] && run rm -f "$t"; done
run "$SYSTEMCTL" daemon-reload

printf '\n3. Remove BlueStream programs and the console sudoers entry\n'
for f in "$ROOTFS/etc/sudoers.d/bluestream-web" "$ROOTFS/usr/local/sbin/bluestream-manager" "$ROOTFS/usr/local/bin/bluestream-status"; do
    [ -e "$f" ] && run rm -f "$f"
done
[ -d "$ROOTFS/usr/local/lib/bluestream" ] && run rm -rf "$ROOTFS/usr/local/lib/bluestream"
[ -d "$ROOTFS/etc/bluestream/nginx" ] && run rm -rf "$ROOTFS/etc/bluestream/nginx"
[ -d "$ROOTFS/var/log/bluestream-nginx" ] && run rm -rf "$ROOTFS/var/log/bluestream-nginx"

printf '\n4. Data\n'
if [ "$PURGE" = 1 ]; then
    printf '  --purge-data: removing ALL BlueStream data (this includes stream keys, media and Studio projects)\n'
    for d in "$ROOTFS/etc/bluestream" "$ROOTFS/var/lib/bluestream" "$ROOTFS/var/www/bluestream"; do [ -d "$d" ] && run rm -rf "$d"; done
else
    printf '  KEPT (not removed): /etc/bluestream  /var/lib/bluestream  /var/www/bluestream   (add --purge-data to delete them)\n'
fi

cat <<'EOF'

Deliberately NOT done by this script (do these yourself only if you really want to):
  - apt packages (nginx, ffmpeg, libnginx-mod-rtmp, gunicorn ...) stay installed. libnginx-mod-rtmp added one 'load_module' line at
    /etc/nginx/modules-enabled/50-mod-rtmp.conf; it is harmless and is left alone. Removing it would reload the system nginx again.
  - Full-server rollback = restore the provider snapshot you took before installing.
  - Verify afterwards that your sites, CloudPanel, RustDesk and WireGuard are unaffected (this script never touched them).
EOF
[ "$EXECUTE" = 1 ] || printf '\nDry run complete. To execute: --execute --confirm "%s"\n' "$PHRASE"
