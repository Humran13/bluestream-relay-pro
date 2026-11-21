#!/usr/bin/env bash
# BlueStream Relay Pro + Studio - Docker deployment CLI (host side). Every command is scoped to ONE Compose project
# (default "bluestream"); it can only see and change that project's own container, network, volumes and image tag.
# It never touches other containers, `docker system prune`, the host nginx/apt/systemd/firewall/SSH/certificates.
#
#   bash docker/bluestream.sh preflight [--project NAME] [--http-port N] [--cpus 2.0] [--mem 2g]     read-only
#   bash docker/bluestream.sh up        [same flags] [--vendor-pubkey HEX] [--execute --confirm "INSTALL BLUESTREAM DOCKER NAME"]
#   bash docker/bluestream.sh status | url | logs | diag [--since 24h] | admin-password | restart   (all scoped, read-only except restart)
#   bash docker/bluestream.sh down      [--project NAME] [--execute --confirm "REMOVE BLUESTREAM DOCKER NAME"]
#                                       [--purge-volumes --confirm-data-loss "DELETE ALL DATA OF NAME"] [--remove-image]
# `up` and `down` are DRY RUNS unless --execute and the exact confirmation phrase are given.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${BS_COMPOSE_FILE:-$HERE/compose.yml}"
ENV_DIR="${BS_ENV_DIR:-$HOME/.bluestream-docker}"
DOCKER="${BS_DOCKER:-docker}"

die() { printf 'REFUSING: %s\n' "$*" >&2; exit 1; }
CMD="${1:-help}"; [ $# -gt 0 ] && shift
PROJECT="bluestream"; HTTP_PORT=""; CPUS="2.0"; MEM="2g"; PUBKEY=""; EXECUTE=0; CONFIRM=""; CONFIRM_DATA=""; PURGE=0; RMIMG=0; SINCE="6h"; LINES=200
while [ $# -gt 0 ]; do
    case "$1" in
        --project) PROJECT="${2:-}"; shift 2 ;;
        --http-port) HTTP_PORT="${2:-}"; shift 2 ;;
        --cpus) CPUS="${2:-}"; shift 2 ;;
        --mem) MEM="${2:-}"; shift 2 ;;
        --vendor-pubkey) PUBKEY="${2:-}"; shift 2 ;;
        --execute) EXECUTE=1; shift ;;
        --confirm) CONFIRM="${2:-}"; shift 2 ;;
        --confirm-data-loss) CONFIRM_DATA="${2:-}"; shift 2 ;;
        --purge-volumes) PURGE=1; shift ;;
        --remove-image) RMIMG=1; shift ;;
        --since) SINCE="${2:-6h}"; shift 2 ;;
        --lines) LINES="${2:-200}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done
printf '%s' "$PROJECT" | grep -Eq '^[a-z][a-z0-9-]{2,30}$' || die "project name must match ^[a-z][a-z0-9-]{2,30}$"
printf '%s' "$SINCE" | grep -Eq '^[0-9]+[smhd]$' || die "--since must look like 6h or 2d"
printf '%s' "$LINES" | grep -Eq '^[0-9]{1,4}$' || die "--lines must be a number"
CNAME="${PROJECT}-app"
ENVFILE="$ENV_DIR/$PROJECT.env"
IMAGE="${BS_IMAGE:-bluestream-relay-pro:local}"

redact() {
    sed -E \
        -e 's#(rtmps?://[^/[:space:]"'"'"']+/[^/[:space:]"'"'"']+/)[^[:space:]"'"'"']+#\1[REDACTED-KEY]#g' \
        -e 's#(STREAM_KEY|stream[_ -]?key|password|passwd|secret|token|api[_-]?key|authorization|cookie)([[:space:]]*[:=][[:space:]]*)[^[:space:]]+#\1\2[REDACTED]#Ig' \
        -e 's#(Bearer|Basic)[[:space:]]+[A-Za-z0-9._~+/=-]{6,}#\1 [REDACTED]#g' \
        -e 's#BS1R?\.[A-Za-z0-9_.-]+#[REDACTED-LICENCE]#g' \
        -e 's#\b[A-Za-z0-9]{4}(-[A-Za-z0-9]{4}){3,4}\b#[REDACTED-KEY]#g' \
        -e 's#\b[A-Za-z0-9_-]{32,}\b#[REDACTED-TOKEN]#g'
}
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }  # Git Bash on Windows only
dc() { "$DOCKER" compose -p "$PROJECT" -f "$(winpath "$COMPOSE_FILE")" --env-file "$(winpath "$ENVFILE")" "$@"; }
owned_container() { [ "$("$DOCKER" inspect -f '{{index .Config.Labels "com.bluestream.project"}}' "$CNAME" 2>/dev/null)" = "$PROJECT" ]; }
need_owned() { owned_container || die "no BlueStream container '$CNAME' belonging to project '$PROJECT' exists (nothing to do)"; }
saved_port() { [ -f "$ENVFILE" ] && sed -n 's/^BS_HTTP_PORT=//p' "$ENVFILE" | head -n1; }

