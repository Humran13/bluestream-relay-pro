#!/usr/bin/env bash
# BlueStream Studio - GUARDED production-canary installer wrapper.
#
# It adds guard rails around the EXISTING installer (install.sh); it does not reimplement it.
# DEFAULT IS A DRY RUN: it only prints what it would do. Nothing changes without --execute AND the exact confirmation phrase.
#
#   sudo bash tools/canary/canary_install.sh --snapshot-id "<provider snapshot id>" --vendor-pubkey <64 hex chars>
#   sudo bash tools/canary/canary_install.sh --snapshot-id "<id>" --vendor-pubkey <hex> --execute --confirm "INSTALL BLUESTREAM CANARY ON <this hostname>"
#
# Guards: root only; the read-only canary preflight must report no blockers; a provider snapshot id must be given;
# the confirmation phrase must name THIS hostname. It never passes --with-ufw or --with-ssl (no firewall, no certificates),
# never enables browser capture, never starts a stream, never restarts nginx or any non-BlueStream service.
# BlueStream's own installer is idempotent and, on a CloudPanel host, uses its separate loopback-only nginx.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFLIGHT="${BS_CANARY_PREFLIGHT:-$HERE/canary_preflight.sh}"
INSTALLER="${BS_CANARY_INSTALLER:-$REPO/install.sh}"
ROOTFS="${BS_CANARY_ROOT:-}"              # test hook only
EXECUTE=0; SNAPSHOT=""; PUBKEY=""; CONFIRM=""

die() { printf 'REFUSING: %s\n' "$*" >&2; exit 1; }
while [ $# -gt 0 ]; do
    case "$1" in
        --snapshot-id)   SNAPSHOT="${2:-}"; shift 2 ;;
        --vendor-pubkey) PUBKEY="${2:-}"; shift 2 ;;
        --execute)       EXECUTE=1; shift ;;
        --confirm)       CONFIRM="${2:-}"; shift 2 ;;
        -h|--help)       sed -n 2,16p "$0"; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

HOST="$(hostname 2>/dev/null)"
PHRASE="INSTALL BLUESTREAM CANARY ON $HOST"

[ "${#SNAPSHOT}" -ge 4 ] || die "give --snapshot-id with the id/name of the provider snapshot you just took (rollback point)"
printf '%s' "$PUBKEY" | grep -Eq '^[0-9a-fA-F]{64}$' || die "--vendor-pubkey must be the 64-hex-character PUBLIC key printed by 'studio_license.py keygen' on YOUR PC (never the private key)"
[ -f "$INSTALLER" ] || die "installer not found: $INSTALLER"
if [ "$EXECUTE" = 1 ]; then
    [ "$(id -u)" = "0" ] || die "--execute must run as root (sudo)"
    [ "$CONFIRM" = "$PHRASE" ] || die "confirmation phrase mismatch. It must be exactly: $PHRASE"
fi

printf '== Step 1: read-only canary preflight\n'
if ! bash "$PREFLIGHT"; then
    die "the canary preflight reported blockers (above). Nothing was changed."
fi

COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
PUBFILE="$ROOTFS/usr/local/lib/bluestream/studio/vendor_pubkey.hex"
DROPDIR="$ROOTFS/etc/systemd/system/bluestream-web.service.d"
printf '\n== Step 2: plan (%s)\n' "$([ "$EXECUTE" = 1 ] && echo EXECUTING || echo "dry run, nothing is changed")"
cat <<EOF
  repo commit      : $COMMIT
  host             : $HOST
  provider snapshot: $SNAPSHOT
  a) bash install.sh --non-interactive --no-ufw --no-ssl       (existing installer; no firewall, no certificates; isolated nginx on CloudPanel hosts)
  b) write the vendor PUBLIC key  -> $PUBFILE
  c) enable the Studio feature    -> $DROPDIR/studio.conf   (Environment=BLUESTREAM_STUDIO_ENABLED=1; browser capture stays OFF)
  d) systemctl daemon-reload, then restart ONLY bluestream-web.service
  e) write a receipt              -> /var/lib/bluestream/canary-install.json
  NOT done: no firewall/SSH/SSL changes, no nginx restart, no Docker action, no stream started, no stream key handled.
EOF
if [ "$EXECUTE" != 1 ]; then
    printf '\nDry run complete. To install, re-run with:  --execute --confirm "%s"\n' "$PHRASE"
    exit 0
fi

printf '\n== Step 3: running the existing installer\n'
bash "$INSTALLER" --non-interactive --no-ufw --no-ssl || die "install.sh failed; see its output. Rollback: tools/canary/canary_rollback.sh (dry run first)"

printf '\n== Step 4: enable Studio (BlueStream-only changes)\n'
mkdir -p "$ROOTFS/usr/local/lib/bluestream/studio" "$DROPDIR" || die "cannot create directories"
printf '%s\n' "$PUBKEY" > "$PUBFILE.tmp" && chmod 0644 "$PUBFILE.tmp" && mv -f "$PUBFILE.tmp" "$PUBFILE" || die "cannot write the vendor public key"
printf '[Service]\nEnvironment=BLUESTREAM_STUDIO_ENABLED=1\n' > "$DROPDIR/studio.conf" && chmod 0644 "$DROPDIR/studio.conf" || die "cannot write the Studio drop-in"
systemctl daemon-reload || die "daemon-reload failed"
systemctl restart bluestream-web.service || die "bluestream-web did not restart; run: journalctl -u bluestream-web -n 50 --no-pager"
sleep 2
systemctl is-active --quiet bluestream-web.service || die "bluestream-web is not active after restart"

mkdir -p "$ROOTFS/var/lib/bluestream" 2>/dev/null
printf '{"installed_at":"%s","host":"%s","commit":"%s","snapshot_id":"%s","mode":"production-canary","browser_capture":"disabled"}\n' \
    "$(date -u +%FT%TZ)" "$HOST" "$COMMIT" "$(printf '%s' "$SNAPSHOT" | tr -cd 'A-Za-z0-9._ -')" > "$ROOTFS/var/lib/bluestream/canary-install.json" 2>/dev/null

WEBPORT="$(awk -F= '/^WEB_PORT=/ {gsub(/"/,"",$2); print $2}' "$ROOTFS/etc/bluestream/server.conf" 2>/dev/null)"
cat <<EOF

== INSTALLED (BlueStream only). No stream has been started.
Next steps (see docs/bluestream/CANARY.md):
  1. Create the console admin password:   sudo bluestream-manager admin init
  2. From YOUR PC open an SSH tunnel:     ssh -L 8765:127.0.0.1:${WEBPORT:-<WEB_PORT>} <your-user>@<this-server>
     then browse to  http://localhost:8765/console   (nothing is exposed publicly)
  3. Create a licence token on YOUR PC (tools/studio_license.py issue ...) and paste it under Studio -> Licence.
EOF
