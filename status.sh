#!/usr/bin/env bash
#
# BlueStream Relay Pro - quick status.
#
# Installed as /usr/local/bin/bluestream-status by install.sh.
# Run with sudo for full relay/playlist detail (configs are root-only).
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/lib/common.sh" ]; then
    BS_ROOT="$SCRIPT_DIR"
elif [ -d /usr/local/lib/bluestream ] && [ -f /usr/local/lib/bluestream/lib/common.sh ]; then
    BS_ROOT="/usr/local/lib/bluestream"
else
    printf 'Cannot locate BlueStream Relay Pro libraries.\n' >&2
    exit 1
fi

for _bs_lib in common osdetect health relay playlist media; do
    # shellcheck source=lib/common.sh
    source "$BS_ROOT/lib/$_bs_lib.sh" || exit 1
done
unset _bs_lib
bs_setup

printf 'BlueStream Relay Pro v%s\n' "$(bs_version)"
printf '  OS        : %s %s\n' "$OS_NAME" "$OS_VERSION"
printf '  Nginx     : %s\n' "$(systemctl is-active nginx 2>/dev/null || printf 'unknown')"
printf '  FFmpeg    : %s\n' "$(ffmpeg -version 2>/dev/null | head -n1 | cut -c1-60 || printf 'not found')"
printf '  Domain    : %s\n' "${BLUESTREAM_DOMAIN:-(not set)}"
printf '  SSL       : %s\n' "$BLUESTREAM_SSL_ENABLED"
printf '  Firewall  : %s\n' "$BLUESTREAM_FIREWALL"

if [ -r "$BLUESTREAM_RELAY_CONF_DIR" ]; then
    printf '  Relays    : %s\n' "$(relay_list_names | grep -c . || true)"
    printf '  Playlists : %s\n' "$(playlist_list_names | grep -c . || true)"
    printf '  Media     : %s files\n' "$(media_list_names | grep -c . || true)"

    show_one_line() {
        local kind="$1" name="$2"
        if [ "$kind" = "relay" ]; then
            relay_load_config "$name" 2>/dev/null || return 0
            health_state relay "$name"
            printf '  relay %-22s %-10s %s\n' "$name" "$(systemctl is-active "$(bs_unit relay "$name")" 2>/dev/null)" "$HEALTH_STATE"
        else
            playlist_load_config "$name" 2>/dev/null || return 0
            health_state playlist "$name"
            printf '  playlist %-19s %-10s %s\n' "$name" "$(systemctl is-active "$(bs_unit playlist "$name")" 2>/dev/null)" "$HEALTH_STATE"
        fi
    }

    for name in $(relay_list_names); do
        show_one_line relay "$name"
    done
    for name in $(playlist_list_names); do
        show_one_line playlist "$name"
    done
else
    printf '  Relays    : (run with sudo to read configuration)\n'
fi

exit 0