case "$CMD" in
    help|-h|--help) sed -n 2,14p "$0" ;;

    preflight)
        exec bash "$HERE/preflight.sh" --project "$PROJECT" ${HTTP_PORT:+--http-port "$HTTP_PORT"} --cpus "$CPUS" --mem "$MEM" ;;

    up)
        [ -z "$PUBKEY" ] || printf '%s' "$PUBKEY" | grep -Eq '^[0-9a-fA-F]{64}$' || die "--vendor-pubkey must be the 64-hex PUBLIC key from 'studio_license.py keygen' (never the private key)"
        PHRASE="INSTALL BLUESTREAM DOCKER $PROJECT"
        printf '== Step 1: read-only preflight\n'
        bash "$HERE/preflight.sh" --project "$PROJECT" ${HTTP_PORT:+--http-port "$HTTP_PORT"} --cpus "$CPUS" --mem "$MEM" || die "the preflight reported blockers (above). Nothing was changed."
        if [ -z "$HTTP_PORT" ]; then
            HTTP_PORT="$(saved_port)"
            if [ -z "$HTTP_PORT" ]; then
                for p in 18090 18091 18092 18093 18094 18095 18096 18097 18098 18099; do
                    ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$p\$" || { HTTP_PORT="$p"; break; }
                done
            fi
        fi
        [ -n "$HTTP_PORT" ] || die "no free dashboard port found; pass --http-port"
        printf '\n== Step 2: plan (%s)\n' "$([ "$EXECUTE" = 1 ] && echo EXECUTING || echo "dry run, nothing is changed")"
        cat <<EOF
  project / container : $PROJECT / $CNAME
  dashboard           : http://127.0.0.1:$HTTP_PORT/console   (loopback only; use an SSH tunnel)
  limits              : $CPUS CPU(s), $MEM RAM, 512 processes, read-only root filesystem, all capabilities dropped except 8 (see compose.yml)
  creates             : image '$IMAGE', network '${PROJECT}_net', volumes '${PROJECT}_etc' '${PROJECT}_units' '${PROJECT}_data' '${PROJECT}_www', container '$CNAME'
  env file            : $ENVFILE (no secrets)
  NOT touched         : host nginx/apt/systemd/firewall/SSH/SSL, CloudPanel, other containers/networks/volumes, a native BlueStream install
EOF
        if [ "$EXECUTE" != 1 ]; then
            printf '\nDry run complete. To install: re-run with  --execute --confirm "%s"\n' "$PHRASE"; exit 0
        fi
        [ "$CONFIRM" = "$PHRASE" ] || die "confirmation phrase mismatch; it must be exactly: $PHRASE"
        mkdir -p "$ENV_DIR" && chmod 0700 "$ENV_DIR"
        ( umask 077; printf 'BS_PROJECT=%s\nBS_HTTP_PORT=%s\nBS_CPUS=%s\nBS_MEM=%s\nBS_VENDOR_PUBKEY=%s\nBS_IMAGE=%s\n' "$PROJECT" "$HTTP_PORT" "$CPUS" "$MEM" "$PUBKEY" "$IMAGE" > "$ENVFILE" )
        printf '\n== Step 3: building the image (needs internet; a few minutes the first time)\n'
        dc build || die "image build failed; nothing is running. Rollback is not needed (no container was created)."
        printf '\n== Step 4: starting the container\n'
        dc up -d || die "could not start the project; see: docker compose -p $PROJECT logs. Roll back with: bash docker/bluestream.sh down --project $PROJECT"
        printf 'waiting for the dashboard to become healthy'
        for _ in $(seq 1 60); do
            st="$("$DOCKER" inspect -f '{{.State.Health.Status}}' "$CNAME" 2>/dev/null)"
            [ "$st" = healthy ] && break
            printf '.'; sleep 3
        done
        echo
        [ "$st" = healthy ] || die "the container did not become healthy within 3 minutes. Run: bash docker/bluestream.sh diag --project $PROJECT"
        cat <<EOF

== INSTALLED. No stream has been started and no stream key was handled.
  1. Admin password:   bash docker/bluestream.sh admin-password --project $PROJECT      (username: admin)
  2. From YOUR PC:     ssh -L 8765:127.0.0.1:$HTTP_PORT <your-ssh-user>@<this-server>
                       then open  http://localhost:8765/console   and log in. Studio is a tab in the same dashboard.
  3. Studio -> Licence: paste your licence token (docs/bluestream/DOCKER.md).
