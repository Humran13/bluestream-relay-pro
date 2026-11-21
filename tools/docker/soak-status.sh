#!/usr/bin/env bash
# Read-only progress summary of the Docker 72-hour soak (does not touch the running container).
# Usage: tools/docker/soak-status.sh [results-dir]   (default D:/bluestream-studio-docker/soak72/result)
dir="${1:-/d/bluestream-studio-docker/soak72/result}"
csv="$dir/soak.csv"
[ -f "$csv" ] || { echo "no soak.csv yet in $dir"; exit 1; }
cat "$dir/../START.txt" 2>/dev/null
echo "container: $(docker ps -a --filter name=bs-studio-soak72 --format '{{.Status}}')"
awk -F, 'NR>1 { n++; el=$1; if ($4>0 && (min==""||$4<min)) min=$4; if ($4>0) {s+=$4; sn++}; c[n]=$2; if ($2>cmax) cmax=$2; if ($3>rmax) rmax=$3; drop=$6; frames=$5; alive+=($8==1?0:1) }
 END { printf "samples=%d elapsed=%ds (%.2f h of 72) speed_min=%s speed_avg=%.3f cpu_max=%.2f rss_max=%.0fMB drops_last=%s not_alive_samples=%d\n", n, el, el/3600, min, (sn?s/sn:0), cmax, rmax, drop, alive }' "$csv"
tail -2 "$csv"
[ -f "$dir/verdict.json" ] && { echo "--- FINAL VERDICT"; cat "$dir/verdict.json"; } || echo "(no verdict.json yet: the run is not finished; nothing may be claimed as a 72 h result)"
