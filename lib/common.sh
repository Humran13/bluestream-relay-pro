#!/usr/bin/env bash
#
# BlueStream Relay Pro - common library
#
# Core helpers: paths, validation, logging, safe key=value configuration
# parsing, credential redaction, privilege handling, systemd wrappers and
# HLS freshness checks.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
#
# This file is safe to source from scripts that may run as root. It never
# sources untrusted files and never evaluates configuration values.

if [ -n "${BLUESTREAM_COMMON_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_COMMON_LOADED=1

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BLUESTREAM_ETC_DIR="/etc/bluestream"
BLUESTREAM_RELAY_CONF_DIR="${BLUESTREAM_ETC_DIR}/relays"
BLUESTREAM_PLAYLIST_CONF_DIR="${BLUESTREAM_ETC_DIR}/playlists"
BLUESTREAM_SERVER_CONF="${BLUESTREAM_ETC_DIR}/server.conf"

BLUESTREAM_VAR_DIR="/var/lib/bluestream"
BLUESTREAM_MEDIA_DIR="${BLUESTREAM_VAR_DIR}/media"
BLUESTREAM_RUN_DIR="${BLUESTREAM_VAR_DIR}/run"
BLUESTREAM_BACKUP_DIR="${BLUESTREAM_VAR_DIR}/backups"

BLUESTREAM_WWW_DIR="/var/www/bluestream"
BLUESTREAM_HLS_ROOT="${BLUESTREAM_WWW_DIR}/hls"
BLUESTREAM_HLS_RELAY_DIR="${BLUESTREAM_HLS_ROOT}/relay"
BLUESTREAM_HLS_PLAYLIST_DIR="${BLUESTREAM_HLS_ROOT}/playlist"
BLUESTREAM_WEB_DIR="${BLUESTREAM_WWW_DIR}/web"

BLUESTREAM_USER="bluestream-relay"
BLUESTREAM_GROUP="bluestream-relay"
BLUESTREAM_NGINX_USER="www-data"

BLUESTREAM_SERVICE_RELAY_PREFIX="bluestream-relay@"
BLUESTREAM_SERVICE_PLAYLIST_PREFIX="bluestream-playlist@"

BLUESTREAM_INSTALL_ROOT="/usr/local/lib/bluestream"
BLUESTREAM_INSTALLED=0

# ---------------------------------------------------------------------------
# Colours (disabled when not a TTY)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_RED="$(printf '\033[31m')"
    C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[36m')"
else
    C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""
fi

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
bs_log()   { printf '%s\n' "$*"; }
bs_info()  { printf '%s[*]%s %s\n' "${C_CYAN}"  "${C_RESET}" "$*"; }
bs_ok()    { printf '%s[OK]%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; }
bs_warn()  { printf '%s[WARN]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
bs_error() { printf '%s[ERR]%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; }
bs_step()  { printf '%s==>%s %s\n' "${C_BOLD}" "${C_RESET}" "$*"; }

bs_die() {
    bs_error "$1"
    exit "${2:-1}"
}

# ---------------------------------------------------------------------------
# Install root detection
# ---------------------------------------------------------------------------
bs_detect_install() {
    local self
    self="${BASH_SOURCE[0]:-}"
    if [ -n "$self" ] && [ -f "$(dirname "$self")/../VERSION" ]; then
        BLUESTREAM_INSTALL_ROOT="$(cd "$(dirname "$self")/.." && pwd)"
    elif [ -d /usr/local/lib/bluestream ] && [ -f /usr/local/lib/bluestream/VERSION ]; then
        BLUESTREAM_INSTALL_ROOT="/usr/local/lib/bluestream"
    else
        BLUESTREAM_INSTALL_ROOT="/usr/local/lib/bluestream"
    fi
    if [ -f "$BLUESTREAM_INSTALL_ROOT/lib/common.sh" ]; then
        BLUESTREAM_INSTALLED=1
    else
        BLUESTREAM_INSTALLED=0
    fi
}

bs_version() {
    local f="$BLUESTREAM_INSTALL_ROOT/VERSION"
    if [ -r "$f" ]; then
        tr -d '[:space:]' < "$f"
    else
        printf '%s' '0.1.0'
    fi
}

# ---------------------------------------------------------------------------
# Server configuration (safe key=value parsing, whitelist only)
# ---------------------------------------------------------------------------
bs_load_server_conf() {
    BLUESTREAM_DOMAIN=""
    BLUESTREAM_ADMIN_EMAIL=""
    BLUESTREAM_SSL_ENABLED="no"
    BLUESTREAM_FIREWALL="none"
    BLUESTREAM_NGINX_USER="www-data"

    [ -r "$BLUESTREAM_SERVER_CONF" ] || return 0
    local key val
    while IFS='=' read -r key val || [ -n "$key" ]; do
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        case "$key" in
            ''|\#*) continue ;;
        esac
        case "$key" in
            DOMAIN)        if bs_valid_domain "$val"; then BLUESTREAM_DOMAIN="$val"; fi ;;
            ADMIN_EMAIL)   if bs_valid_email "$val"; then BLUESTREAM_ADMIN_EMAIL="$val"; fi ;;
            SSL_ENABLED)   case "$val" in yes|no) BLUESTREAM_SSL_ENABLED="$val" ;; esac ;;
            FIREWALL)      case "$val" in none|ufw) BLUESTREAM_FIREWALL="$val" ;; esac ;;
            NGINX_USER)    case "$val" in [a-z][a-z0-9_.-]*) BLUESTREAM_NGINX_USER="$val" ;; esac ;;
        esac
    done < "$BLUESTREAM_SERVER_CONF"
    return 0
}

