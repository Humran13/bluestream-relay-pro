#!/usr/bin/env bash
#
# BlueStream Relay Pro - streaming destination configuration library
#
# Phase 1A foundation: a "destination" is a declarative configuration record
# (name, friendly display name, platform, RTMP/RTMPS server URL, secret
# stream key, enabled flag) that later phases will attach to streams and
# playlists and push to. NO outgoing RTMP worker, systemd unit or process is
# created by this library.
#
# Config model mirrors relays/playlists exactly:
#   directory  /etc/bluestream/destinations            root:root 0700
#   config     /etc/bluestream/destinations/<id>.conf   root:root 0600
#
# Stream keys are secrets: they are written only to the root-only config file
# and are never printed, never echoed, and never returned by list operations.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_DESTINATIONS_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_DESTINATIONS_LOADED=1

DEST_NAME=""; DEST_DISPLAY=""; DEST_PLATFORM=""
DEST_SERVER_URL=""; DEST_STREAM_KEY=""; DEST_ENABLED="no"; DEST_CREATED=""

# Stable internal platform identifiers.  The user always supplies the actual
# RTMP/RTMPS server URL and stream key; the platform value is organisation/
# display/future-behaviour only - never treated as an authoritative endpoint.
# 'tiktok' is a label only: TikTok Live ingest URLs and stream-key access vary
# by account, so no endpoint is ever hard-coded (the user supplies both).
DEST_PLATFORM_ALLOW="youtube facebook twitch rumble instagram tiktok custom"

# Bounds shared with the web layer (Flask mirrors these exactly).
DEST_DISPLAY_MAX=64
DEST_SERVER_URL_MAX=4096
DEST_STREAM_KEY_MAX=256

bs_valid_platform() {
    local p="$1" x
    for x in $DEST_PLATFORM_ALLOW; do
        [ "$p" = "$x" ] && return 0
    done
    return 1
}

# Destination publish URLs accept ONLY rtmp:// and rtmps:// - never http,
# https, file, ftp, ssh, rtsp or local paths.  Values are data only: no
# whitespace/control/shell-hostile characters and a bounded length.
bs_valid_dest_url() {
    local url="$1"
    case "$url" in
        rtmp://*|rtmps://*) ;;
        *) return 1 ;;
    esac
    case "$url" in
        *[[:space:]]*) return 1 ;;
    esac
    if printf '%s' "$url" | grep -qE '[`"\\<>{}[:cntrl:]]'; then
        return 1
    fi
    [ "${#url}" -le "$DEST_SERVER_URL_MAX" ] || return 1
    return 0
}

# Stream keys are opaque secrets: non-empty, bounded length, and no control
# characters (including NUL and newlines).  The value is never interpreted or
# executed; the web bridge delivers it to the engine over stdin and, in a
# later phase, to the outgoing worker by a mechanism chosen then.
bs_valid_stream_key() {
    local key="$1"
    [ -n "$key" ] || return 1
    case "$key" in
        *[[:cntrl:]]*) return 1 ;;
    esac
    [ "${#key}" -le "$DEST_STREAM_KEY_MAX" ] || return 1
    return 0
}

# Friendly display name: printable text only, bounded, no newline/control
# injection (it is stored as a plain key=value config field).
bs_valid_dest_display() {
    local d="$1"
    d="${d#"${d%%[![:space:]]*}"}"
    d="${d%"${d##*[![:space:]]}"}"
    [ -n "$d" ] || return 1
    case "$d" in
        *[[:cntrl:]]*) return 1 ;;
    esac
    [ "${#d}" -le "$DEST_DISPLAY_MAX" ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Config load / save / validate
# ---------------------------------------------------------------------------
dest_load_config() {
    local name="$1"
    DEST_NAME=""; DEST_DISPLAY=""; DEST_PLATFORM=""
    DEST_SERVER_URL=""; DEST_STREAM_KEY=""; DEST_ENABLED="no"; DEST_CREATED=""
    local conf="$BLUESTREAM_DEST_CONF_DIR/$name.conf"
    [ -f "$conf" ] || return 1
    BLUESTREAM_CFG_FILE="$conf"
    BLUESTREAM_CFG_KEYS="NAME DISPLAY PLATFORM SERVER_URL STREAM_KEY ENABLED CREATED"
    BLUESTREAM_CFG_PREFIX="DEST_"
    bs_parse_kv_file
    [ "$DEST_NAME" = "$name" ] || return 1
    case "$DEST_ENABLED" in yes|no) ;; *) return 1 ;; esac
    return 0
}

