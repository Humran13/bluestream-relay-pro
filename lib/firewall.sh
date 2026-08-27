#!/usr/bin/env bash
# BlueStream Relay Pro - UFW firewall management.
#
# BlueStream Relay Pro 0.1.0 (foundation). See LICENSE for terms.

if [ -n "${BLUESTREAM_FIREWALL_LOADED:-}" ]; then
    return 0 2>/dev/null || exit 0
fi
BLUESTREAM_FIREWALL_LOADED=1

firewall_status() {
    if bs_have_cmd ufw; then
        ufw status verbose
    else
        bs_warn "ufw is not installed on this system."
        return 1
    fi
}

firewall_configure() {
    bs_require_root
    if ! bs_have_cmd ufw; then
        bs_warn "ufw is not installed. Install it with: apt-get install -y ufw"
        return 1
    fi
    local ssh_port="22"
    bs_prompt ssh_port "SSH port to keep open (IMPORTANT: do not lock yourself out)" "22" || return 1
    case "$ssh_port" in ''|*[!0-9]*) bs_error "Invalid port."; return 1 ;; esac

    bs_confirm "Enable UFW allowing SSH (${ssh_port}/tcp), HTTP (80/tcp), HTTPS (443/tcp)?" || { bs_info "Cancelled."; return 1; }

    ufw allow "${ssh_port}/tcp"
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
    bs_step "UFW status:"
    ufw status verbose
    BLUESTREAM_FIREWALL="ufw"
    bs_save_server_conf
    bs_ok "UFW configured and enabled."
    return 0
}
