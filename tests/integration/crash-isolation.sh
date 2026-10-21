#!/usr/bin/env bash
# BlueStream Relay Pro - crash-isolation integration test.
#
# Proves, with REAL services (not mocks), that BlueStream's own streaming
# engine can never take down a pre-existing web server/control panel
# (CloudPanel, or any unrelated website sharing the host) when it crashes.
# This is a release-validation test, not part of the fast CI suite: it
# requires Docker with a privileged, systemd-PID1 container and takes a
# couple of minutes. Run it after any change to install.sh, lib/nginx.sh,
# lib/envdetect.sh, lib/migrate.sh, or any *.service unit.
#
# What it does, against a single disposable Ubuntu 24.04 container:
#   1. Installs a plain nginx site FIRST (stands in for CloudPanel/other
#      websites already on the host) and confirms it serves traffic.
#   2. Installs BlueStream Relay Pro and confirms it auto-selected the
#      ISOLATED nginx strategy (lib/envdetect.sh) because of that existing
#      site - i.e. it never touches the system nginx.
#   3. Starts a real looping local-file relay; confirms real HLS is served
#      through BlueStream's OWN independent nginx (bluestream-nginx.service).
#   4. Repeatedly SIGSEGVs BlueStream's own nginx process and asserts the
#      other site, the system nginx.service, and BlueStream's own GUI
#      (bluestream-web.service) are never affected.
#   5. Confirms systemd's restart-rate limiting contains the crash loop
#      (the other site stays up regardless of which way that settles).
#   6. Confirms an intentionally-broken isolated nginx config is REJECTED
#      by `bluestream-manager nginx reload` (nginx -t gate) and that the
#      already-running last-known-good config keeps serving real HLS.
#
# Usage (from the repo root, Linux/macOS/WSL host with Docker):
#   MSYS_NO_PATHCONV=1 \
#   CID=$(docker run -d --privileged --cgroupns=host \
#     --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
#     -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
#     -v "$(pwd):/repo:ro" \
#     -v "$(pwd)/tests/integration/crash-isolation.sh:/crash-isolation-test.sh:ro" \
#     ubuntu:24.04 bash -c "apt-get update -qq && \
#       DEBIAN_FRONTEND=noninteractive apt-get install -y -qq systemd systemd-sysv dbus && \
#       exec /sbin/init")
#   # wait for: docker exec "$CID" systemctl is-system-running  ->  running|degraded
#   docker exec "$CID" bash /crash-isolation-test.sh
#   docker rm -f "$CID"
#
# --security-opt seccomp=unconfined/apparmor=unconfined is required by some
# Docker hosts (notably Docker Desktop on Windows/WSL2) for nested systemd
# to apply the relay/playlist/publisher units' own sandboxing directives
# (ProtectSystem=strict etc.) correctly; without it those units can fail to
# start for container-runtime reasons unrelated to BlueStream's own code.
set -euo pipefail

