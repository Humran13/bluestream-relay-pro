#!/usr/bin/env bash
# Studio staging preflight. Run ON the staging VPS BEFORE installing anything:
#
#   sudo tools/staging_preflight.sh --snapshot-id "<provider snapshot name/id>"
#
# It REFUSES to continue (exit 1) unless every check passes. It changes nothing on the host
# except writing its report to /var/tmp/studio-staging/preflight.txt.
#
# The operator must create the marker file first, which is a deliberate act that says
# "this machine is staging and may be broken":
#       echo STAGING-OK | sudo tee /etc/bluestream-staging
#
# Overrides (tests only): BS_PF_ROOT (fake filesystem root), BS_PF_CPUS, BS_PF_MEM_KB, BS_PF_FREE_KB, BS_PF_HOSTNAME.
set -u

ROOT="${BS_PF_ROOT:-}"
PROD_HOSTS="${BLUESTREAM_PRODUCTION_HOSTNAMES:-vmi2980718}"
MIN_CPUS=4
MIN_MEM_KB=7500000        # ~7.2 GiB usable of an 8 GB VPS
MIN_FREE_KB=20000000      # 20 GB free where /var lives
SNAPSHOT=""
FAIL=0
REPORT_DIR="${BS_PF_REPORT_DIR:-/var/tmp/studio-staging}"

while [ $# -gt 0 ]; do
    case "$1" in
        --snapshot-id) SNAPSHOT="${2:-}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

LINES=()
ok()   { LINES+=("PASS  $*"); }
bad()  { LINES+=("FAIL  $*"); FAIL=1; }

# 1. staging marker (explicit operator acknowledgement)
if [ "$(cat "$ROOT/etc/bluestream-staging" 2>/dev/null | head -n1)" = "STAGING-OK" ]; then
    ok "staging marker /etc/bluestream-staging present"
else
    bad "missing staging marker: run  echo STAGING-OK | sudo tee /etc/bluestream-staging  ONLY on the staging VPS"
fi

# 2. must not be a production host
HOST="${BS_PF_HOSTNAME:-$(hostname 2>/dev/null)}"
for p in $PROD_HOSTS; do
    if [ "$HOST" = "$p" ]; then bad "hostname '$HOST' is a PRODUCTION host - refusing"; fi
done
[ "$FAIL" = "0" ] && ok "hostname '$HOST' is not a listed production host"

# CloudPanel / production websites mean this is the shared production box
if [ -d "$ROOT/home/clp" ] || [ -d "$ROOT/etc/cloudpanel" ] || [ -d "$ROOT/usr/local/clp" ] || \
   [ -e "$ROOT/etc/systemd/system/clp-agent.service" ] || [ -e "$ROOT/etc/nginx/sites-enabled/cloudpanel" ]; then
    bad "CloudPanel detected - this looks like the production hosting server; refusing"
else
    ok "no CloudPanel markers"
fi

# 3. hardware / OS / disk
CPUS="${BS_PF_CPUS:-$(nproc 2>/dev/null || echo 0)}"
[ "$CPUS" -ge "$MIN_CPUS" ] 2>/dev/null && ok "CPU count $CPUS (>= $MIN_CPUS)" || bad "CPU count $CPUS (< $MIN_CPUS): results would not represent the 4-vCPU target"
MEM="${BS_PF_MEM_KB:-$(awk '/MemTotal/ {print $2}' "$ROOT/proc/meminfo" 2>/dev/null || echo 0)}"
[ "$MEM" -ge "$MIN_MEM_KB" ] 2>/dev/null && ok "RAM ${MEM} kB (>= $MIN_MEM_KB)" || bad "RAM ${MEM} kB (< $MIN_MEM_KB): not an 8 GB class VPS"
VER="$(. "$ROOT/etc/os-release" 2>/dev/null; echo "${ID:-?} ${VERSION_ID:-?}")"
[ "$VER" = "ubuntu 24.04" ] && ok "OS $VER" || bad "OS is '$VER', staging must be Ubuntu 24.04"
FREE="${BS_PF_FREE_KB:-$(df -Pk "$ROOT/var" 2>/dev/null | awk 'NR==2 {print $4}')}"
[ "${FREE:-0}" -ge "$MIN_FREE_KB" ] 2>/dev/null && ok "free disk on /var ${FREE} kB (>= $MIN_FREE_KB)" || bad "free disk on /var ${FREE:-0} kB (< $MIN_FREE_KB)"

# 4. rollback strategy
if [ -n "$SNAPSHOT" ] && [ "${#SNAPSHOT}" -ge 4 ]; then
    ok "provider snapshot recorded: $SNAPSHOT (restore this to roll back)"
else
    bad "no snapshot recorded: take a provider snapshot, then pass --snapshot-id <id>"
fi

# 5. SSH lock-out safety (we never touch sshd/firewall; verify it is up and a key is installed)
if [ -f "$ROOT/etc/ssh/sshd_config" ] || [ -n "${BS_PF_ROOT:-}" ]; then ok "sshd config present (this suite never edits SSH or firewall rules)"; fi
if [ -z "${BS_PF_ROOT:-}" ]; then
    systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null \
        && ok "ssh service active" || bad "ssh service not active - keep a second console/recovery path before continuing"
fi

# 6. test media / destinations reminder (cannot be verified automatically)
LINES+=("NOTE  use TEST media and a TEST/unlisted destination or a local RTMP sink only; never a production stream key")

mkdir -p "$REPORT_DIR" 2>/dev/null || true
{
    echo "studio staging preflight  $(date -u +%FT%TZ)  host=$HOST"
    printf '%s\n' "${LINES[@]}"
    [ "$FAIL" = "0" ] && echo "RESULT: ALL CHECKS PASSED" || echo "RESULT: BLOCKED"
} | tee "$REPORT_DIR/preflight.txt"
exit "$FAIL"