bs_save_server_conf() {
    bs_require_root
    local tmp="$BLUESTREAM_ETC_DIR/.server.conf.tmp.$$"
    {
        printf '# BlueStream Relay Pro server configuration (root-only)\n'
        printf 'DOMAIN=%s\n' "$BLUESTREAM_DOMAIN"
        printf 'ADMIN_EMAIL=%s\n' "$BLUESTREAM_ADMIN_EMAIL"
        printf 'SSL_ENABLED=%s\n' "$BLUESTREAM_SSL_ENABLED"
        printf 'FIREWALL=%s\n' "$BLUESTREAM_FIREWALL"
        printf 'NGINX_USER=%s\n' "$BLUESTREAM_NGINX_USER"
    } > "$tmp"
    chmod 0600 "$tmp"
    chown root:root "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$BLUESTREAM_SERVER_CONF"
    chmod 0600 "$BLUESTREAM_SERVER_CONF"
}

bs_setup() {
    if [ -n "${BLUESTREAM_SETUP_DONE:-}" ]; then return 0; fi
    bs_detect_install
    bs_load_server_conf
    BLUESTREAM_SETUP_DONE=1
    return 0
}

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
# Names: lowercase letters, digits, '-' and '_', must start alphanumeric.
bs_valid_name() {
    local n="$1"
    case "$n" in
        '')            return 1 ;;
        *[!a-z0-9_-]*) return 1 ;;
        -*|_*)         return 1 ;;
    esac
    [ "${#n}" -le 48 ] || return 1
    return 0
}

bs_valid_type() {
    case "$1" in
        local-file|remote-hls|rtmp|rtmps|rtsp|http-file) return 0 ;;
        *) return 1 ;;
    esac
}

# URLs: only the supported schemes. Rejects whitespace, control characters
# and shell-hostile characters. Values are never evaluated by the shell, but
# we still refuse obviously dangerous input defensively.
bs_valid_url() {
    local url="$1"
    case "$url" in
        http://*|https://*|rtmp://*|rtmps://*|rtsp://*) ;;
        *) return 1 ;;
    esac
    case "$url" in
        *[[:space:]]*) return 1 ;;
    esac
    if printf '%s' "$url" | grep -qE '[`"\\<>{}[:cntrl:]]'; then
        return 1
    fi
    [ "${#url}" -le 4096 ] || return 1
    return 0
}

# Managed media file names: safe subset only, no separators, no "..",
# no hidden files. Prevents path traversal.
bs_valid_media_name() {
    local f="$1"
    case "$f" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
        .*|*'..'*)             return 1 ;;
    esac
    [ "${#f}" -le 255 ] || return 1
    return 0
}

bs_valid_restart_sec() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 300 ] 2>/dev/null
}

bs_valid_domain() {
    local d="$1"
    case "$d" in
        ''|*[!A-Za-z0-9.-]*) return 1 ;;
        *'..'*|.*|*.)        return 1 ;;
    esac
    [ "${#d}" -le 253 ] || return 1
    return 0
}