dest_config_validate() {
    bs_valid_name "$DEST_NAME" || return 1
    bs_valid_dest_display "$DEST_DISPLAY" || return 1
    bs_valid_platform "$DEST_PLATFORM" || return 1
    bs_valid_dest_url "$DEST_SERVER_URL" || return 1
    bs_valid_stream_key "$DEST_STREAM_KEY" || return 1
    case "$DEST_ENABLED" in yes|no) ;; *) return 1 ;; esac
    return 0
}

dest_save_config() {
    local name="$1"
    local tmp="$BLUESTREAM_DEST_CONF_DIR/.$name.conf.tmp.$$"
    local dest="$BLUESTREAM_DEST_CONF_DIR/$name.conf"
    # Fail closed exactly like relay_save_config: any critical write step must
    # succeed or the temp file is removed and nothing is presented as saved.
    if ! {
        printf '# BlueStream Relay Pro destination configuration (root-only; stream key is secret)\n'
        printf 'NAME=%s\n' "$DEST_NAME"
        printf 'DISPLAY=%s\n' "$DEST_DISPLAY"
        printf 'PLATFORM=%s\n' "$DEST_PLATFORM"
        printf 'SERVER_URL=%s\n' "$DEST_SERVER_URL"
        printf 'STREAM_KEY=%s\n' "$DEST_STREAM_KEY"
        printf 'ENABLED=%s\n' "$DEST_ENABLED"
        printf 'CREATED=%s\n' "$DEST_CREATED"
    } > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    chmod 0600 "$tmp" || { rm -f "$tmp"; return 1; }
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$dest" || { rm -f "$tmp"; return 1; }
    chmod 0600 "$dest" || { rm -f "$dest"; return 1; }
    return 0
}

dest_exists() {
    [ -f "$BLUESTREAM_DEST_CONF_DIR/$1.conf" ]
}

