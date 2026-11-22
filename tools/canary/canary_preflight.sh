#!/usr/bin/env bash
# BlueStream Studio - PRODUCTION-CANARY PREFLIGHT.   READ-ONLY.
#
# For the owner-authorised live test on an EXISTING production VPS (CloudPanel, other sites, Docker, WireGuard...).
# It changes NOTHING: no files written, no services touched, no packages installed (apt is only *simulated* with -s).
# It does NOT use or fake the separate staging marker; it is a different, clearly named gate.
#
#   sudo bash tools/canary/canary_preflight.sh            # root gives the most complete picture (process names, ufw, nginx -t)
#
# Exit status: 0 = no blocking conflict found (warnings may exist), 1 = at least one BLOCKER.
# A PASS here is necessary, not sufficient: you must also have a provider snapshot and make the install decision.
#
# Test hooks (never needed in real use): BS_CP_ROOT (fake filesystem root), stub commands first on PATH.
set -u

ROOT="${BS_CP_ROOT:-}"
NPASS=0; NWARN=0; NFAIL=0
WARNINGS=(); BLOCKERS=()

pass() { NPASS=$((NPASS+1)); printf '  PASS  %s\n' "$*"; }
warn() { NWARN=$((NWARN+1)); WARNINGS+=("$*"); printf '  WARN  %s\n' "$*"; }
fail() { NFAIL=$((NFAIL+1)); BLOCKERS+=("$*"); printf '  FAIL  %s\n' "$*"; }
info() { printf '  info  %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
num() { case "${1:-}" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

# Exact package list the installer will ask apt for (install.sh install_packages + web_packages).
BS_APT_PACKAGES="nginx ffmpeg curl ca-certificates coreutils util-linux libnginx-mod-rtmp python3 python3-flask gunicorn sudo"

printf 'BlueStream Studio production-canary preflight (READ-ONLY)  %s\n' "$(date -u +%FT%TZ 2>/dev/null)"
printf 'host: %s   user: %s\n' "$(hostname 2>/dev/null)" "$(id -un 2>/dev/null)"
[ "$(id -u 2>/dev/null)" = "0" ] || info "not running as root: some checks (process names, ufw, nginx -t) are skipped or partial; re-run with sudo for a full picture"

# ---------------------------------------------------------------- OS / resources
section "Operating system and resources"
. "$ROOT/etc/os-release" 2>/dev/null
if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ]; then pass "Ubuntu 24.04"
elif [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "22.04" ]; then warn "Ubuntu 22.04: supported by the installer but Studio was only validated on 24.04"
else fail "OS is '${ID:-?} ${VERSION_ID:-?}': BlueStream targets Ubuntu 24.04 (22.04 tolerated)"; fi
have systemctl && pass "systemd present" || fail "systemd not found"

CPUS="$(num "$(nproc 2>/dev/null)")"
LOAD1="$(awk '{print $1}' "$ROOT/proc/loadavg" 2>/dev/null)"; LOAD5="$(awk '{print $2}' "$ROOT/proc/loadavg" 2>/dev/null)"; LOAD15="$(awk '{print $3}' "$ROOT/proc/loadavg" 2>/dev/null)"
info "CPUs: $CPUS   load average (1/5/15 min): ${LOAD1:-?} ${LOAD5:-?} ${LOAD15:-?}"
if [ "$CPUS" -lt 2 ]; then fail "only $CPUS CPU: one 720p30 x264 encode needs about 1 full core plus headroom"
elif [ "$CPUS" -lt 4 ]; then warn "$CPUS CPUs (Studio target is 4); a single 720p30 stream should fit if the host is otherwise idle"
else pass "$CPUS CPUs"; fi
# spare cores = cpus - 15-minute load (use the longest window: the stream runs for days)
SPARE="$(awk -v c="$CPUS" -v l="${LOAD15:-0}" 'BEGIN{printf "%.2f", c-l}')"
if awk -v s="$SPARE" 'BEGIN{exit !(s<1.5)}'; then fail "only ~$SPARE spare cores on the 15-minute load average: not enough for a live x264 encode (needs ~1-2)"
elif awk -v s="$SPARE" 'BEGIN{exit !(s<2.5)}'; then warn "~$SPARE spare cores (15-min load): tight; watch the production sites during the first hour"
else pass "~$SPARE spare cores (15-min load)"; fi

MEM_KB="$(num "$(awk '/^MemTotal:/ {print $2}' "$ROOT/proc/meminfo" 2>/dev/null)")"
AVAIL_KB="$(num "$(awk '/^MemAvailable:/ {print $2}' "$ROOT/proc/meminfo" 2>/dev/null)")"
SWAP_KB="$(num "$(awk '/^SwapTotal:/ {print $2}' "$ROOT/proc/meminfo" 2>/dev/null)")"
info "RAM total $((MEM_KB/1024)) MB, available $((AVAIL_KB/1024)) MB, swap $((SWAP_KB/1024)) MB"
if [ "$AVAIL_KB" -lt 1500000 ]; then fail "only $((AVAIL_KB/1024)) MB RAM available (< 1.5 GB): risk of OOM-killing production services"
elif [ "$AVAIL_KB" -lt 3000000 ]; then warn "$((AVAIL_KB/1024)) MB RAM available (< 3 GB): tight"
else pass "$((AVAIL_KB/1024)) MB RAM available"; fi

VAR_FREE_KB="$(num "$(df -Pk "$ROOT/var" 2>/dev/null | awk 'NR==2{print $4}')")"
ROOT_FREE_KB="$(num "$(df -Pk "$ROOT/" 2>/dev/null | awk 'NR==2{print $4}')")"
info "free disk: /var $((VAR_FREE_KB/1024/1024)) GB, / $((ROOT_FREE_KB/1024/1024)) GB"
if [ "$VAR_FREE_KB" -lt 10000000 ]; then fail "less than 10 GB free on /var (media, HLS and logs live there)"
elif [ "$VAR_FREE_KB" -lt 20000000 ]; then warn "less than 20 GB free on /var"
else pass "enough free disk on /var"; fi

if [ -n "$(fuser /var/lib/dpkg/lock-frontend 2>/dev/null)" ] 2>/dev/null; then fail "apt/dpkg is busy right now (lock held): wait for unattended upgrades to finish"; else pass "apt/dpkg lock is free"; fi
timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes && pass "clock is NTP-synchronised (countdown/clock overlays depend on it)" || warn "clock sync not confirmed (timedatectl); countdown and clock overlays depend on correct time"

# ---------------------------------------------------------------- hosting panel / other web servers
section "Hosting panel and web servers"
PANEL=""
if have clpctl || [ -d "$ROOT/etc/cloudpanel" ] || [ -d "$ROOT/home/clp" ]; then PANEL="CloudPanel"; fi
if [ -n "$PANEL" ]; then
    pass "CloudPanel detected: the installer will use ISOLATED nginx (own unit bluestream-nginx.service, loopback only) and will not write /etc/nginx"
else
    info "no CloudPanel markers found"
fi
NGINX_PRESENT=no; have nginx && NGINX_PRESENT=yes
if [ "$NGINX_PRESENT" = yes ]; then
    NGINX_BIN="$(command -v nginx)"; NGINX_VER="$(nginx -v 2>&1 | head -n1)"
    info "system nginx: $NGINX_BIN ($NGINX_VER)"
    if dpkg -s nginx >/dev/null 2>&1; then
        INST_VER="$(dpkg-query -W -f='${Version}' nginx 2>/dev/null)"
        pass "nginx is managed by apt/dpkg (version $INST_VER)"
        POLICY="$(apt-cache policy nginx 2>/dev/null)"
        CAND="$(printf '%s\n' "$POLICY" | awk '/Candidate:/ {print $2}')"
        if [ -n "$CAND" ] && [ "$CAND" != "$INST_VER" ]; then
            fail "apt would change the nginx package ($INST_VER -> $CAND) while installing BlueStream's dependencies: this could disturb CloudPanel's web server. Do NOT continue until this is understood"
        else
            pass "nginx installed version equals the apt candidate (no nginx upgrade needed)"
        fi
        ABI="$(dpkg -s nginx 2>/dev/null | grep -E '^Provides:' | grep -o 'nginx-abi-[0-9.]*-[0-9]*' | head -n1)"
        if [ -z "$ABI" ]; then
            # CloudPanel/other builds may provide the ABI through nginx-common/nginx-core; check any installed package
            ABI="$(dpkg -l 2>/dev/null | awk '/^ii/ {print $2}' | xargs -r dpkg -s 2>/dev/null | grep -E '^Provides:' | grep -o 'nginx-abi-[0-9.]*-[0-9]*' | head -n1)"
        fi
        if [ "$ABI" = "nginx-abi-1.24.0-1" ]; then pass "nginx provides the ABI (nginx-abi-1.24.0-1) that libnginx-mod-rtmp requires"
        else fail "installed nginx provides ABI '${ABI:-none}', but the Ubuntu libnginx-mod-rtmp package requires nginx-abi-1.24.0-1: apt would have to replace nginx"; fi
    else
        fail "an nginx binary exists that is NOT managed by apt/dpkg (control-panel or source build): the installer would refuse, and installing the Ubuntu nginx package could overwrite it"
    fi
    if [ "$(id -u)" = "0" ]; then
        if nginx -t >/dev/null 2>&1; then pass "current nginx configuration tests OK (nginx -t): a graceful reload will not fail because of existing problems"
        else fail "nginx -t FAILS right now: installing the RTMP module queues a system-nginx reload, which would then fail or be unsafe. Fix nginx first"; fi
    else
        info "run as root to execute 'nginx -t'"
    fi
    if [ -e "$ROOT/etc/nginx/modules-enabled/50-mod-rtmp.conf" ]; then pass "RTMP module already enabled in system nginx (the package step will not change it)"
    else warn "libnginx-mod-rtmp will add /etc/nginx/modules-enabled/50-mod-rtmp.conf (one 'load_module' line) and queue a GRACEFUL reload of the system nginx at the end of the apt run. Existing connections are not dropped; this is the only change to the system nginx"; fi
else
    info "no system nginx: the installer would install nginx and own it (not a CloudPanel host)"
fi
for ws in apache2 caddy; do systemctl is-active --quiet "$ws" 2>/dev/null && info "$ws is active (isolated nginx will be used)"; done

# ---------------------------------------------------------------- apt simulation
section "Package changes (apt SIMULATION only, nothing is installed)"
if have apt-get; then
    SIM="$(apt-get -s install --no-install-recommends $BS_APT_PACKAGES 2>&1)"; SIMRC=$?
    if [ $SIMRC -ne 0 ]; then
        fail "apt cannot satisfy BlueStream's package list: $(printf '%s\n' "$SIM" | grep -E '^E:|unmet|Unable' | head -n3 | tr '\n' ' ') -- if this says 'Unable to locate package', your apt package lists are empty or stale: run 'sudo apt-get update' (metadata only, installs nothing) and re-run this preflight"
    else
        NEW="$(printf '%s\n' "$SIM" | grep -c '^Inst ')"; REM="$(printf '%s\n' "$SIM" | grep -c '^Remv ')"
        info "the install would add/upgrade $NEW package(s) and remove $REM"
        if [ "$REM" -gt 0 ]; then fail "apt would REMOVE packages: $(printf '%s\n' "$SIM" | grep '^Remv ' | awk '{print $2}' | head -n8 | tr '\n' ' ')"; else pass "no packages would be removed"; fi
        UPG="$(printf '%s\n' "$SIM" | grep '^Inst ' | grep -E ' \[[^]]+\] \(' | awk '{print $2}')"
        if [ -n "$UPG" ]; then
            if printf '%s\n' "$UPG" | grep -Eq '^(nginx|nginx-common|nginx-core|libnginx|docker|containerd|openssh|wireguard|cloudpanel|mysql|mariadb|php|libc6|systemd)'; then
                fail "apt would UPGRADE critical packages already on this host: $(printf '%s\n' "$UPG" | grep -E '^(nginx|nginx-common|nginx-core|libnginx|docker|containerd|openssh|wireguard|cloudpanel|mysql|mariadb|php|libc6|systemd)' | head -n8 | tr '\n' ' ')"
            else
                warn "apt would upgrade already-installed packages: $(printf '%s\n' "$UPG" | head -n10 | tr '\n' ' ')"
            fi
        else
            pass "no already-installed package would be upgraded"
        fi
        NEWPKGS="$(printf '%s\n' "$SIM" | grep '^Inst ' | awk '{print $2}' | head -n40 | tr '\n' ' ')"
        [ -n "$NEWPKGS" ] && info "new packages (first 40): $NEWPKGS"
    fi
else
    fail "apt-get not found"
fi

# ---------------------------------------------------------------- containers, VPN, other services
section "Other applications (must keep running untouched)"
if have docker; then
    if docker ps >/dev/null 2>&1; then
        info "docker containers running: $(docker ps --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
        pass "Docker present: BlueStream never touches Docker, its networks or its firewall rules"
    else
        info "docker is installed but not queryable by this user (run with sudo to list containers)"
    fi
else
    info "docker not installed"
fi
if have wg; then info "WireGuard interfaces: $(wg show interfaces 2>/dev/null | tr '\n' ' ')"; else info "wg tool not found"; fi

# ---------------------------------------------------------------- ports
section "Network ports (BlueStream uses loopback only)"
LISTEN="$(ss -H -ltn 2>/dev/null | awk '{print $4}')"
inuse() { printf '%s\n' "$LISTEN" | grep -Eq "[:.]$1\$"; }
if inuse 1935; then
    if systemctl is-active --quiet bluestream-nginx.service 2>/dev/null; then info "port 1935 is held by an existing BlueStream isolated nginx"
    else fail "port 1935 (BlueStream's private RTMP bus, 127.0.0.1 only) is already in use by something else: $(ss -H -ltnp 2>/dev/null | grep ':1935 ' | awk '{print $NF}' | head -n1)"; fi
else pass "port 1935 free (RTMP bus would bind 127.0.0.1 only)"; fi
FREEP=""; for p in 8080 8180 8280 8380 8480 18080 28080 38080; do inuse "$p" || FREEP="$FREEP $p"; done
if [ -n "$FREEP" ]; then pass "console/gateway loopback ports available:$FREEP"; else fail "none of the candidate loopback ports (8080 8180 ... 38080) is free"; fi
info "listening TCP ports: $(printf '%s\n' "$LISTEN" | sed 's/.*[:.]//' | sort -un | tr '\n' ' ')"
RUSTDESK="$(printf '%s\n' "$LISTEN" | grep -Ec '[:.](2111[5-9])$')"; [ "$RUSTDESK" -gt 0 ] && info "RustDesk ports 21115-21119 are in use (BlueStream does not use them)"
pass "installer does not open any public port and (without --with-ufw) does not change firewall rules"

# ---------------------------------------------------------------- SSH / firewall (informational)
section "SSH and firewall (informational: the install will not change them)"
if systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; then pass "ssh service active"; else warn "ssh service not detected as active"; fi
if have ufw && [ "$(id -u)" = "0" ]; then info "ufw: $(ufw status 2>/dev/null | head -n1)"; fi
info "BlueStream's installer changes SSH config: no; firewall rules: no (unless --with-ufw, which the canary wrapper never passes); SSL certificates: no (unless --with-ssl, never passed)"

# ---------------------------------------------------------------- existing BlueStream
section "Existing BlueStream installation"
EXIST=no
{ [ -d "$ROOT/etc/bluestream" ] || [ -d "$ROOT/usr/local/lib/bluestream" ]; } && EXIST=yes
UNITS="$(systemctl list-units --all --plain --no-legend 'bluestream-*' 2>/dev/null | awk '{print $1" "$3}' | head -n20)"
if [ "$EXIST" = yes ] || [ -n "$UNITS" ]; then
    warn "an existing BlueStream installation or units were found: the installer will REPAIR/UPDATE it in place; confirm you want that for the canary"
    [ -n "$UNITS" ] && printf '%s\n' "$UNITS" | sed 's/^/        /'
    ACTIVE_RELAYS="$(systemctl list-units --plain --no-legend 'bluestream-relay@*' 'bluestream-playlist@*' 2>/dev/null | grep -c running)"
    [ "$ACTIVE_RELAYS" -gt 0 ] && warn "$ACTIVE_RELAYS existing BlueStream stream(s) are RUNNING: do not install while they must stay uninterrupted without reading the install notes (CANARY.md)"
else
    pass "no existing BlueStream installation (clean install)"
fi
have ffmpeg && info "ffmpeg present: $(ffmpeg -version 2>/dev/null | head -n1)" || info "ffmpeg not installed (will be installed)"

# ---------------------------------------------------------------- YouTube reachability
section "Outbound reachability (TCP connect only, no data sent)"
for target in a.rtmp.youtube.com:1935 a.rtmps.youtube.com:443; do
    h="${target%:*}"; p="${target#*:}"
    if timeout 6 bash -c "exec 3<>/dev/tcp/$h/$p" 2>/dev/null; then pass "can connect to $target"; else warn "cannot connect to $target (firewall/provider block, or DNS)"; fi
done

# ---------------------------------------------------------------- summary
section "Result"
printf '  passed=%d  warnings=%d  blockers=%d\n' "$NPASS" "$NWARN" "$NFAIL"
if [ "$NFAIL" -gt 0 ]; then
    printf '\nRESULT: BLOCKED - do not install. Blockers:\n'; for b in "${BLOCKERS[@]}"; do printf '  - %s\n' "$b"; done
    printf '\nStop and resolve (or share this output); nothing was changed on this server.\n'
    exit 1
fi
if [ "$NWARN" -gt 0 ]; then printf '\nRESULT: NO BLOCKERS, %d WARNING(S) - read each warning before deciding.\n' "$NWARN"
else printf '\nRESULT: NO BLOCKERS FOUND.\n'; fi
cat <<'EOF'

Before installing you must still, yourself:
  1. Take a PROVIDER SNAPSHOT of this VPS and note its id/name.
  2. Decide that this live test is acceptable for the sites and services on this server.
  3. Run the guarded installer (dry run first):  see docs/bluestream/CANARY.md
Nothing was modified by this preflight.
EOF
exit 0