bs_valid_email() {
    case "$1" in
        ''|*[[:space:]]*) return 1 ;;
        *@*)              return 0 ;;
        *)                return 1 ;;
    esac
}

# Absolute path validation: must start with '/', no control characters,
# no ".." traversal components.
bs_valid_abs_path() {
    local p="$1"
    case "$p" in
        /*) ;;
        *) return 1 ;;
    esac
    case "$p" in
        *[[:cntrl:]]*|*/../*|*/..|'..'*) return 1 ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------
# Credential redaction for display/logging
# ---------------------------------------------------------------------------
bs_redact_url() {
    local url="$1"
    local scheme="" rest="" path="" qs="" out="" pair="" key="" val="" sep=""
    # Not a URL (e.g. a local file path): return unchanged.
    case "$url" in
        *://*) ;;
        *) printf '%s' "$url"; return ;;
    esac
    scheme="${url%%://*}"
    rest="${url#*://}"
    # Strip any embedded userinfo credentials entirely.
    if [[ "$rest" == *@* ]]; then
        rest="${rest#*@}"
    fi
    path="$rest"
    qs=""
    if [[ "$rest" == *\?* ]]; then
        path="${rest%%\?*}"
        qs="${rest#*\?}"
    fi
    out="${scheme}://${path}"
    if [ -n "$qs" ]; then
        out="${out}?"
        sep=""
        IFS='&' read -r -a qparts <<< "$qs"
        for pair in "${qparts[@]}"; do
            key="${pair%%=*}"
            val="${pair#*=}"
            case "$key" in
                token|key|secret|sig|signature|password|pass|pwd|apikey|api_key|auth|access_token|token_id|st|x-amz-signature|x-amz-credential|x-goog-signature|X-Amz-Signature|X-Amz-Credential)
                    val="***"
                    ;;
            esac
            out="${out}${sep}${key}=${val}"
            sep="&"
        done
    fi
    printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# Root / command checks
# ---------------------------------------------------------------------------
bs_require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        bs_die "This operation requires root. Run with: sudo bluestream-manager" 2
    fi
}

bs_require_cmd() {
    command -v "$1" >/dev/null 2>&1 || bs_die "Required command not found: $1"
}

bs_have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# systemd helpers
# ---------------------------------------------------------------------------
bs_unit() {
    local kind="$1" name="$2"
    if [ "$kind" = "relay" ]; then
        printf '%s%s.service' "$BLUESTREAM_SERVICE_RELAY_PREFIX" "$name"
    else
        printf '%s%s.service' "$BLUESTREAM_SERVICE_PLAYLIST_PREFIX" "$name"
    fi
}

bs_unit_active() {
    [ "$(systemctl is-active "$1" 2>/dev/null)" = "active" ]
}

bs_unit_enabled() {
    [ "$(systemctl is-enabled "$1" 2>/dev/null)" = "enabled" ]
}

# ---------------------------------------------------------------------------
# HLS freshness
# ---------------------------------------------------------------------------
bs_hls_mtime() {
    local dir="$1"
    if [ -f "$dir/index.m3u8" ]; then
        stat -c '%Y' "$dir/index.m3u8"
    else
        printf '0'
    fi
}

bs_hls_age_seconds() {
    local dir="$1" now m
    now="$(date +%s)"
    m="$(bs_hls_mtime "$dir")"
    if [ "$m" -eq 0 ] 2>/dev/null; then
        printf '999999'
    else
        printf '%s' "$((now - m))"
    fi
}

bs_hls_segment_count() {
    local dir="$1"
    if [ -r "$dir/index.m3u8" ]; then
        grep -c '\.ts$' "$dir/index.m3u8" 2>/dev/null || printf '0'
    else
        printf '0'
    fi
}

bs_hls_is_fresh() {
    local dir="$1" stale_after="${2:-30}" age
    [ -f "$dir/index.m3u8" ] || return 1
    age="$(bs_hls_age_seconds "$dir")"
    [ "$age" -le "$stale_after" ] || return 1
    [ "$(bs_hls_segment_count "$dir")" -ge 1 ] || return 1
    return 0
}

# ---------------------------------------------------------------------------
# HLS output directories
# ---------------------------------------------------------------------------
bs_hls_dir_for() {
    local kind="$1" name="$2"
    if [ "$kind" = "relay" ]; then
        printf '%s/%s' "$BLUESTREAM_HLS_RELAY_DIR" "$name"
    else
        printf '%s/%s' "$BLUESTREAM_HLS_PLAYLIST_DIR" "$name"
    fi
}

