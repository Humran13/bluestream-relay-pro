#!/usr/bin/env bash
# Activate BlueStream Studio inside the EXISTING native dashboard (same URL, same login, one extra "Studio" tab).
#
#   cd /root/bluestream-docker-src && git fetch -q origin feature/bluestream-studio && git checkout -q --detach <SHA given to you>
#   bash tools/canary/studio_activate.sh --execute [--vendor-pubkey <64 hex>]
# Without --execute it only checks and prints the plan (read-only).
#
# SAFETY (the live stream must not be interrupted):
#  * It never starts, stops or restarts any relay, publisher, playlist, FFmpeg, nginx, CloudPanel, RustDesk or Docker service.
#  * The ONLY restart is bluestream-web.service (the dashboard), after everything was staged and validated.
#  * Files are replaced atomically (write temp + rename), so a running script keeps its old inode and is never edited in place.
#  * `systemctl daemon-reload` does not restart running units. No package install, no firewall/SSH/SSL change, no sudoers change.
#  * Stream units are snapshotted before/after (MainPID, restart counter, invocation id); the report says Interrupted if any changed.
#  * Any failure triggers an automatic rollback of the dashboard files (studio_activate_rollback.sh); rollback never touches streams.
set -u
umask 022
EXPECT="1df0657a7b21ef1867ad81efc021c3b8ccd2a298"        # the code commit that was tested; deployed paths must match it exactly
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${BS_ACT_SRC:-$(cd "$HERE/../.." && pwd)}"
INST=/usr/local/lib/bluestream
UNITDIR=/etc/systemd/system
STATE=/var/lib/bluestream/web
STUDIO_DIR=/var/lib/bluestream/studio
BKROOT="${BS_ACT_BACKUP_ROOT:-/root/bluestream-backups}"
EXECUTE=0; PUBKEY=""; PORT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=1; shift ;;
        --vendor-pubkey) PUBKEY="${2:-}"; shift 2 ;;
        --web-port) PORT="${2:-}"; shift 2 ;;
        --expect) EXPECT="${2:-}"; shift 2 ;;
        -h|--help) sed -n 2,16p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
stop_here() { say ""; say "BLOCKED: $*"; say "Nothing was changed on the server."; exit 1; }
[ "$(id -u)" = 0 ] || stop_here "run as root"
for t in systemctl python3 install cmp sha256sum pgrep visudo; do command -v "$t" >/dev/null 2>&1 || stop_here "required tool missing: $t"; done
[ -z "$PUBKEY" ] || printf '%s' "$PUBKEY" | grep -Eq '^[0-9a-fA-F]{64}$' || stop_here "--vendor-pubkey must be the 64-hex PUBLIC key (never the private key)"

# ---------------------------------------------------------------- 1. source verification
say "== 1. Source"
if [ "${BS_ACT_SKIP_GIT:-0}" != 1 ]; then
    command -v git >/dev/null 2>&1 || stop_here "git is required to verify the source"
    git -C "$SRC" cat-file -e "$EXPECT^{commit}" 2>/dev/null || stop_here "commit $EXPECT is not in $SRC (run: git -C $SRC fetch origin feature/bluestream-studio)"
    git -C "$SRC" diff --quiet "$EXPECT" -- lib config web-ctl webapp ':(exclude)webapp/test_*.py' \
        || stop_here "deployed paths (lib config web-ctl webapp) in $SRC differ from tested commit $EXPECT (wrong checkout or local edits)"
    say "  source $SRC: deployed paths identical to tested commit ${EXPECT:0:12}"
fi
for f in lib/relay.sh lib/publisher.sh web-ctl config/systemd/run-relay.sh config/systemd/run-publisher.sh webapp/app.py webapp/studio/views.py tools/canary/studio_smoke.py tools/canary/studio_activate_rollback.sh; do
    [ -f "$SRC/$f" ] || stop_here "source file missing: $f"
done