dest_list_names() {
    local f
    shopt -s nullglob
    for f in "$BLUESTREAM_DEST_CONF_DIR"/*.conf; do
        basename "$f" .conf
    done
    shopt -u nullglob
}

# Authoritative destination creation (Phase 1A).  Validates every field and
# writes a NEW destination config; the enabled flag is stored as given.  No
# streaming worker or service is started.  Returns a small documented code so
# web-ctl can map failures to controlled messages:
#   0 ok | 1 invalid name | 2 invalid display | 3 unknown platform |
#   4 invalid URL | 5 invalid stream key | 6 invalid enabled |
#   7 already exists | 8 validation backstop | 9 write failure
dest_create() {
    local name="$1" display="$2" platform="$3" url="$4" key="$5" enabled="$6"
    bs_require_root
    bs_valid_name "$name" || return 1
    bs_valid_dest_display "$display" || return 2
    bs_valid_platform "$platform" || return 3
    bs_valid_dest_url "$url" || return 4
    bs_valid_stream_key "$key" || return 5
    case "$enabled" in yes|no) ;; *) return 6 ;; esac
    dest_exists "$name" && return 7
    DEST_NAME="$name"; DEST_DISPLAY="$display"; DEST_PLATFORM="$platform"
    DEST_SERVER_URL="$url"; DEST_STREAM_KEY="$key"; DEST_ENABLED="$enabled"
    DEST_CREATED="$(bs_now_ts)"
    dest_config_validate || return 8
    mkdir -p "$BLUESTREAM_DEST_CONF_DIR" 2>/dev/null || return 9
    dest_save_config "$name" || return 9
    return 0
}

# Enable/disable a destination (state flag only - never starts a worker).
dest_set_enabled() {
    local name="$1" enabled="$2"
    bs_require_root
    bs_valid_name "$name" || return 1
    case "$enabled" in yes|no) ;; *) return 2 ;; esac
    dest_load_config "$name" || return 3
    DEST_ENABLED="$enabled"
    dest_save_config "$name" || return 4
    return 0
}

# Delete ONLY the fixed, validated destination config file.  No generic path
# or wildcard deletion operation exists at any layer.
#
# Phase 1B safety: a destination that is ATTACHED to any stream or playlist is
# NOT deletable here (the authoritative, root-side check).  The caller must
# detach it first; deletion never silently detaches.
#   0 ok | 1 invalid name | 2 not found | 3 attached (detach first) |
#   4 remove failed
dest_delete() {
    local name="$1"
    bs_require_root
    bs_valid_name "$name" || return 1
    dest_exists "$name" || return 2
    [ -z "$(dest_attached_targets "$name")" ] || return 3
    rm -f "$BLUESTREAM_DEST_CONF_DIR/$name.conf" || return 4
    return 0
}

# ---------------------------------------------------------------------------
# Phase 1B: destination <-> stream/playlist ATTACHMENTS.
#
# A "target" is a relay (stream) or a playlist.  Its config file carries at
# most one bounded line:  DESTINATIONS=id1,id2,...  listing destination IDs
# ONLY (never a stream key, secret, or publish URL).  A missing line means no
# destinations are attached.  This library holds the shared attachment helpers
# so relay.sh / playlist.sh and the web bridge share ONE implementation.
#
# Attachment is independent of the destination's global enabled/disabled flag:
# a disabled destination may stay attached and is never auto-detached.
# ---------------------------------------------------------------------------

# Upper bound on destinations attached to a single target.  Bounds every
# privileged argv line and config write; the web form validates the same limit.
BLUESTREAM_MAX_TARGET_DESTINATIONS=32

DEST_ATTACH_CSV=""

# Normalize a comma-separated destination-ID list.  On success sets
# DEST_ATTACH_CSV to the cleaned list (order preserved, no surrounding spaces)
# and returns 0.  Fails closed:
#   1 an ID does not match the engine name rule
#   2 the same ID appears more than once
#   3 an ID does not resolve to an existing destination
#   4 more than BLUESTREAM_MAX_TARGET_DESTINATIONS IDs
bs_dest_attach_normalize() {
    DEST_ATTACH_CSV=""
    local id out="" seen="," n=0
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        bs_valid_name "$id" || return 1
        case "$seen" in *",$id,"*) return 2 ;; esac
        dest_exists "$id" || return 3
        seen="${seen}${id},"
        out="${out:+$out,}$id"
        n=$((n + 1))
        [ "$n" -le "$BLUESTREAM_MAX_TARGET_DESTINATIONS" ] || return 4
    done < <(bs_csv_fields "$1")
    DEST_ATTACH_CSV="$out"
    return 0
}

# Count the IDs in a DESTINATIONS= value (0 for empty/malformed-empty).
bs_dest_attach_count() {
    local id n=0
    while IFS= read -r id; do
        [ -n "$id" ] && n=$((n + 1))
    done < <(bs_csv_fields "$1")
    printf '%d' "$n"
}

# Print the basename of every relay/playlist config whose DESTINATIONS= line
# references destination ID "$1".  Reads the config files directly (no
# dependency on relay.sh / playlist.sh being loaded) so it is safe to call
# from dest_delete in any context.
dest_attached_targets() {
    local id="$1" f line val
    bs_valid_name "$id" || return 0
    shopt -s nullglob
    for f in "$BLUESTREAM_RELAY_CONF_DIR"/*.conf "$BLUESTREAM_PLAYLIST_CONF_DIR"/*.playlist; do
        [ -f "$f" ] || continue
        line="$(grep -m1 '^DESTINATIONS=' "$f" 2>/dev/null || true)"
        [ -n "$line" ] || continue
        val="${line#DESTINATIONS=}"
        val="${val//[[:space:]]/}"
        case ",$val," in
            *",$id,"*) basename "$f" ;;
        esac
    done
    shopt -u nullglob
    return 0
}

