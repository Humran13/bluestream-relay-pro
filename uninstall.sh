#!/usr/bin/env bash
#
# BlueStream Relay Pro - uninstaller.
#
# Safe by default:
#   - creates a configuration backup first (unless --no-backup)
#   - asks twice for confirmation (second: type the project name)
#   - only deletes data with --purge or explicit confirmation
#   - never removes nginx/ffmpeg system packages
#
# Usage:
#   sudo bash uninstall.sh          # keep data
#   sudo bash uninstall.sh --purge  # also remove config, media and HLS data
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
set -u

PURGE=0
NO_BACKUP=0
for _a in "$@"; do
    case "$_a" in
        --purge) PURGE=1 ;;
        --no-backup) NO_BACKUP=1 ;;
        --help|-h)
            sed -n '1,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
done
unset _a

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/lib/common.sh" ]; then
    BS_ROOT="$SCRIPT_DIR"
elif [ -d /usr/local/lib/bluestream ] && [ -f /usr/local/lib/bluestream/lib/common.sh ]; then
    BS_ROOT="/usr/local/lib/bluestream"
else
    printf 'Cannot locate BlueStream Relay Pro libraries.\n' >&2
    exit 1
fi

for _bs_lib in common osdetect backup webconsole; do
    # shellcheck source=lib/common.sh
    source "$BS_ROOT/lib/$_bs_lib.sh" || { printf 'Failed to load library %s\n' "$_bs_lib" >&2; exit 1; }
done
unset _bs_lib
bs_setup

printf '%s\n' \
    "==============================================" \
    "   BlueStream Relay Pro v$(bs_version) - uninstaller" \
    "=============================================="

[ "$(id -u)" -eq 0 ] || { printf 'uninstall.sh must run as root (sudo).\n' >&2; exit 1; }

if [ "$NO_BACKUP" = "0" ] && [ -d /etc/bluestream ]; then
    printf 'Creating a configuration backup before uninstalling...\n'
    backup_configs no || printf 'Warning: backup failed; continuing.\n'
fi

printf '\n%s\n' "You are about to uninstall BlueStream Relay Pro."
bs_confirm "Continue with uninstall?" || { printf 'Uninstall cancelled.\n'; exit 0; }
printf 'Type the project name exactly to confirm: '
read -r answer || exit 1
if [ "$answer" != "bluestream" ]; then
    printf 'Confirmation failed. Uninstall cancelled.\n'
    exit 1
fi

# --- stop and disable all instances ---
printf 'Stopping and disabling relays/playlists...\n'
systemctl stop 'bluestream-relay@*' 'bluestream-playlist@*' 2>/dev/null || true
for u in $(systemctl list-unit-files --plain --no-legend 'bluestream-relay@*.service' 'bluestream-playlist@*.service' 2>/dev/null | awk '{print $1}'); do
    systemctl disable "$u" 2>/dev/null || true
done

# --- remove systemd units and drop-ins ---
rm -f /etc/systemd/system/bluestream-relay@.service
rm -f /etc/systemd/system/bluestream-playlist@.service
rm -rf /etc/systemd/system/bluestream-relay@*.service.d
rm -rf /etc/systemd/system/bluestream-playlist@*.service.d
systemctl daemon-reload 2>/dev/null || true

# --- remove the web console service, sudoers policy and nginx snippet ---
web_uninstall

# --- remove installed project files ---
rm -f /usr/local/sbin/bluestream-manager
rm -f /usr/local/bin/bluestream-status
rm -rf /usr/local/lib/bluestream

# --- remove nginx site config and RTMP include (keep nginx itself) ---
rm -f /etc/nginx/sites-available/bluestream
rm -f /etc/nginx/sites-enabled/bluestream
rm -f /etc/nginx/modules-enabled/60-bluestream-rtmp.conf
rm -rf /etc/nginx/bluestream
if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
    systemctl reload nginx 2>/dev/null || true
fi

# --- data removal (optional) ---
if [ "$PURGE" = "1" ]; then
    rm -rf /etc/bluestream /var/lib/bluestream /var/www/bluestream
    printf 'BlueStream configuration, media and HLS data removed.\n'
else
    if bs_confirm "Remove all BlueStream data too (/etc/bluestream, /var/lib/bluestream, /var/www/bluestream)?"; then
        rm -rf /etc/bluestream /var/lib/bluestream /var/www/bluestream
        printf 'BlueStream data removed.\n'
    else
        printf 'Data retained:\n'
        printf '  - %s\n' /etc/bluestream /var/lib/bluestream /var/www/bluestream
    fi
fi

printf '%s\n' \
    "" \
    "BlueStream Relay Pro has been uninstalled." \
    "" \
    "System packages (nginx, ffmpeg, certbot, libnginx-mod-rtmp) were NOT removed." \
    "Remove them yourself if no longer needed, for example:" \
    "  sudo apt-get purge nginx ffmpeg certbot libnginx-mod-rtmp" \
    ""

exit 0
