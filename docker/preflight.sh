#!/usr/bin/env bash
# BlueStream (Docker) - READ-ONLY PREFLIGHT. Changes nothing: no containers, networks, volumes, images, files or packages.
#
#   bash docker/bluestream.sh preflight [--project NAME] [--http-port N] [--cpus X] [--mem 2g]
#
# Exit status: 0 = no blockers, 1 = at least one BLOCKER (installation is refused). Warnings need your judgement.
# Test hooks: BS_PF_ROOT (fake filesystem root); stub commands first on PATH.
set -u
ROOT="${BS_PF_ROOT:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${BS_COMPOSE_FILE:-$HERE/compose.yml}"
PROJECT="bluestream"; HTTP_PORT=""; CPUS="2.0"; MEM="2g"
while [ $# -gt 0 ]; do
    case "$1" in
        --project) PROJECT="${2:-}"; shift 2 ;;
        --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
        --cpus) CPUS="${2:-}"; shift 2 ;;
        --mem) MEM="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
NPASS=0; NWARN=0; NFAIL=0; BLOCKERS=()
pass() { NPASS=$((NPASS+1)); printf '  PASS  %s\n' "$*"; }
warn() { NWARN=$((NWARN+1)); printf '  WARN  %s\n' "$*"; }
fail() { NFAIL=$((NFAIL+1)); BLOCKERS+=("$*"); printf '  FAIL  %s\n' "$*"; }
info() { printf '  info  %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
num() { case "${1:-}" in ''|*[!0-9]*) echo 0 ;; *) echo "$1" ;; esac; }

printf 'BlueStream Docker preflight (READ-ONLY)  project=%s  %s\n' "$PROJECT" "$(date -u +%FT%TZ 2>/dev/null)"
printf '%s' "$PROJECT" | grep -Eq '^[a-z][a-z0-9-]{2,30}$' || { fail "project name must match ^[a-z][a-z0-9-]{2,30}$"; echo; echo "RESULT: BLOCKED"; exit 1; }
printf '%s' "$CPUS" | grep -Eq '^[0-9]+(\.[0-9]+)?$' || fail "--cpus must be a number"
printf '%s' "$MEM" | grep -Eq '^[0-9]+[mMgG]$' || fail "--mem must look like 2g or 1536m"
[ -z "$HTTP_PORT" ] || { printf '%s' "$HTTP_PORT" | grep -Eq '^[0-9]{4,5}$' && [ "$HTTP_PORT" -ge 1024 ] && [ "$HTTP_PORT" -le 65535 ] || fail "--http-port must be 1024-65535"; }

# ---------------------------------------------------------------- docker
section "Docker engine and Compose"
if ! have docker; then
    fail "Docker is not installed. BlueStream's Docker deployment needs Docker Engine + the Compose plugin. Nothing was installed. See docs/bluestream/DOCKER.md section 'If Docker is missing' for the separately approved setup procedure"
else
    if docker info >/dev/null 2>&1; then
        pass "docker daemon reachable by this user ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
    else
        fail "cannot talk to the Docker daemon as this user (permission denied or daemon stopped): $(docker info 2>&1 | grep -iE 'permission|cannot connect|denied' | head -n1). Run with sudo or add your user to the docker group yourself; this tool will not change it"
    fi
    if docker compose version >/dev/null 2>&1; then pass "Docker Compose plugin present ($(docker compose version --short 2>/dev/null))"
    else fail "the 'docker compose' plugin is missing (Compose v2 required)"; fi
    DINFO="$(docker info --format '{{.NCPU}} {{.MemTotal}} {{.CgroupVersion}} {{.CgroupDriver}} {{.DockerRootDir}} {{.Driver}}' 2>/dev/null)"
    set -- $DINFO
    D_NCPU="$(num "${1:-}")"; D_MEM="$(num "${2:-}")"; D_CGV="${3:-?}"; D_ROOT="${5:-/var/lib/docker}"
    info "engine: ${D_NCPU} CPUs, $((D_MEM/1024/1024)) MB RAM visible, cgroup v${D_CGV}, root dir ${D_ROOT}, storage driver ${6:-?}"
    [ "$D_CGV" = "2" ] || warn "cgroup v1 or unknown: CPU/memory limits are enforced less strictly"
    docker info --format '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless && info "rootless Docker detected (fine; low ports are not used)"
fi