# ---------------------------------------------------------------- 2. production discovery
say "== 2. Production discovery (read-only)"
systemctl cat bluestream-web.service >/dev/null 2>&1 || stop_here "bluestream-web.service not found: this is not a native BlueStream install"
systemctl is-active --quiet bluestream-web.service || stop_here "bluestream-web.service is not active; fix the dashboard first (journalctl -u bluestream-web -n 50)"
[ -n "$PORT" ] || PORT="$(systemctl cat bluestream-web.service | grep -oE -- '--bind +127\.0\.0\.1:[0-9]+' | head -n1 | grep -oE '[0-9]+$')"
[ -n "$PORT" ] || stop_here "cannot determine the dashboard port (pass --web-port)"
for d in "$INST/lib" "$INST/webapp" "$INST/web-ctl" "$STATE/secret_key" "$STATE/admin.json"; do [ -e "$d" ] || stop_here "expected production path missing: $d"; done
id bluestream-web >/dev/null 2>&1 || stop_here "user bluestream-web missing"
visudo -c >/dev/null 2>&1 || stop_here "current sudoers does not validate (visudo -c); not touching anything"
grep -rqs "web-ctl" /etc/sudoers.d 2>/dev/null || stop_here "no sudoers rule for web-ctl found in /etc/sudoers.d"
say "  dashboard: bluestream-web.service active, 127.0.0.1:$PORT"
say "  sudoers: valid and unchanged by this script (the existing web-ctl rule covers the Studio operations)"

if [ -d "$STUDIO_DIR" ]; then
    say "  studio data dir: $(stat -c '%a %U:%G' "$STUDIO_DIR") (expected 2775 root:bluestream-web: the web user writes projects, relay accounts only read them)"
else
    say "  studio data dir missing: will be created root:bluestream-web 2775"
fi

# ---------------------------------------------------------------- 3. live-stream snapshot and impact analysis
tree_pids() { local p="$1" c; echo "$p"; for c in $(pgrep -P "$p" 2>/dev/null); do tree_pids "$c"; done; }
stream_units() {
    systemctl list-units --type=service --state=active,activating --plain --no-legend --no-pager 2>/dev/null \
        | awk '{print $1}' | grep -E '^bluestream-(relay|publisher|playlist|browser)@' | sort
}
snapshot() {
    local out="$1" u mp nr inv st ff p
    : > "$out"
    for u in $(stream_units); do
        mp="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"; nr="$(systemctl show -p NRestarts --value "$u" 2>/dev/null)"
        inv="$(systemctl show -p InvocationID --value "$u" 2>/dev/null)"; st="$(systemctl is-active "$u" 2>/dev/null)"
        ff=""; if [ -n "$mp" ] && [ "$mp" != 0 ]; then for p in $(tree_pids "$mp"); do [ "$(cat /proc/$p/comm 2>/dev/null)" = ffmpeg ] && ff="$ff $p"; done; fi
        printf '%s|%s|%s|%s|%s|ffmpeg:%s\n' "$u" "$mp" "$nr" "$inv" "$st" "${ff# }" >> "$out"
    done
}
SNAPDIR="$(mktemp -d /root/.bs-act.XXXXXX)" || stop_here "cannot create a temp dir under /root"
trap 'rm -rf "$SNAPDIR"' EXIT
snapshot "$SNAPDIR/streams.before"
find /etc/bluestream -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum > "$SNAPDIR/config.before" 2>/dev/null
say "== 3. Live streams that must not be interrupted"
if [ -s "$SNAPDIR/streams.before" ]; then
    while IFS='|' read -r u mp nr inv st ff; do
        say "  $u  state=$st MainPID=$mp restarts=$nr $ff"
        held="$(ls -l "/proc/$mp/fd" 2>/dev/null | grep -oE '/usr/local/lib/bluestream/[A-Za-z0-9_./-]+' | sort -u | sed 's#.*/##' | tr '\n' ' ')"
        [ -n "$held" ] && say "      files held open by its process: $held"
    done < "$SNAPDIR/streams.before"
    say "  Impact: replaced files get NEW inodes (atomic rename). Running processes keep the old, unchanged inodes, so they are not"
    say "  affected. Only a FUTURE restart of a unit would load the new files, which are the tested versions of the same code."
else
    say "  WARNING: no active relay/publisher/playlist unit found. If you expected a live stream (dino), it is not running under systemd as seen here."
fi

