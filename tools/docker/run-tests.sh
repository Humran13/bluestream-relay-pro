#!/usr/bin/env bash
# Runs INSIDE the bs-studio-test container. /src = read-only repo checkout; /work = scratch copy; /out = results (bind mount on D:).
# Phases are independent so one failure never hides the rest. Exit code = number of failed phases.
set -u
OUT=/out; mkdir -p "$OUT"
rm -rf /work && mkdir -p /work
( cd /src && tar --exclude=.git --exclude=node_modules --exclude=test-results --exclude=__pycache__ -cf - . ) | tar -xf - -C /work
cd /work || exit 1
ln -sfn /opt/pwdeps/node_modules /work/node_modules
FAILED=0
phase() { # name, command...
    local name="$1"; shift
    echo "=== PHASE $name ($(date -u +%T))" | tee -a "$OUT/summary.txt"
    if "$@" > "$OUT/$name.log" 2>&1; then echo "PASS  $name" | tee -a "$OUT/summary.txt"
    else echo "FAIL  $name (see $name.log)" | tee -a "$OUT/summary.txt"; FAILED=$((FAILED+1)); fi
    tail -4 "$OUT/$name.log" | sed 's/^/      /' | tee -a "$OUT/summary.txt"
}
: > "$OUT/summary.txt"
{ echo "uname: $(uname -a)"; nproc; free -m | head -2; ffmpeg -version | head -1; nginx -v 2>&1; python3 --version; node --version; } > "$OUT/env.txt" 2>&1
cat "$OUT/env.txt" | tee -a "$OUT/summary.txt"

id tester >/dev/null 2>&1 || useradd -m tester
chown -R tester /work
as_tester() { runuser -u tester -- env HOME=/home/tester BS_DOCKER_E2E=1 PLAYWRIGHT_BROWSERS_PATH=/opt/pw "$@"; }
# CI and production run these as NON-root; running as root hides real permission behaviour.
phase 01-full-suite        as_tester bash -c 'cd /work/webapp && python3 -m pytest -q -p no:cacheprovider'
phase 02-permissions-root  bash -c 'cd /work/webapp && BS_DOCKER_PERMS=1 python3 -m pytest test_studio_docker_perms.py -q -p no:cacheprovider'
phase 03-shell-selftests   bash -c 'cd /work && bash web-ctl-selftest.sh && for f in lib/*.sh config/systemd/*.sh install.sh web-ctl tools/*.sh tools/docker/*.sh; do bash -n "$f" || exit 1; done && shellcheck -S error lib/*.sh config/systemd/*.sh tools/*.sh tools/docker/*.sh'
phase 04-editor-playwright as_tester bash -c 'cd /work && PYTHON="python3 studio_server.py" npx playwright test -c tests/browser/playwright.config.js --project=chromium --project=firefox --project=webkit studio-editor'
CHROME="$(find /opt/pw -type f -name chrome -path '*chromium-*' ! -path '*headless*' | head -1)"
phase 05-browser-capture   as_tester bash -c "cd /work/webapp && BLUESTREAM_BROWSER_BIN='$CHROME' python3 -m pytest test_studio_browser.py -q -p no:cacheprovider"
# INFORMATIONAL: does Chromium's own sandbox start inside this container (default seccomp usually blocks user namespaces)?
echo "=== INFO chromium-sandbox-probe" | tee -a "$OUT/summary.txt"
if as_tester timeout 40 "$CHROME" --headless=new --user-data-dir=/tmp/sbprobe --screenshot=/tmp/sbprobe.png --window-size=320,240 about:blank > "$OUT/chromium-sandbox-probe.log" 2>&1 && [ -s /tmp/sbprobe.png ]; then
    echo "INFO  chromium sandbox: STARTS in this container" | tee -a "$OUT/summary.txt"
else
    echo "INFO  chromium sandbox: DOES NOT START in this container (see chromium-sandbox-probe.log) - expected under Docker's default seccomp; says nothing about a VPS" | tee -a "$OUT/summary.txt"
fi
exit "$FAILED"