# ---------------------------------------------------------------- resources
section "Host resources and current load"
CORES="$(num "$(nproc 2>/dev/null)")"
LOAD15="$(awk '{print $3}' "$ROOT/proc/loadavg" 2>/dev/null)"
AVAIL_KB="$(num "$(awk '/^MemAvailable:/ {print $2}' "$ROOT/proc/meminfo" 2>/dev/null)")"
info "CPUs $CORES, 15-min load ${LOAD15:-?}, RAM available $((AVAIL_KB/1024)) MB"
SPARE="$(awk -v c="$CORES" -v l="${LOAD15:-0}" 'BEGIN{printf "%.2f", c-l}')"
if [ -z "$LOAD15" ] || [ "$CORES" -le 0 ]; then warn "could not read CPU count/load on this host (no Linux /proc); verify manually that 2+ cores are idle"
elif awk -v s="$SPARE" 'BEGIN{exit !(s<1.5)}'; then fail "only ~$SPARE spare cores (15-min load): not enough for a live 720p30 encode (needs ~1-2)"
elif awk -v s="$SPARE" 'BEGIN{exit !(s<2.5)}'; then warn "~$SPARE spare cores: tight; the container is capped at $CPUS CPU(s) so other apps keep their share, but watch them"
else pass "~$SPARE spare cores"; fi
case "$MEM" in *[gG]) MEM_KB=$(( ${MEM%[gG]} * 1024 * 1024 )) ;; *) MEM_KB=$(( ${MEM%[mM]} * 1024 )) ;; esac
if [ "$AVAIL_KB" -le 0 ]; then warn "could not read available RAM on this host (no Linux /proc); verify manually that about $((MEM_KB/1024+700)) MB is free"
elif [ "$AVAIL_KB" -lt $((MEM_KB + 700000)) ]; then fail "only $((AVAIL_KB/1024)) MB RAM available; the container limit is $MEM and the host needs headroom (~700 MB)"
else pass "$((AVAIL_KB/1024)) MB RAM available for a $MEM container limit"; fi
if have docker && [ -n "${D_ROOT:-}" ]; then
    FREE_KB="$(num "$(df -Pk "$ROOT$D_ROOT" 2>/dev/null | awk 'NR==2{print $4}')")"
    [ "$FREE_KB" -gt 0 ] || FREE_KB="$(num "$(df -Pk "$ROOT/var" 2>/dev/null | awk 'NR==2{print $4}')")"
    info "free disk for Docker data: $((FREE_KB/1024/1024)) GB"
    if [ "$FREE_KB" -le 0 ]; then warn "could not measure free disk for Docker data; verify manually that 8+ GB is free"
    elif [ "$FREE_KB" -lt 8000000 ]; then fail "less than 8 GB free for Docker data (image ~1.5 GB + media + HLS + logs)"
    elif [ "$FREE_KB" -lt 20000000 ]; then warn "less than 20 GB free for Docker data"
    else pass "enough free disk"; fi
fi

# ---------------------------------------------------------------- conflicts
section "Existing applications, ports, names and storage"
if have docker && docker info >/dev/null 2>&1; then
    info "running containers: $(docker ps --format '{{.Names}}' 2>/dev/null | tr '\n' ' ')"
    CNAME="${PROJECT}-app"
    OWNER="$(docker inspect -f '{{index .Config.Labels "com.bluestream.project"}}' "$CNAME" 2>/dev/null)"
    if docker inspect "$CNAME" >/dev/null 2>&1; then
        if [ "$OWNER" = "$PROJECT" ]; then warn "container $CNAME already exists and belongs to this BlueStream project: 'up' will UPDATE it in place (data volumes are kept)"
        else fail "a container named $CNAME exists and is NOT a BlueStream project container: choose another --project"; fi
    else pass "container name $CNAME is free"; fi
    net="${PROJECT}_net"
    {
        if docker network inspect "$net" >/dev/null 2>&1; then
            [ "$(docker network inspect -f '{{index .Labels "com.bluestream.project"}}' "$net" 2>/dev/null)" = "$PROJECT" ] && warn "network $net already belongs to this project" || fail "docker network $net exists and is not BlueStream's: choose another --project"
        else pass "network name $net is free"; fi
    }
    for v in etc units data www; do
        vol="${PROJECT}_$v"
        if docker volume inspect "$vol" >/dev/null 2>&1; then
            [ "$(docker volume inspect -f '{{index .Labels "com.bluestream.project"}}' "$vol" 2>/dev/null)" = "$PROJECT" ] && warn "volume $vol already exists (this project's data will be REUSED, not wiped)" || fail "docker volume $vol exists and is not BlueStream's: choose another --project"
        else pass "volume name $vol is free"; fi
    done
fi
LISTEN="$(ss -H -ltn 2>/dev/null | awk '{print $4}')"
PUBLISHED=""
have docker && PUBLISHED="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -v "^${PROJECT}-app " | grep -oE '[0-9.]*:[0-9]+->' | sed -E 's/->$//; s/.*://' | sort -u | tr '\n' ' ')"
# a re-run for the same project updates it in place, so the port its own container already holds is not a conflict
OWNPORT=""
if have docker && [ "$(docker inspect -f '{{index .Config.Labels "com.bluestream.project"}}' "${PROJECT}-app" 2>/dev/null)" = "$PROJECT" ]; then
    OWNPORT="$(docker port "${PROJECT}-app" 80/tcp 2>/dev/null | head -n1 | sed 's/.*://' | tr -d '\r')"
fi
busy() { [ "$1" = "$OWNPORT" ] && return 1; printf '%s\n' "$LISTEN" | grep -Eq "[:.]$1\$" || printf ' %s ' "$PUBLISHED" | grep -q " $1 "; }
if [ -n "$HTTP_PORT" ]; then
    busy "$HTTP_PORT" && fail "host port $HTTP_PORT is already in use or published by another container" || pass "requested dashboard port 127.0.0.1:$HTTP_PORT is free"