# ---------------------------------------------------------------- 4. staging + validation (nothing in production touched)
BK="$BKROOT/studio-activate-$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$SNAPDIR/stage"
mkdir -p "$STAGE/lib" "$STAGE/webapp" "$STAGE/run" || stop_here "cannot stage"
cp -a "$SRC"/lib/*.sh "$STAGE/lib/"
for n in run-relay.sh run-playlist.sh run-playlist-start.sh run-publisher.sh; do cp -a "$SRC/config/systemd/$n" "$STAGE/run/$n"; done
cp -a "$SRC/web-ctl" "$STAGE/web-ctl"
cp -a "$SRC/config/systemd/bluestream-publisher@.service" "$STAGE/bluestream-publisher@.service"
( cd "$SRC/webapp" && find . -type f ! -name 'test_*.py' ! -name '*.pyc' ! -path '*/__pycache__/*' ! -path '*/.*/*' -print0 | while IFS= read -r -d '' f; do
      mkdir -p "$STAGE/webapp/$(dirname "$f")"; cp -a "$f" "$STAGE/webapp/$f"; done )
find "$STAGE" -type f \( -name '*.sh' -o -name web-ctl \) -exec sed -i 's/\r$//' {} + 2>/dev/null
printf '[Service]\nEnvironment=BLUESTREAM_STUDIO_ENABLED=1\nReadWritePaths=%s\n' "$STUDIO_DIR" > "$STAGE/50-studio.conf"

say "== 4. Validation of the staged files"
bad=0
for f in "$STAGE"/lib/*.sh "$STAGE"/run/*.sh "$STAGE/web-ctl"; do bash -n "$f" 2>/dev/null || { say "  bash syntax error: ${f#$STAGE/}"; bad=1; }; done
while IFS= read -r -d '' f; do python3 - "$f" <<'PY' || { say "  python syntax error: ${f#$STAGE/}"; bad=1; }
import ast, sys
ast.parse(open(sys.argv[1], encoding="utf-8").read())
PY
done < <(find "$STAGE/webapp" -name '*.py' -print0)
grep -q 'studio_frame' "$STAGE/web-ctl" || { say "  staged web-ctl lacks the Studio operations"; bad=1; }
grep -q 'bs_have_ffprobe' "$STAGE/lib/common.sh" || { say "  staged lib lacks bs_have_ffprobe"; bad=1; }
( cd "$STAGE" && BLUESTREAM_STUDIO_ENABLED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$STAGE" python3 -c "import webapp.app, webapp.studio.views, webapp.studio.runtime" ) >"$SNAPDIR/import.log" 2>&1 \
    || { say "  the staged application does not import:"; tail -n 5 "$SNAPDIR/import.log" | sed 's/^/      /'; bad=1; }
[ "$bad" = 0 ] || stop_here "staged files failed validation"
say "  bash, python, web-ctl operations and application import: OK"

# ---------------------------------------------------------------- 5. change plan
PLAN=()
plan() { local src="$1" dst="$2" mode="$3"; if [ -f "$dst" ] && cmp -s "$src" "$dst" && [ "$(stat -c %a "$dst")" = "$mode" ] && [ "$(stat -c %U:%G "$dst")" = root:root ]; then return 0; fi; PLAN+=("$src|$dst|$mode"); }
for f in "$STAGE"/lib/*.sh; do plan "$f" "$INST/lib/$(basename "$f")" 644; done
for f in "$STAGE"/run/*.sh; do plan "$f" "$INST/$(basename "$f")" 755; done
plan "$STAGE/web-ctl" "$INST/web-ctl" 700
while IFS= read -r -d '' f; do rel="${f#$STAGE/webapp/}"; plan "$f" "$INST/webapp/$rel" 644; case "$rel" in studio/*.py) plan "$f" "$INST/$rel" 644 ;; esac; done < <(find "$STAGE/webapp" -type f -print0)
plan "$STAGE/bluestream-publisher@.service" "$UNITDIR/bluestream-publisher@.service" 644
plan "$STAGE/50-studio.conf" "$UNITDIR/bluestream-web.service.d/50-studio.conf" 644
if [ -n "$PUBKEY" ]; then printf '%s\n' "$PUBKEY" > "$STAGE/vendor_pubkey.hex"; plan "$STAGE/vendor_pubkey.hex" "$INST/studio/vendor_pubkey.hex" 644; fi

say "== 5. Plan"
say "  ${#PLAN[@]} file(s) to install/replace (atomic):"
for e in "${PLAN[@]}"; do d="${e#*|}"; d="${d%|*}"; if [ -e "$d" ]; then say "    replace $d"; else say "    add     $d"; fi; done
say "  then: systemctl daemon-reload (no unit restarted), restart ONLY bluestream-web.service, run the smoke test"
if [ "$EXECUTE" != 1 ]; then
    say ""; say "DRY RUN complete: no blockers, nothing changed. To deploy:  bash tools/canary/studio_activate.sh --execute${PUBKEY:+ --vendor-pubkey <hex>}"
    exit 0
fi

# ---------------------------------------------------------------- 6. deploy
say "== 6. Deploying"
mkdir -p "$BK/files" || stop_here "cannot create backup dir"
chmod 0700 "$BK"; : > "$BK/manifest"; echo "$PORT" > "$BK/web-port"
cp -a "$SRC/tools/canary/studio_activate_rollback.sh" "$BK/rollback.sh"
( cd / && tar -czf "$BK/pre-deploy-snapshot.tar.gz" usr/local/lib/bluestream/lib usr/local/lib/bluestream/webapp usr/local/lib/bluestream/web-ctl     usr/local/lib/bluestream/studio usr/local/lib/bluestream/run-*.sh etc/systemd/system/bluestream-publisher@.service etc/systemd/system/bluestream-web.service ) 2>/dev/null
chmod 0600 "$BK/pre-deploy-snapshot.tar.gz" 2>/dev/null
say "  backup: $BK  (rollback: bash $BK/rollback.sh)"

fail_and_rollback() {
    say ""; say "FAILED: $*"; say "Rolling back the dashboard files (no stream is touched)..."
    bash "$BK/rollback.sh" --dir "$BK" --auto || say "!! automatic rollback reported a problem; run: bash $BK/rollback.sh --dir $BK"
    snapshot "$SNAPDIR/streams.after"
    cmp -s "$SNAPDIR/streams.before" "$SNAPDIR/streams.after" && say "Live streams: unchanged." || say "Live streams: CHANGED (see below)"
    exit 1
}

mkdir -p "$UNITDIR/bluestream-web.service.d" "$INST/studio"
if [ ! -d "$STUDIO_DIR" ]; then install -d -o root -g bluestream-web -m 2775 "$STUDIO_DIR" || fail_and_rollback "cannot create $STUDIO_DIR"; fi
for e in "${PLAN[@]}"; do
    src="${e%%|*}"; rest="${e#*|}"; dst="${rest%|*}"; mode="${rest##*|}"
    if [ -e "$dst" ]; then mkdir -p "$BK/files$(dirname "$dst")"; cp -a "$dst" "$BK/files$dst" || fail_and_rollback "cannot back up $dst"; echo "MOD $dst" >> "$BK/manifest"
    else echo "NEW $dst" >> "$BK/manifest"; fi
    mkdir -p "$(dirname "$dst")"
    install -o root -g root -m "$mode" "$src" "$dst.bs-new.$$" && mv -f "$dst.bs-new.$$" "$dst" || fail_and_rollback "cannot install $dst"
done
for e in "${PLAN[@]}"; do
    src="${e%%|*}"; rest="${e#*|}"; dst="${rest%|*}"
    cmp -s "$src" "$dst" || fail_and_rollback "verification failed for $dst"
done
say "  ${#PLAN[@]} file(s) installed and verified byte-for-byte"
( cd "$INST" && BLUESTREAM_STUDIO_ENABLED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INST" python3 -c "import webapp.app, webapp.studio.views" ) >/dev/null 2>&1 \
    || fail_and_rollback "the installed application does not import"
visudo -c >/dev/null 2>&1 || fail_and_rollback "sudoers no longer validates"
systemctl daemon-reload || fail_and_rollback "systemctl daemon-reload failed"

say "  restarting ONLY bluestream-web.service ..."
touch "$BK/web-restarted"
systemctl restart bluestream-web.service || fail_and_rollback "bluestream-web.service failed to restart"
ok=0
for _ in $(seq 1 30); do
    if systemctl is-active --quiet bluestream-web.service && python3 -c "
import http.client,sys
c=http.client.HTTPConnection('127.0.0.1',$PORT,timeout=5);c.request('GET','/console/login');sys.exit(0 if c.getresponse().status==200 else 1)" 2>/dev/null; then ok=1; break; fi
    sleep 2
done
[ "$ok" = 1 ] || fail_and_rollback "the dashboard did not return HTTP 200 on 127.0.0.1:$PORT after the restart"
systemctl show -p Environment --value bluestream-web.service | grep -q 'BLUESTREAM_STUDIO_ENABLED=1' || fail_and_rollback "Studio is not enabled in the running dashboard"

# ---------------------------------------------------------------- 7. verification
say "== 7. Verification"
MP="$(systemctl show -p MainPID --value bluestream-web.service)"
if command -v nsenter >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1 && [ -n "$MP" ] && [ "$MP" != 0 ] && nsenter -t "$MP" -m -- true 2>/dev/null; then
    probe="$STUDIO_DIR/.bs-write-probe.$$"
    if nsenter -t "$MP" -m -- setpriv --reuid bluestream-web --regid bluestream-web --init-groups sh -c "echo ok > '$probe' && rm -f '$probe'" 2>/dev/null; then
        say "PASS  the dashboard process (sandboxed, as bluestream-web) can write Studio projects in $STUDIO_DIR"
    else
        rm -f "$probe" 2>/dev/null
        fail_and_rollback "the sandboxed dashboard cannot write $STUDIO_DIR (ReadWritePaths/permissions)"
    fi
else
    say "WARN  could not enter the dashboard's mount namespace to probe sandbox write access; the smoke test below still runs"
fi
python3 "$SRC/tools/canary/studio_smoke.py" --port "$PORT" --state-dir "$STATE" --expect-studio | tee "$SNAPDIR/smoke.out"
SMOKE_RC=${PIPESTATUS[0]}
[ "$SMOKE_RC" = 0 ] || fail_and_rollback "the post-deployment smoke test failed (see FAIL lines above)"
LIC="$(sed -n 's/^INFO  licence state: //p' "$SNAPDIR/smoke.out" | head -n1)"

snapshot "$SNAPDIR/streams.after"
find /etc/bluestream -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum > "$SNAPDIR/config.after" 2>/dev/null
STREAMS="Running"
if [ -s "$SNAPDIR/streams.before" ]; then
    while IFS='|' read -r u mp nr inv st ff; do
        line="$(grep -F "$u|" "$SNAPDIR/streams.after" | head -n1)"
        a_mp="$(printf '%s' "$line" | cut -d'|' -f2)"; a_nr="$(printf '%s' "$line" | cut -d'|' -f3)"; a_inv="$(printf '%s' "$line" | cut -d'|' -f4)"; a_st="$(printf '%s' "$line" | cut -d'|' -f5)"
        if [ "$a_st" = active ] && [ "$a_mp" = "$mp" ] && [ "$a_nr" = "$nr" ] && [ "$a_inv" = "$inv" ]; then say "PASS  $u unchanged (same process, same invocation, restarts $a_nr)"
        else say "FAIL  $u CHANGED: before MainPID=$mp restarts=$nr ($st); after MainPID=${a_mp:-none} restarts=${a_nr:-?} (${a_st:-gone})"; STREAMS="Interrupted"; fi
    done < "$SNAPDIR/streams.before"
else STREAMS="Unverified (no active stream units were seen)"; fi
if cmp -s "$SNAPDIR/config.before" "$SNAPDIR/config.after"; then CFG="Intact"; say "PASS  /etc/bluestream configuration files are byte-identical to before"
else CFG="CHANGED"; say "WARN  these /etc/bluestream files differ from before (changed by someone/something during the run):"; diff <(awk '{print $2}' "$SNAPDIR/config.before") <(awk '{print $2}' "$SNAPDIR/config.after") | sed 's/^/        /'; fi

date -u +%FT%TZ > "$BK/deployed"
cp "$SNAPDIR/smoke.out" "$BK/smoke-report.txt" 2>/dev/null
say ""
say "================ REPORT ================"
say "Dashboard:        Working (127.0.0.1:$PORT, restarted once, only that unit)"
say "Studio tab:       Working (loads with the existing login session)"
say "Existing login:   Preserved (same admin account and secret key; not modified)"
case "$LIC" in
    ACTIVE) say "Studio backend:   Working (licence active)" ;;
    NO-VENDOR-KEY) say "Studio backend:   Integrated; BLOCKED for premium use: no vendor public key is installed (licence cannot be verified)" ;;
    *) say "Studio backend:   Integrated; licence state: ${LIC:-unknown} (a valid licence token is required to create/publish projects)" ;;
esac
say "Live streams:     $STREAMS"
say "Configuration:    $CFG"
say "CloudPanel:       Unaffected by this script (it never touches nginx, CloudPanel or RustDesk)"
say "Rollback:         Available: bash $BK/rollback.sh"
[ "$STREAMS" = Interrupted ] && exit 3
exit 0
