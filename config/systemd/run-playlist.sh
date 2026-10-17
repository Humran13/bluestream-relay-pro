#!/usr/bin/env bash
# BlueStream Relay Pro - systemd runtime wrapper for a playlist.
#
# Root bootstrap (runs as root): reads the root-only playlist config,
# normalizes every entry into the managed playlist cache (content-addressed,
# unchanged media reused), writes the concat file (listing only the
# prepared baseline artifacts), then re-execs ITSELF as the unprivileged
# bluestream-relay user (setpriv) to run the actual FFmpeg supervision loop
# (lib/supervise.sh). FFmpeg never runs as root. Fails closed if
# preparation fails or the privilege drop cannot be performed.
#
# Playlists are ALWAYS continuous/24-7 channels by design - there is no
# per-playlist loop/finite config field (unlike relays' LOOP=yes|no):
# playlist_build_ffmpeg_args always passes `-stream_loop -1` to the concat
# demuxer. So for supervision purposes every playlist is the "continuous"
# case: any FFmpeg termination, with ANY exit code (including a clean 0),
# is unexpected and is reconnected with bounded backoff - never treated as
# a natural "completion" (there is no finite-playlist concept to complete).
# The one exception, same as relays, is a genuinely deliberate stop
# (`playlist stop` / `systemctl stop`), recognized via the trapped
# SIGTERM/SIGINT itself, never inferred from FFmpeg's exit code - FFmpeg
# exits 0 on both a deliberate stop and a transient concat/decode hiccup,
# which is exactly the ambiguity systemd's Restart=on-failure cannot
# resolve on its own (see lib/supervise.sh for the full rationale; this is
# the same root cause fixed for relays, reused here rather than
# reimplemented).
#
# Installed to /usr/local/lib/bluestream/run-playlist.sh
set -u
shopt -s nullglob

NAME="${1:-}"
case "$NAME" in
    *[!a-z0-9_-]*|""|-*|_*) exit 1 ;;
esac

LIBDIR="/usr/local/lib/bluestream/lib"
RUN_DIR="/var/lib/bluestream/run"

[ -f "$LIBDIR/common.sh" ] || exit 1
# shellcheck source=lib/common.sh
source "$LIBDIR/common.sh" || exit 1
# shellcheck source=lib/probe.sh
source "$LIBDIR/probe.sh" || exit 1
# shellcheck source=lib/playlist.sh
source "$LIBDIR/playlist.sh" || exit 1
# shellcheck source=lib/destinations.sh
source "$LIBDIR/destinations.sh" || exit 1
# shellcheck source=lib/relay.sh
source "$LIBDIR/relay.sh" || exit 1
# shellcheck source=lib/publisher.sh
source "$LIBDIR/publisher.sh" || exit 1
# shellcheck source=lib/supervise.sh
source "$LIBDIR/supervise.sh" || exit 1

bs_detect_install
bs_load_server_conf