log() { printf '\n=== %s ===\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1"; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }

export DEBIAN_FRONTEND=noninteractive

log "Waiting for systemd to be ready"
for i in $(seq 1 30); do
    systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
    sleep 1
done

log "Installing base packages (simulating a host with its OWN pre-existing nginx/site)"
apt-get update -qq
apt-get install -y -qq nginx curl ffmpeg sudo python3 >/dev/null

mkdir -p /var/www/othersite
echo "OTHER-SITE-OK" > /var/www/othersite/index.html
cat > /etc/nginx/sites-available/othersite <<'EOF'
server {
    listen 8099;
    root /var/www/othersite;
    location / { try_files /index.html =404; }
}
EOF
ln -sf /etc/nginx/sites-available/othersite /etc/nginx/sites-enabled/othersite
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
sleep 1
curl -fsS http://127.0.0.1:8099/ | grep -q OTHER-SITE-OK || fail "pre-existing other-site did not come up before BlueStream install"
pass "pre-existing 'CloudPanel-like' other-site is up on system nginx (port 8099)"

log "Installing BlueStream Relay Pro"
cd /repo
bash ./install.sh --non-interactive --assume-yes --domain bluestream.test 2>&1 | tee /tmp/install.log
grep -qi "isolated" /tmp/install.log || fail "installer did not report isolated strategy"
pass "installer selected ISOLATED nginx strategy (other site forced it)"

# shellcheck disable=SC1091
. /etc/bluestream/server.conf 2>/dev/null || true
[ "${NGINX_STRATEGY:-}" = "isolated" ] || fail "server.conf NGINX_STRATEGY is not 'isolated' (got '${NGINX_STRATEGY:-}')"
pass "server.conf confirms NGINX_STRATEGY=isolated"

curl -fsS http://127.0.0.1:8099/ | grep -q OTHER-SITE-OK || fail "other-site broken immediately after BlueStream install"
pass "other-site still OK immediately after BlueStream install"

systemctl is-active bluestream-nginx.service >/dev/null || fail "bluestream-nginx.service is not active after install"
pass "bluestream-nginx.service (BlueStream's own isolated nginx) is active"

log "Admin account + a real looping relay"
printf 'Str0ngP@ssw0rd!\n' | (cd /usr/local/lib/bluestream && python3 -m webapp.security init-admin --state-dir /var/lib/bluestream/web --username admin) >/tmp/admin.log 2>&1 \
    || fail "admin init failed: $(cat /tmp/admin.log)"

# NOTE: the relay unit's ProtectSystem=strict/PrivateTmp=true sandbox means
# the source media must live outside /tmp (which becomes a private, empty
# tmpfs per service) and outside /root (hidden by ProtectHome) - use /opt.
mkdir -p /opt/bsmedia
ffmpeg -loglevel error -f lavfi -i testsrc=size=320x240:rate=15 -f lavfi -i sine=frequency=1000 \
    -t 60 -c:v libx264 -g 30 -c:a aac -pix_fmt yuv420p -y /opt/bsmedia/clip.mp4 \
    || fail "could not generate synthetic test clip"
chmod 0644 /opt/bsmedia/clip.mp4

bluestream-manager relay add crashtest --type local-file --url /opt/bsmedia/clip.mp4 --loop yes --start --enable \
    || fail "relay add failed"
sleep 10

ISO_PORT="$(grep -oE '^ISOLATED_HTTP_PORT=.*' /etc/bluestream/server.conf | cut -d= -f2 | tr -d '"')"
[ -n "$ISO_PORT" ] || fail "could not determine isolated HTTP port"
HLS_OK=0
for i in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:${ISO_PORT}/hls/relay/crashtest/index.m3u8" >/tmp/hls_before.m3u8 2>/dev/null; then
        HLS_OK=1; break
    fi
    sleep 2
done
[ "$HLS_OK" = "1" ] || fail "HLS playlist not reachable through BlueStream's own isolated nginx before crash test"
pass "real relay streaming; HLS reachable through bluestream-nginx on loopback port ${ISO_PORT}"

systemctl is-active bluestream-web.service >/dev/null || fail "bluestream-web (GUI) is not active"
pass "bluestream-web (GUI/gunicorn) is active and independent of bluestream-nginx"

log "CRASH-ISOLATION TEST: repeatedly killing BlueStream's own nginx engine"
OTHER_FAILURES=0
for i in $(seq 1 8); do
    MAINPID="$(systemctl show -p MainPID --value bluestream-nginx.service)"
    if [ -n "$MAINPID" ] && [ "$MAINPID" != "0" ]; then
        kill -SEGV "$MAINPID" 2>/dev/null || true
    fi
    sleep 1
    curl -fsS -m 2 http://127.0.0.1:8099/ | grep -q OTHER-SITE-OK || OTHER_FAILURES=$((OTHER_FAILURES + 1))
    systemctl is-active nginx >/dev/null || fail "system (other-site) nginx.service itself went inactive during the crash loop at iteration $i"
    systemctl is-active bluestream-web.service >/dev/null || fail "bluestream-web (GUI) went down during BlueStream engine crash loop at iteration $i"
done

if [ "$OTHER_FAILURES" -gt 0 ]; then
    fail "other-site had $OTHER_FAILURES/8 failed requests while BlueStream's engine was being crashed"
fi
pass "other-site answered OK on every one of 8 checks while bluestream-nginx was repeatedly SIGSEGV'd"
pass "system nginx.service and bluestream-web.service both stayed active throughout the crash loop"

log "Restart-storm containment check"
sleep 2
STATE="$(systemctl is-active bluestream-nginx.service 2>/dev/null || true)"
FAILED="$(systemctl is-failed bluestream-nginx.service 2>/dev/null || true)"
printf 'bluestream-nginx.service state after crash loop: active=%s failed=%s\n' "$STATE" "$FAILED"
# Either it recovered (active) under Restart=on-failure, or systemd's
# StartLimitBurst correctly tripped and parked it in "failed" rather than
# restart-storming forever. Both are acceptable; an uncontrolled restart
# storm would still have left the OTHER site/services unaffected above,
# which is the actual safety property under test.
curl -fsS http://127.0.0.1:8099/ | grep -q OTHER-SITE-OK || fail "other-site broken AFTER the crash loop settled"
pass "other-site still OK after the crash loop settled (final state: active=$STATE failed=$FAILED)"

log "Invalid-config rejection test (must not clobber a working isolated instance)"
# Start from a clean, healthy state (the crash loop above may have left the
# unit mid-backoff) before introducing the deliberate corruption.
systemctl reset-failed bluestream-nginx.service 2>/dev/null || true
systemctl restart bluestream-nginx.service
sleep 1
systemctl is-active bluestream-nginx.service >/dev/null || fail "bluestream-nginx.service did not come back healthy before the config test"
curl -fsS "http://127.0.0.1:${ISO_PORT}/hls/relay/crashtest/index.m3u8" >/dev/null \
    || fail "HLS not reachable on the healthy baseline before the config test"

cp /etc/bluestream/nginx/nginx.conf /tmp/nginx.conf.goodcopy
printf 'this is not valid nginx syntax {{{\n' >> /etc/bluestream/nginx/nginx.conf
if bluestream-manager nginx reload 2>/tmp/reload.log; then
    fail "nginx reload with an intentionally-broken config did NOT fail"
fi
grep -qi "invalid\|not reloading\|failed validation" /tmp/reload.log || fail "reload rejection message unclear: $(cat /tmp/reload.log)"
pass "'bluestream-manager nginx reload' correctly refused to apply an invalid config"

# The REJECTED reload must never have told the running master to re-exec/
# re-read: the already-running worker (loaded from the last-good in-memory
# config) must keep serving, even though the file on disk is now broken by
# this direct hand-edit (BlueStream's own generator never writes a file in
# this state - this proves the activation gate, not the generator).
systemctl is-active bluestream-nginx.service >/dev/null \
    || fail "bluestream-nginx.service stopped serving after a REJECTED reload (should have kept running the last-good in-memory config)"
curl -fsS "http://127.0.0.1:${ISO_PORT}/hls/relay/crashtest/index.m3u8" >/dev/null \
    || fail "HLS stopped being served after a rejected (invalid) config reload attempt"
pass "last-known-good config kept running and still serving real HLS after the rejected reload"

# Restore the good file so a later restart (e.g. from a subsequent crash)
# can still pass its own ExecStartPre validation.
cp /tmp/nginx.conf.goodcopy /etc/bluestream/nginx/nginx.conf
nginx -t -c /etc/bluestream/nginx/nginx.conf || fail "restored config unexpectedly still invalid"
pass "good config restored on disk for any future restart"

log "ALL CRASH-ISOLATION ASSERTIONS PASSED"
