#!/usr/bin/env bash
#
# BlueStream Relay Pro - public one-line bootstrap installer.
#
#   curl -fsSL https://raw.githubusercontent.com/Humran13/bluestream-relay-pro/main/bootstrap.sh | sudo bash
#
# This script does exactly four things:
#   1. Verifies it is running as root on a supported Ubuntu host (advisory
#      here; install.sh re-checks authoritatively).
#   2. Installs the small set of bootstrap prerequisites (git, ca-certificates)
#      if they are missing.
#   3. Clones (or safely updates an existing checkout of) the PUBLIC
#      BlueStream Relay Pro repository into a stable source directory -
#      NEVER into /etc/bluestream, /var/lib/bluestream, /var/www/bluestream or
#      /usr/local/lib/bluestream, so no existing relay/playlist/destination/
#      media/schedule/backup data or installed runtime is ever touched by this
#      script, ever.
#   4. Runs the checkout's own install.sh (the authoritative installer),
#      forwarding any arguments this script was given.
#
# Extra arguments after the script are passed straight to install.sh, e.g.:
#   curl -fsSL .../bootstrap.sh | sudo bash -s -- --domain video.example.com --assume-yes
#
# Safety:
#   - No secondary URL is ever piped into a shell; only this one fixed
#     GitHub raw URL is documented, and the clone target is a fixed,
#     hard-coded repository URL (never taken from the environment or args).
#   - Never runs `rm -rf` on anything outside the source checkout directory.
#   - "Verifies the downloaded repository" here means a structural sanity
#     check (install.sh, VERSION and lib/common.sh exist and are plausible)
#     before install.sh is invoked - this project does not currently publish
#     signed release artifacts, so no cryptographic signature check is
#     performed. Anyone relying on supply-chain integrity beyond GitHub's own
#     HTTPS/TLS transport should clone and audit the repository manually.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.
set -u

REPO_URL="https://github.com/Humran13/bluestream-relay-pro.git"
REPO_BRANCH="main"
SRC_DIR="/usr/local/src/bluestream-relay-pro"

log()  { printf '[*] %s\n' "$1"; }
ok()   { printf '[OK] %s\n' "$1"; }
err()  { printf '[ERR] %s\n' "$1" >&2; }
die()  { err "$1"; exit 1; }

# ---------------------------------------------------------------------------
# 1. Root + OS check (advisory; install.sh is authoritative).
# ---------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "bootstrap.sh must run as root. Re-run as: curl -fsSL <url> | sudo bash"

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" != "ubuntu" ]; then
        err "BlueStream Relay Pro targets Ubuntu Server. Detected ID='${ID:-unknown}'."
        die "Installation aborted. Run install.sh directly with your own judgment if you want to continue anyway."
    fi
    case "${VERSION_ID:-}" in
        20.04|22.04|24.04|26.04) ok "Detected supported Ubuntu ${VERSION_ID} (${PRETTY_NAME:-})" ;;
        *) log "Detected Ubuntu ${VERSION_ID:-unknown} (not on the confirmed-supported list; install.sh will re-check)" ;;
    esac
else
    err "Cannot read /etc/os-release; this does not look like a standard Ubuntu host."
    die "Installation aborted."
fi

command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This installer requires Ubuntu."
command -v systemctl >/dev/null 2>&1 || die "systemd not detected. This installer requires systemd."

# ---------------------------------------------------------------------------
# 2. Bootstrap prerequisites (git + ca-certificates + curl). Idempotent.
# ---------------------------------------------------------------------------
NEED_UPDATE=0
for pkg_check in git ca-certificates; do
    case "$pkg_check" in
        git) command -v git >/dev/null 2>&1 || NEED_UPDATE=1 ;;
        ca-certificates) [ -d /etc/ssl/certs ] || NEED_UPDATE=1 ;;
    esac
done
command -v curl >/dev/null 2>&1 || NEED_UPDATE=1

if [ "$NEED_UPDATE" = "1" ]; then
    log "Installing bootstrap prerequisites (git, curl, ca-certificates)..."
    apt-get update -qq || die "apt-get update failed. Check network/DNS and try again."
    apt-get install -y -qq git curl ca-certificates || die "Failed to install bootstrap prerequisites."
fi
command -v git >/dev/null 2>&1 || die "git is still not available after installation attempt."
ok "Bootstrap prerequisites available (git, curl, ca-certificates)"

# ---------------------------------------------------------------------------
# 3. Clone or safely update the source checkout.
#
# This directory holds ONLY the git checkout of the public repository - it is
# never the installed runtime (/usr/local/lib/bluestream) and never holds any
# relay/playlist/destination/media/schedule/backup/web-admin state, so
# replacing its contents here can never destroy BlueStream data.
# ---------------------------------------------------------------------------
if [ -d "$SRC_DIR/.git" ]; then
    log "Existing source checkout found at $SRC_DIR; updating..."
    if ! git -C "$SRC_DIR" remote get-url origin 2>/dev/null | grep -qF "bluestream-relay-pro"; then
        die "$SRC_DIR exists but is not a checkout of this project. Move it aside and re-run, or clone manually."
    fi
    git -C "$SRC_DIR" fetch --depth 1 origin "$REPO_BRANCH" || die "git fetch failed. Check network/DNS and try again."
    git -C "$SRC_DIR" checkout -q "$REPO_BRANCH" 2>/dev/null || true
    git -C "$SRC_DIR" reset --hard "origin/$REPO_BRANCH" || die "git reset failed."
    ok "Source checkout updated to the latest $REPO_BRANCH"
elif [ -e "$SRC_DIR" ]; then
    die "$SRC_DIR already exists and is not a git checkout. Move it aside (it is never BlueStream runtime data) and re-run."
else
    log "Cloning $REPO_URL into $SRC_DIR..."
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$SRC_DIR" \
        || die "git clone failed. Check network/DNS and that the repository is reachable, then try again."
    ok "Cloned $REPO_URL ($REPO_BRANCH) into $SRC_DIR"
fi

# ---------------------------------------------------------------------------
# Structural sanity check before handing off to install.sh (see the Safety
# note at the top of this file for exactly what this does and does not cover).
# ---------------------------------------------------------------------------
for required in install.sh VERSION lib/common.sh webapp/app.py; do
    [ -f "$SRC_DIR/$required" ] || die "Downloaded checkout is missing '$required'; refusing to run an incomplete/corrupt checkout."
done
bash -n "$SRC_DIR/install.sh" || die "Downloaded install.sh failed a basic syntax check; refusing to run it."
ok "Checkout passed structural sanity checks"

# ---------------------------------------------------------------------------
# 4. Hand off to the authoritative installer.
# ---------------------------------------------------------------------------
log "Running install.sh..."
cd "$SRC_DIR" || die "Could not enter $SRC_DIR"
exec bash install.sh "$@"