bs_ensure_hls_dir() {
    local kind="$1" name="$2" dir
    dir="$(bs_hls_dir_for "$kind" "$name")"
    mkdir -p "$dir" 2>/dev/null || return 1
    chown "${BLUESTREAM_USER}:${BLUESTREAM_NGINX_USER}" "$dir" 2>/dev/null || return 1
    chmod 0750 "$dir" 2>/dev/null || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Privilege drop (used by systemd runtime wrappers). Fails closed.
# ---------------------------------------------------------------------------
bs_can_drop_to() {
    local user="$1"
    id "$user" >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1
}

bs_drop_and_exec() {
    local user="$1" group=""
    shift
    bs_can_drop_to "$user" || return 1
    # Resolve the account's real primary group; never assume user name == group
    # name. Fail closed if it cannot be resolved.
    group="$(id -gn "$user" 2>/dev/null)" || return 1
    [ -n "$group" ] || return 1
    exec setpriv --reuid "$user" --regid "$group" \
        --init-groups --inh-caps=-all --no-new-privs "$@"
}

# ---------------------------------------------------------------------------
# Public URLs
# ---------------------------------------------------------------------------
bs_public_base() {
    if [ -n "${BLUESTREAM_DOMAIN:-}" ]; then
        if [ "$BLUESTREAM_SSL_ENABLED" = "yes" ]; then
            printf 'https://%s' "$BLUESTREAM_DOMAIN"
        else
            printf 'http://%s' "$BLUESTREAM_DOMAIN"
        fi
    else
        printf 'http://SERVER_IP_OR_DOMAIN'
    fi
}

bs_relay_m3u8_url()      { printf '%s/hls/relay/%s/index.m3u8'    "$(bs_public_base)" "$1"; }
bs_playlist_m3u8_url()   { printf '%s/hls/playlist/%s/index.m3u8' "$(bs_public_base)" "$1"; }
bs_relay_player_url()    { printf '%s/player/?relay=%s'           "$(bs_public_base)" "$1"; }
bs_playlist_player_url() { printf '%s/player/?playlist=%s'        "$(bs_public_base)" "$1"; }

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------
# Set BS_ASSUME_YES=1 to auto-answer confirmations (used by install.sh).
BS_ASSUME_YES="${BS_ASSUME_YES:-0}"

bs_confirm() {
    local prompt="$1" ans
    if [ "$BS_ASSUME_YES" = "1" ]; then
        return 0
    fi
    printf '%s [y/N]: ' "$prompt"
    read -r ans || return 1
    case "$ans" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

bs_prompt() {
    local var="$1" prompt="$2" def="${3:-}" val
    # Non-interactive safety: if stdin is not a TTY, use the default when one
    # is available, otherwise fail rather than hang on read.
    if [ ! -t 0 ]; then
        if [ -n "$def" ]; then
            printf -v "$var" '%s' "$def"
            return 0
        fi
        return 1
    fi
    if [ -n "$def" ]; then
        printf '%s [%s]: ' "$prompt" "$def"
    else
        printf '%s: ' "$prompt"
    fi
    read -r val || return 1
    if [ -z "$val" ] && [ -n "$def" ]; then
        val="$def"
    fi
    printf -v "$var" '%s' "$val"
    return 0
}

bs_select() {
    local title="$1" var="$2"
    shift 2
    local items=("$@")
    local i choice
    printf '\n%s\n' "$title"
    for i in "${!items[@]}"; do
        printf '  %2d) %s\n' "$((i + 1))" "${items[$i]}"
    done
    printf 'Selection [1-%d, 0 to cancel]: ' "${#items[@]}"
    read -r choice || return 1
    case "$choice" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ "$choice" -ge 1 ] && [ "$choice" -le "${#items[@]}" ] 2>/dev/null; then
        printf -v "$var" '%s' "${items[$((choice - 1))]}"
        return 0
    fi
    return 1
}

# Safe option-value helper for CLI parsers.
#   bs_val2 <varname> <optname> "$@"
# Dies if the option has no following value. The caller then does `shift 2`,
# which is guaranteed safe because the value was verified to exist.
bs_val2() {
    local var="$1" opt="$2"
    if [ "$#" -lt 3 ]; then
        bs_die "Missing value for option '$opt'"
    fi
    printf -v "$var" '%s' "$3"
}

# ---------------------------------------------------------------------------
# Safe key=value configuration parsing.
#
# Only whitelisted keys are ever turned into shell variables. The file is
# never sourced and values are never evaluated. This prevents malicious
# relay/playlist configuration from executing commands or injecting
# variables.
# ---------------------------------------------------------------------------
bs_parse_kv_file() {
    local line key val
    [ -r "$BLUESTREAM_CFG_FILE" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|\#*) continue ;;
        esac
        key="${line%%=*}"
        val="${line#*=}"
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        case "$key" in
            ''|*[!A-Za-z0-9_]*) continue ;;
        esac
        case " ${BLUESTREAM_CFG_KEYS:-} " in
            *" $key "*) ;;
            *) continue ;;
        esac
        printf -v "${BLUESTREAM_CFG_PREFIX:-}${key}" '%s' "$val"
    done < "$BLUESTREAM_CFG_FILE"
    return 0
}

# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
bs_to_lower() {
    printf '%s' "$1" | tr 'A-Z' 'a-z'
}

bs_now_ts() {
    date +%Y%m%d-%H%M%S
}

bs_disk_free() {
    local p="$1"
    df -h "$p" 2>/dev/null | awk 'NR==2 { print $4" free of "$2" ("$5" used)" }'
}

bs_append_line() {
    # Appends a single validated line to a root-only config file.
    local file="$1" line="$2"
    printf '%s\n' "$line" >> "$file"
}

bs_strip_quotes() {
    local s="$1"
    s="${s#\"}"
    s="${s%\"}"
    s="${s#\'}"
    s="${s%\'}"
    printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# FFmpeg argv integrity helpers
# ---------------------------------------------------------------------------
# Redact one argv element for safe display: URL-like values (which may carry
# credentials) pass through bs_redact_url; everything else is shown as-is
# (local paths contain no secrets by construction).
bs_redact_arg() {
    local a="$1"
    case "$a" in
        http://*|https://*|rtmp://*|rtmps://*|rtsp://*) bs_redact_url "$a" ;;
        *) printf '%s' "$a" ;;
    esac
}

# Print a sanitized copy of the global FFMPEG_ARGS array (one element/line).
bs_dump_ffmpeg_args() {
    local i
    for i in "${!FFMPEG_ARGS[@]}"; do
        printf '  argv[%d] <%s>\n' "$i" "$(bs_redact_arg "${FFMPEG_ARGS[$i]}")"
    done
}

# Fail-closed structural verification of the global FFMPEG_ARGS array before
# exec'ing FFmpeg. Guarantees the codec options are separate elements with
# their own 'copy' values, and that the final positional is the HLS
# index.m3u8 output, so a stray 'copy' can never become a positional output
# filename. Returns 1 (and dumps the sanitized argv) on any anomaly.
bs_verify_ffmpeg_args() {
    local n="${#FFMPEG_ARGS[@]}" i
    [ "$n" -gt 0 ] || { bs_error "FFmpeg argv is empty."; return 1; }
    [ "$n" -ge 20 ] || { bs_error "FFmpeg argv implausibly short ($n elements)."; return 1; }
    for i in "${!FFMPEG_ARGS[@]}"; do
        [ -n "${FFMPEG_ARGS[$i]}" ] || { bs_error "FFmpeg argv element $i is empty."; return 1; }
    done
    i=0
    while [ "$i" -lt "$n" ]; do
        case "${FFMPEG_ARGS[$i]}" in
            -c:v|-c:a)
                [ $((i + 1)) -lt "$n" ] || { bs_error "Option ${FFMPEG_ARGS[$i]} has no value."; return 1; }
                if [ "${FFMPEG_ARGS[$((i + 1))]}" != "copy" ]; then
                    bs_error "Option ${FFMPEG_ARGS[$i]} must be followed by 'copy', found '${FFMPEG_ARGS[$((i + 1))]}'."
                    return 1
                fi
                i=$((i + 2))
                ;;
            *) i=$((i + 1)) ;;
        esac
    done
    case "${FFMPEG_ARGS[$((n - 1))]}" in
        */index.m3u8) ;;
        *) bs_error "FFmpeg output must end with 'index.m3u8', found '${FFMPEG_ARGS[$((n - 1))]}'."; return 1 ;;
    esac
    return 0
}

# end of common.sh