if [ "${BLUESTREAM_PRIV_DROPPED:-0}" != "1" ]; then
    # --- Root bootstrap (runs once, before privileges are dropped) --------
    playlist_load_config "$NAME" || exit 1
    [ "${#PLAYLIST_FILES[@]}" -gt 0 ] || exit 1

    # A fresh (re)start clears any stale status from a previous run, so the
    # dashboard never shows a leftover FAILED/STOPPED state while this new
    # attempt is only just starting.
    bs_supervise_clear_status playlist "$NAME"

    # nginx-rtmp (www-data) owns all HLS output; FFmpeg publishes over the
    # private local RTMP socket and never writes the HLS filesystem directly.
    mkdir -p "$RUN_DIR" || exit 1

    # GUI-3A race safety: serialize cache preparation against the manual
    # "Clear Unused Cache" web operation. Both hold a shared flock on
    # $BLUESTREAM_RUN_DIR/.cache.lock, so cleanup can never delete an
    # artifact while this playlist is preparing/normalizing or between
    # concat-write and FFmpeg exec (the flock is released right before the
    # privilege drop below).
    mkdir -p "$BLUESTREAM_RUN_DIR" 2>/dev/null || exit 1
    command -v flock >/dev/null 2>&1 || exit 1
    exec 9>>"$BLUESTREAM_RUN_DIR/.cache.lock" || exit 1
    flock 9 || exit 1

    # Managed playlist normalization cache. Defensive create + ownership fix
    # so an upgraded host that never re-ran the installer still fails
    # safely: the wrapper must be able to write artifacts as root, and the
    # dropped-privilege FFmpeg concat process must be able to read them
    # (root:bluestream-relay 0640, directory root:bluestream-relay 0750).
    mkdir -p "$BLUESTREAM_PLAYLIST_CACHE_DIR" || exit 1
    chown root:"$BLUESTREAM_GROUP" "$BLUESTREAM_PLAYLIST_CACHE_DIR" 2>/dev/null || true
    chmod 0750 "$BLUESTREAM_PLAYLIST_CACHE_DIR" || exit 1

    # Preparation marker: while normalization runs inside this root
    # bootstrap, health reports STARTING instead of STALE (see
    # lib/health.sh). Removed right before the privilege drop, when
    # preparation is complete. This happens ONCE per systemd-level start -
    # an internal reconnect (same playlist, same already-prepared concat
    # file) never re-normalizes media, only the unprivileged continuation's
    # FFmpeg retries.
    : > "$RUN_DIR/$NAME.prepare" || exit 1
    chown root:root "$RUN_DIR/$NAME.prepare" 2>/dev/null || true
    chmod 0600 "$RUN_DIR/$NAME.prepare" || exit 1

    # Prepare every entry into the managed normalization cache (content-
    # addressed, so unchanged media is reused). This is the authoritative
    # media/cache resolution boundary: only validated playlist basenames
    # reach it, and any entry that cannot be decoded/prepared fails the
    # playlist closed - no partial concat is ever written and the unit
    # never claims HEALTHY.
    playlist_cache_prune || true
    playlist_prepare_all || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

    # Write the concat file (readable by bluestream-relay after the drop).
    # It lists ONLY the prepared baseline artifacts.
    playlist_write_concat_file || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

    # Fail closed: privilege drop prerequisites.
    id "$BLUESTREAM_USER" >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }
    command -v setpriv >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }
    command -v ffmpeg >/dev/null 2>&1 || { rm -f "$RUN_DIR/$NAME.prepare"; exit 1; }

    # Phase 1C: (re)start this playlist's attached+enabled outbound
    # publishers. Needs root (writes systemd drop-ins under
    # /etc/systemd/system) and must happen here, before the privilege
    # drop. Runs once per systemd-level start of this unit; internal
    # reconnects below never re-run it. Best-effort: a publisher sync
    # failure must never block the playlist itself from starting.
    publisher_sync_target playlist "$NAME" 2>/dev/null || true

    # Preparation is complete: HLS output begins once FFmpeg publishes.
    # Clear the marker so health transitions to HEALTHY/STARTING as normal.
    rm -f "$RUN_DIR/$NAME.prepare" 2>/dev/null || true

    # Release the GUI-3A cache lock: preparation is done, so a manual cache
    # cleanup may now run. This playlist's artifacts stay protected while
    # it runs because its unit is active and its concat file lists exactly
    # those artifacts.
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true

    # Hand the already-validated name to the unprivileged continuation via
    # the environment, instead of having it re-read the root-only (0600)
    # playlist config file as a non-root user (which would simply fail).
    export BLUESTREAM_PRIV_DROPPED=1
    export BS_PLAYLIST_NAME="$PLAYLIST_NAME"

    exec setpriv --reuid "$BLUESTREAM_USER" --regid "$BLUESTREAM_GROUP" \
        --init-groups --inh-caps=-all --no-new-privs "$0" "$@"
fi

# ---------------------------------------------------------------------------
# Unprivileged continuation - runs as $BLUESTREAM_USER from here on.
# ---------------------------------------------------------------------------
PLAYLIST_NAME="$BS_PLAYLIST_NAME"

# Rebuilt fresh before every FFmpeg attempt (first start AND every
# reconnect) by lib/supervise.sh's bs_supervise_run. The concat file itself
# was already written once, in the root bootstrap above, and does not
# change between reconnects of the SAME systemd-level start; rebuilding the
# argv here is cheap and keeps this symmetric with the relay supervisor.
_playlist_build_for_supervisor() {
    playlist_build_ffmpeg_args
}

# Final umask so HLS segments stay 0640 (group-readable by nginx).
umask 0027

# Playlists are always the continuous/24-7 case (see header comment above):
# finite_ok=0 unconditionally - there is no finite-playlist concept to
# complete, so ANY FFmpeg termination is unexpected and reconnected with
# bounded exponential backoff. A fixed 5s base delay is used (playlists
# have no per-instance configurable restart delay, unlike relays'
# RESTART_SEC).
bs_supervise_run playlist "$PLAYLIST_NAME" 0 5 _playlist_build_for_supervisor
