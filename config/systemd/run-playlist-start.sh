#!/usr/bin/env bash
# BlueStream Relay Pro - one-time scheduled playlist start wrapper (GUI-8A).
#
# Invoked ONLY by bluestream-playlist-start@<name>.service, which is triggered
# ONLY by a generated bluestream-schedule-<name>.timer at the scheduled
# instant. Behaviour at trigger time:
#   - playlist config missing   -> clean up the schedule and exit 0 (fail safe)
#   - scheduled instant is more than BLUESTREAM_SCHEDULE_GRACE seconds in the
#     past (a pathological very-late fire) -> do NOT start; drop the spent
#     timer but KEEP the sidecar so the UI shows a "missed" schedule; exit 0
#   - playlist already running   -> exit 0 (never launch a duplicate)
#   - otherwise                  -> run the authoritative playlist_start, then
#     remove this schedule's timer unit + sidecar (one-shot)
#
# The generated timer is Persistent=false, so a start missed while the machine
# was offline is never run late on the next boot; this grace check is only
# defence-in-depth. No catch-up broadcast is ever launched.
#
# Installed to /usr/local/lib/bluestream/run-playlist-start.sh
set -u

NAME="${1:-}"
case "$NAME" in
    *[!a-z0-9_-]*|""|-*|_*) exit 1 ;;
esac

LIBDIR="/usr/local/lib/bluestream/lib"
[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/health.sh
source "$LIBDIR/health.sh" || exit 1
# shellcheck source=lib/probe.sh
source "$LIBDIR/probe.sh" || exit 1
# shellcheck source=lib/playlist.sh
source "$LIBDIR/playlist.sh" || exit 1
# shellcheck source=lib/schedule.sh
source "$LIBDIR/schedule.sh" || exit 1

bs_detect_install
bs_load_server_conf

# Playlist gone: remove the now-orphaned schedule and exit cleanly.
if ! playlist_exists "$NAME"; then
    schedule_clear "$NAME" >/dev/null 2>&1 || true
    exit 0
fi

# Missed-time guard: if the fire arrives well after the scheduled instant, do
# NOT start the playlist. Keep the sidecar as "missed" evidence for the UI;
# only the spent timer unit is removed. A missed schedule never runs late.
if schedule_load "$NAME" 2>/dev/null; then
    _now="$(date +%s)"
    if [ $(( _now - SCHEDULE_START_AT )) -gt "$BLUESTREAM_SCHEDULE_GRACE" ]; then
        schedule_expire_timer "$NAME" >/dev/null 2>&1 || true
        exit 0
    fi
fi

unit="$(bs_unit playlist "$NAME")"
state="$(systemctl is-active "$unit" 2>/dev/null)"
if [ "$state" = "active" ] || [ "$state" = "activating" ]; then
    # Already running (or starting) - a schedule must never launch a duplicate.
    schedule_clear "$NAME" >/dev/null 2>&1 || true
    exit 0
fi

# Reuse the ONE authoritative start path (same as the web console / CLI).
rc=0
playlist_start "$NAME" || rc=$?

# One-shot: remove this schedule's timer unit + sidecar regardless of outcome
# (a failed start is surfaced through the playlist's own health, not by
# re-firing the timer).
schedule_clear "$NAME" >/dev/null 2>&1 || true

exit "$rc"
