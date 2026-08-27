#!/usr/bin/env bash
# BlueStream Relay Pro - OS detection helpers.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_OSDETECT_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_OSDETECT_LOADED=1

OS_ID="unknown"
OS_NAME=""
OS_VERSION=""
OS_VERSION_ID=""
OS_LIKE=""

os_detect() {
    OS_ID="unknown"
    OS_NAME=""
    OS_VERSION=""
    OS_VERSION_ID=""
    OS_LIKE=""
    if [ -r /etc/os-release ]; then
        # /etc/os-release is a trusted, root-owned, read-only file shipped by
        # the distribution itself; sourcing it is safe and standard practice.
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_NAME="${NAME:-}"
        OS_VERSION="${VERSION:-}"
        OS_VERSION_ID="${VERSION_ID:-}"
        OS_LIKE="${ID_LIKE:-}"
    fi
}

os_is_ubuntu() {
    [ "$OS_ID" = "ubuntu" ] && return 0
    case " $OS_LIKE " in
        *" ubuntu "*) return 0 ;;
    esac
    return 1
}

os_is_debian() {
    [ "$OS_ID" = "debian" ]
}

os_version_supported() {
    case "$OS_VERSION_ID" in
        20.04|22.04|24.04|26.04|18.04) return 0 ;;
    esac
    return 1
}

os_ubuntu_major() {
    case "$OS_VERSION_ID" in
        20.04) printf '20' ;;
        22.04) printf '22' ;;
        24.04) printf '24' ;;
        26.04) printf '26' ;;
        18.04) printf '18' ;;
        *)     printf '%s' "${OS_VERSION_ID%%.*}" ;;
    esac
}

os_have_systemd() {
    [ -d /run/systemd/system ] || command -v systemctl >/dev/null 2>&1
}

os_have_apt() {
    command -v apt-get >/dev/null 2>&1
}

os_pkg_installed() {
    dpkg -s "$1" >/dev/null 2>&1
}

os_apt_update() {
    bs_require_root
    os_have_apt || bs_die "apt-get not available; cannot update package metadata"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
}

os_pkg_install() {
    bs_require_root
    os_have_apt || bs_die "apt-get not available; cannot install packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends "$@"
}