EOF
        ;;

    status)
        need_owned
        "$DOCKER" ps -a --filter "label=com.bluestream.project=$PROJECT" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
        "$DOCKER" exec "$CNAME" bs-in-container status ;;

    url)
        p="$(saved_port)"; [ -n "$p" ] || die "no saved port for project '$PROJECT'"
        printf 'dashboard (loopback only): http://127.0.0.1:%s/console\ntunnel: ssh -L 8765:127.0.0.1:%s <user>@<server>  ->  http://localhost:8765/console\n' "$p" "$p" ;;

    logs)
        need_owned
        "$DOCKER" logs --tail "$LINES" "$CNAME" 2>&1 | redact ;;

    admin-password)
        need_owned
        "$DOCKER" exec "$CNAME" bs-in-container admin-password ;;

    diag)
        need_owned
        {
            printf 'BlueStream Docker diagnostics (read-only, redacted)  project=%s  %s\n' "$PROJECT" "$(date -u +%FT%TZ)"
            printf '\n===== container (limits, state, restarts) =====\n'
            "$DOCKER" inspect -f 'status={{.State.Status}} health={{.State.Health.Status}} restarts={{.RestartCount}} oomkilled={{.State.OOMKilled}} exit={{.State.ExitCode}} started={{.State.StartedAt}} privileged={{.HostConfig.Privileged}} readonly_rootfs={{.HostConfig.ReadonlyRootfs}} nanocpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}' "$CNAME" 2>&1
            "$DOCKER" port "$CNAME" 2>&1
            printf '\n===== live usage =====\n'
            "$DOCKER" stats --no-stream --format 'cpu={{.CPUPerc}} mem={{.MemUsage}} ({{.MemPerc}}) net={{.NetIO}} blk={{.BlockIO}} pids={{.PIDs}}' "$CNAME" 2>&1
            printf '\n===== container log tail (entrypoint / unit manager) =====\n'
            "$DOCKER" logs --tail 60 "$CNAME" 2>&1
            printf '\n===== inside the container =====\n'
            "$DOCKER" exec "$CNAME" bs-in-container diag --since "$SINCE" 2>&1
        } | redact
        printf '\nThis command is read-only: it restarted nothing and changed nothing.\n' ;;

    restart)
        need_owned
        "$DOCKER" restart "$CNAME" ;;

    down)
        printf 'BlueStream Docker rollback for project %s (%s)\n' "$PROJECT" "$([ "$EXECUTE" = 1 ] && echo EXECUTING || echo "DRY RUN - nothing is changed")"
        printf '\nThis project owns ONLY:\n'
        "$DOCKER" ps -a --filter "label=com.bluestream.project=$PROJECT" --format '  container {{.Names}} ({{.Status}})'
        "$DOCKER" network ls --filter "label=com.bluestream.project=$PROJECT" --format '  network   {{.Name}}'
        "$DOCKER" volume ls --filter "label=com.bluestream.project=$PROJECT" --format '  volume    {{.Name}}   (KEPT unless --purge-volumes)'
        printf '\nStops/removes the container and network. Keeps the data volumes (media, projects, configs, stream keys) and the image.\n'
        printf 'Never touches: other containers/networks/volumes/images, the host, nginx, firewall, SSH, CloudPanel, a native BlueStream.\n'
        if [ "$EXECUTE" != 1 ]; then
            printf '\nDry run complete. To remove:  --execute --confirm "REMOVE BLUESTREAM DOCKER %s"\n' "$PROJECT"; exit 0
        fi
        [ "$CONFIRM" = "REMOVE BLUESTREAM DOCKER $PROJECT" ] || die "confirmation phrase mismatch; it must be exactly: REMOVE BLUESTREAM DOCKER $PROJECT"
        [ -f "$ENVFILE" ] || die "no env file $ENVFILE for this project (was it installed with this tool?)"
        dc down --remove-orphans || die "compose down failed"
        if [ "$PURGE" = 1 ]; then
            [ "$CONFIRM_DATA" = "DELETE ALL DATA OF $PROJECT" ] || die "--purge-volumes also needs  --confirm-data-loss \"DELETE ALL DATA OF $PROJECT\"  (the volumes hold your stream key and media)"
            for v in etc units data www; do
                vol="${PROJECT}_$v"
                if [ "$("$DOCKER" volume inspect -f '{{index .Labels "com.bluestream.project"}}' "$vol" 2>/dev/null)" = "$PROJECT" ]; then
                    "$DOCKER" volume rm "$vol"
                else
                    printf 'skipping %s (not labelled as this project'"'"'s)\n' "$vol"
                fi
            done
        else
            printf 'data volumes kept: %s_etc %s_units %s_data %s_www\n' "$PROJECT" "$PROJECT" "$PROJECT" "$PROJECT"
        fi
        if [ "$RMIMG" = 1 ]; then "$DOCKER" image rm "$IMAGE" 2>&1 | tail -n1; fi
        printf 'Done. Other containers on this host were not touched.\n' ;;

    *) die "unknown command '$CMD' (try: help)" ;;
esac
