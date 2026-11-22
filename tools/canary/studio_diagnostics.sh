#!/usr/bin/env bash
# BlueStream Studio - READ-ONLY diagnostic collector with automatic redaction.
#
#   sudo bash tools/canary/studio_diagnostics.sh > /tmp/bluestream-diag.txt        # then share that file
#   sudo bash tools/canary/studio_diagnostics.sh --since 24h > /tmp/diag.txt
#
# Changes NOTHING: no restarts, no repairs, no config edits, no files written (it prints to stdout; YOU choose where it goes).
# It never reads /etc/bluestream/destinations (stream keys), relay configs, admin.json, secret_key, sudoers or certificates.
# Everything printed passes through a redaction filter (stream keys, rtmp(s) URL paths, tokens, passwords, authorization,
# cookies, YouTube-style keys, long random strings, BlueStream licence tokens), and a final scan warns if anything still looks secret.
#
# Test hooks: BS_DIAG_ROOT (fake filesystem root); stub commands first on PATH.
set -u
umask 077
ROOT="${BS_DIAG_ROOT:-}"
SINCE="6h"
while [ $# -gt 0 ]; do
    case "$1" in
        --since) SINCE="${2:-6h}"; shift 2 ;;
        -h|--help) sed -n 2,12p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
case "$SINCE" in *[!0-9a-z]*|'') echo "bad --since (use e.g. 6h, 24h, 2d)" >&2; exit 2 ;; esac

have() { command -v "$1" >/dev/null 2>&1; }
t() { timeout 25 "$@" 2>&1; }

# ---------------------------------------------------------------- redaction
redact() {
    # The private loopback RTMP bus (rtmp://127.0.0.1:1935/<app>/<stream-name>) carries no secret: shield it from the URL rule.
    sed -E -e 's#rtmp://127\.0\.0\.1(:[0-9]+)?/#@@LOOPBUS@@\1@@/#g' | sed -E \
        -e 's#(rtmps?://[^/[:space:]"'"'"']+/[^/[:space:]"'"'"']+/)[^[:space:]"'"'"']+#\1[REDACTED-KEY]#g' \
        -e 's#(://)[^/[:space:]:@"'"'"']+:[^/[:space:]@"'"'"']+@#\1[REDACTED]@#g' \
        -e 's#([?&;](key|k|token|signature|sig|password|passwd|secret|auth|apikey|api_key|access_token|sessionid|session)=)[^&[:space:]"'"'"']*#\1[REDACTED]#Ig' \
        -e 's#(STREAM_KEY|DEST_STREAM_KEY|stream[_ -]?key|password|passwd|secret|token|api[_-]?key|authorization|cookie|set-cookie|csrf_token|secret_key)([[:space:]]*[:=][[:space:]]*)[^[:space:]]+#\1\2[REDACTED]#Ig' \
        -e 's#(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+/=-]{6,}#\1 [REDACTED]#g' \
        -e 's#BS1R?\.[A-Za-z0-9_.-]+#[REDACTED-LICENCE]#g' \
        -e 's#\b[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3,4}\b#[REDACTED-KEY]#g' \
        -e 's#\b[A-Za-z0-9_-]{32,}\b#[REDACTED-TOKEN]#g' \
        -e 's#@@LOOPBUS@@([^@]*)@@/#rtmp://127.0.0.1\1/#g'
}

section() { printf '\n===== %s =====\n' "$*"; }
cat_if() { [ -r "$1" ] && cat "$1" 2>/dev/null; }

