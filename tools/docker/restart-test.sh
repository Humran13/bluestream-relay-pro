#!/usr/bin/env bash
# Host-side driver (Git Bash on Windows / any shell with docker): hard-restarts a container mid-stream and verifies recovery.
# State lives in a bind mount, results in $OUT. Does not touch any other container/volume/network.
set -eu
REPO_WIN="${1:?repo path (Windows form, e.g. C:\\...\\bluestream-relay-pro)}"
STATE_WIN="${2:?state dir (Windows form, e.g. D:\\bluestream-studio-docker\\state)}"
NAME=bs-studio-restart
export MSYS_NO_PATHCONV=1
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --cpus=4 --memory=8g --memory-swap=8g -v "$REPO_WIN:/src:ro" -v "$STATE_WIN:/state" bs-studio-test:local sleep infinity >/dev/null
run() { docker exec -u tester -e HOME=/home/tester "$NAME" python3 /work/tools/docker/restart_phase.py "$1"; }
docker exec "$NAME" bash -c 'id tester >/dev/null 2>&1 || useradd -m tester; mkdir -p /work /state; (cd /src && tar --exclude=.git --exclude=node_modules -cf - .) | tar -xf - -C /work; chown -R tester /work /state'
echo "[1] prepare";  run prepare
echo "[2] start (first life)"; run start
echo "[3] docker restart (hard stop mid-stream)"; docker restart -t 1 "$NAME" >/dev/null; sleep 3
docker exec "$NAME" bash -c 'pgrep -a ffmpeg || echo "no ffmpeg survived the restart (expected)"; ls /state/run | head -20'
echo "[4] start (second life, same persisted state)"; run start
sleep 8
echo "[5] verify"; run verify
docker rm -f "$NAME" >/dev/null
echo "RESTART TEST PASSED"
