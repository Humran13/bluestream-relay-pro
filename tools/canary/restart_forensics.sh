#!/usr/bin/env bash
# BlueStream - READ-ONLY restart forensics for one stream's units (owner-run on the VPS).
#   bash tools/canary/restart_forensics.sh [--stream dino] [--days 14]
# Changes nothing: no restarts, no config writes, no network. Stream keys/URLs are redacted from anything printed.
set -u
STREAM=dino; DAYS=14
while [ $# -gt 0 ]; do
    case "$1" in
        --stream) STREAM="${2:-}"; shift 2 ;;
        --days) DAYS="${2:-}"; shift 2 ;;
        *) echo "usage: $0 [--stream NAME] [--days N]" >&2; exit 1 ;;
    esac
done
case "$STREAM" in ''|*[!a-z0-9_-]*) echo "invalid stream name" >&2; exit 1 ;; esac
case "$DAYS" in ''|*[!0-9]*) echo "invalid days" >&2; exit 1 ;; esac
redact() { sed -E 's#(rtmps?://[^ /]+/[^ /]+/)[^ "'\'']+#\1<redacted>#g; s#(key|token|secret)=[^ &"]+#\1=<redacted>#Ig'; }

echo "== units for stream '$STREAM' =="
mapfile -t UNITS < <(systemctl list-units --all --plain --no-legend --type=service "bluestream-relay@${STREAM}.service" "bluestream-publisher@relay:${STREAM}:*" 2>/dev/null | awk '{print $1}')
[ "${#UNITS[@]}" -gt 0 ] || { echo "no matching units found"; exit 2; }
for u in "${UNITS[@]}"; do
    echo "---- $u"
    systemctl show "$u" -p ActiveState -p SubState -p NRestarts -p ActiveEnterTimestamp -p ExecMainStatus -p Result -p Restart -p RestartUSec 2>/dev/null
    echo "restart events per day (last ${DAYS}d), from the journal:"
    journalctl -u "$u" --since "-${DAYS}d" -o short-iso --no-pager 2>/dev/null \
        | grep -E "Scheduled restart job|Main process exited|Failed with result|Started " | awk '{print substr($1,1,10)}' | sort | uniq -c | tail -n "$DAYS"
    echo "restart events per hour-of-day (pattern: periodic = loop/idle timeout, bursts = network):"
    journalctl -u "$u" --since "-${DAYS}d" -o short-iso --no-pager 2>/dev/null \
        | grep -E "Scheduled restart job" | awk '{print substr($1,12,2)}' | sort | uniq -c | sort -k2 | tr '\n' ' '; echo
    echo "most common exit reasons:"
    journalctl -u "$u" --since "-${DAYS}d" --no-pager 2>/dev/null | grep -E "Main process exited|Failed with result" | sed -E 's/^[^ ]+ +[0-9]+ +[0-9:]+ +[^ ]+ +[^ ]+ +//' | sort | uniq -c | sort -rn | head -5
    echo "last 12 FFmpeg/supervisor lines (redacted):"
    journalctl -u "$u" -n 12 --no-pager -o cat 2>/dev/null | redact
done

echo "== live FFmpeg processes (command lines redacted) =="
ps -eo pid,etimes,pcpu,rss,args --sort=-pcpu 2>/dev/null | grep -E "[f]fmpeg" | redact | cut -c1-200 | head -8
echo "== host headroom =="
uptime; free -m | head -2; df -h /var/lib/bluestream /var/www/bluestream 2>/dev/null
echo "== HLS freshness for the stream =="
HLS_DIR="/var/www/bluestream/hls/relay/${STREAM}"
if [ -d "$HLS_DIR" ]; then
    ls -l --time-style=+%H:%M:%S "$HLS_DIR" 2>/dev/null | tail -4
    date +%H:%M:%S
fi
echo "Read-only run complete. Nothing was changed."