report() {
    printf 'BlueStream Studio diagnostics (read-only, redacted)  %s  window=%s\n' "$(date -u +%FT%TZ 2>/dev/null)" "$SINCE"
    printf 'host: %s   run-as: %s\n' "$(hostname 2>/dev/null)" "$(id -un 2>/dev/null)"
    [ "$(id -u 2>/dev/null)" = 0 ] || printf 'NOTE: not root - journal, service details and process lists will be incomplete. Re-run with sudo.\n'

    section "Install receipt and versions"
    cat_if "$ROOT/var/lib/bluestream/canary-install.json"
    cat_if "$ROOT/usr/local/lib/bluestream/VERSION"
    have ffmpeg && ffmpeg -version 2>/dev/null | head -n1
    have nginx && nginx -v 2>&1 | head -n1
    python3 --version 2>&1
    uname -srm; cat_if "$ROOT/etc/os-release" | grep -E '^(PRETTY_NAME)='

    section "Resources"
    uptime
    have free && free -m
    have df && df -h "$ROOT/" "$ROOT/var" 2>/dev/null
    printf -- '-- top processes by CPU (command names only)\n'
    ps -eo pid,etimes,pcpu,pmem,rss,comm --sort=-pcpu 2>/dev/null | head -n 13
    printf -- '-- ffmpeg processes (arguments redacted below)\n'
    ps -eo pid,etimes,pcpu,pmem,rss,args 2>/dev/null | grep -E '[f]fmpeg' | cut -c1-600

    section "BlueStream services"
    t systemctl list-units --all --plain --no-pager 'bluestream-*'
    for u in $(systemctl list-units --all --plain --no-legend 'bluestream-*' 2>/dev/null | awk '{print $1}' | grep -E '^bluestream-[A-Za-z0-9@:_.-]+\.service$' | head -n 40); do
        printf -- '-- %s\n' "$u"
        t systemctl show "$u" -p ActiveState,SubState,Result,NRestarts,ExecMainStatus,ActiveEnterTimestamp,MemoryCurrent,CPUUsageNSec,TasksCurrent
    done

    section "Studio health and supervisor status files (non-secret, written by BlueStream)"
    for f in "$ROOT"/var/lib/bluestream/run/studio-*.health.json "$ROOT"/var/lib/bluestream/run/relay-*.status "$ROOT"/var/lib/bluestream/run/playlist-*.status; do
        [ -r "$f" ] || continue
        printf -- '-- %s\n' "$(basename "$f")"; cat "$f" 2>/dev/null; printf '\n'
    done
    for f in "$ROOT"/var/lib/bluestream/run/*.resolve-fail; do [ -r "$f" ] && { printf -- '-- %s: ' "$(basename "$f")"; cat "$f"; }; done

    section "Streams and destinations as the console sees them (read-only bridge; no keys)"
    WCTL="$ROOT/usr/local/lib/bluestream/web-ctl"
    if [ -x "$WCTL" ] && [ "$(id -u 2>/dev/null)" = 0 ]; then
        t "$WCTL" relay_list
        t "$WCTL" destination_list
        for r in $("$WCTL" relay_list 2>/dev/null | grep -o '"name": *"[a-z0-9_-]*"' | sed 's/.*"\([a-z0-9_-]*\)"$/\1/' | head -n 12); do
            printf -- '-- publisher status for %s\n' "$r"; t "$WCTL" relay_publisher_status "$r"
            t "$WCTL" studio_health "$r"
        done
    else
        printf '(needs root and an installed web-ctl)\n'
    fi

    section "Recent journal for BlueStream units (window $SINCE, last 400 lines)"
    have journalctl && timeout 40 journalctl --no-pager -o short-iso --since "-$SINCE" -u 'bluestream-*' -n 400 2>&1

    section "Kernel OOM / killed processes (24h)"
    have journalctl && timeout 40 journalctl -k --no-pager --since -24h 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process' | tail -n 20
    printf '(end of OOM list)\n'

    section "Isolated nginx error log (tail)"
    [ -r "$ROOT/var/log/bluestream-nginx/error.log" ] && tail -n 60 "$ROOT/var/log/bluestream-nginx/error.log"

    section "Network (counts and listening ports only)"
    printf -- '-- listening TCP ports\n'; ss -H -ltn 2>/dev/null | awk '{print $4}' | sort -u | head -n 60
    printf -- '-- established connections by remote port\n'
    ss -H -tn state established 2>/dev/null | awk '{n=split($4,a,":"); print a[n]}' | sort | uniq -c | sort -rn | head -n 10
    printf -- '-- established connections to RTMP/RTMPS (1935/443) from this host\n'
    ss -H -tn state established 2>/dev/null | awk '{n=split($4,a,":"); m=split($3,b,":"); if (b[m]=="1935" || b[m]=="443") print $3}' | sort | uniq -c | sort -rn | head -n 10

    section "Other workloads (names only; shows what shares this host)"
    have docker && t docker ps --format '{{.Names}}  {{.Status}}'
    have wg && t wg show interfaces

    section "Permissions of BlueStream directories (modes only)"
    for d in /var/lib/bluestream/media /var/lib/bluestream/run /var/lib/bluestream/studio /var/www/bluestream/hls; do
        stat -c '%U:%G %a %n' "$ROOT$d" 2>/dev/null
    done
}

OUT="$(report 2>&1 | redact)"
printf '%s\n' "$OUT"
printf '\n===== redaction self-check =====\n'
if printf '%s\n' "$OUT" | sed -E 's#rtmp://127\.0\.0\.1[^ ]*##g' | grep -Eiq '(rtmps?://[^ /]+/[^ /]+/[^[ ][^ ]{7,})|STREAM_KEY=[^[]|[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3,4}|BS1\.[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9]{8,}'; then
    printf 'WARNING: something that looks like a secret may remain in this report. Read it and remove it before sharing.\n'
else
    printf 'no stream-key-, token- or authorization-like strings found in this report.\n'
fi
printf 'Reminder: this tool is read-only. It restarted nothing and changed nothing.\n'