else
    FREEP=""; for p in 18090 18091 18092 18093 18094 18095 18096 18097 18098 18099; do busy "$p" || { FREEP="$p"; break; }; done
    [ -n "$FREEP" ] && pass "dashboard will use 127.0.0.1:$FREEP (loopback only; the RTMP bus is not published at all)" || fail "no free port in 18090-18099; pass --http-port"
fi
info "occupied host ports (not touched): $(printf '%s\n' "$LISTEN" | sed 's/.*[:.]//' | sort -un | tr '\n' ' ')"

section "Existing native BlueStream installation (never reused or modified)"
NATIVE=no
{ [ -d "$ROOT/etc/bluestream" ] || [ -d "$ROOT/usr/local/lib/bluestream" ]; } && NATIVE=yes
UNITS="$(systemctl list-units --all --plain --no-legend 'bluestream-*' 2>/dev/null | awk '{print $1" "$3" "$4}' | head -n 20)"
if [ "$NATIVE" = yes ] || [ -n "$UNITS" ]; then
    warn "a NATIVE BlueStream installation exists on this host. The Docker deployment is completely separate: it uses its own volumes, its own container-internal services and ports, and cannot see or change /etc/bluestream, /var/lib/bluestream, /var/www/bluestream or any bluestream-* unit. It is NOT an upgrade of the native dashboard; it will have its own dashboard URL, admin login and licence"
    [ -n "$UNITS" ] && printf '%s\n' "$UNITS" | sed 's/^/        native unit: /'
    RUNNING="$(printf '%s\n' "$UNITS" | grep -c 'running')"
    [ "$RUNNING" -gt 0 ] && warn "$RUNNING native BlueStream unit(s) running: their stream keeps its CPU; do not start a second stream on the same YouTube key"
else
    pass "no native BlueStream installation"
fi

# ---------------------------------------------------------------- compose file safety
section "Compose file safety (static)"
if [ -f "$COMPOSE_FILE" ]; then
    bad=0
    grep -Eq '^\s*privileged:\s*true' "$COMPOSE_FILE" && { fail "compose file requests privileged mode"; bad=1; }
    grep -Eq 'docker\.sock' "$COMPOSE_FILE" && { fail "compose file mounts the Docker socket"; bad=1; }
    grep -Eq 'network_mode:\s*"?host' "$COMPOSE_FILE" && { fail "compose file uses host networking"; bad=1; }
    grep -Eq '^\s*pid:\s*"?host|^\s*ipc:\s*"?host' "$COMPOSE_FILE" && { fail "compose file shares the host PID/IPC namespace"; bad=1; }
    grep -Eq '^\s*-\s*(/|\./|\.\./|~)[^:]*:/' "$COMPOSE_FILE" && { fail "compose file bind-mounts a host path"; bad=1; }
    grep -Eq '"?(0\.0\.0\.0:)?[0-9]+:[0-9]+"?\s*$' "$COMPOSE_FILE" && grep -E '^\s*-\s*"?[0-9]+:[0-9]+' "$COMPOSE_FILE" | grep -vq '127.0.0.1' && { fail "compose file publishes a port on all interfaces"; bad=1; }
    grep -Eq 'cpus:' "$COMPOSE_FILE" && grep -Eq 'mem_limit:' "$COMPOSE_FILE" || { fail "compose file has no CPU/memory limits"; bad=1; }
    grep -Eq 'cap_drop:\s*\[ALL\]' "$COMPOSE_FILE" || { fail "compose file does not drop all capabilities"; bad=1; }
    [ "$bad" = 0 ] && pass "compose file: not privileged, no Docker socket, no host mounts, loopback-only ports, CPU/RAM limits, capabilities dropped"
else
    fail "compose file not found: $COMPOSE_FILE"
fi

# ---------------------------------------------------------------- outbound
section "Outbound reachability (TCP connect only)"
for target in a.rtmp.youtube.com:1935 a.rtmps.youtube.com:443; do
    h="${target%:*}"; p="${target#*:}"
    if timeout 6 bash -c "exec 3<>/dev/tcp/$h/$p" 2>/dev/null; then pass "can connect to $target"; else warn "cannot connect to $target (provider/firewall block, or DNS)"; fi
done

section "Result"
printf '  passed=%d  warnings=%d  blockers=%d\n' "$NPASS" "$NWARN" "$NFAIL"
if [ "$NFAIL" -gt 0 ]; then
    printf '\nRESULT: BLOCKED - installation is refused. Blockers:\n'; for b in "${BLOCKERS[@]}"; do printf '  - %s\n' "$b"; done
    printf '\nNothing was changed.\n'; exit 1
fi
[ "$NWARN" -gt 0 ] && printf '\nRESULT: NO BLOCKERS, %d WARNING(S) - read each one.\n' "$NWARN" || printf '\nRESULT: NO BLOCKERS FOUND.\n'
exit 0
