# BlueStream Relay Pro + Studio - self-contained application image (Ubuntu 24.04).
#
# nginx + the RTMP module, FFmpeg, Python/Flask/Gunicorn and the whole BlueStream engine live INSIDE this image.
# Nothing is installed on, or shared with, the host's package manager, nginx, CloudPanel or systemd.
#
# The real, unmodified install.sh is run at BUILD time (so the layout is identical to a native install). systemd does not
# exist in a container, so a tiny unit manager (docker/unitd) provides the `systemctl`/`journalctl` behaviour BlueStream
# relies on. Secrets are NEVER baked in: the console secret key and admin password are created on first start.
FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8

RUN printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/99retry \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl iproute2 procps tzdata fonts-dejavu-core python3 sudo util-linux \
 && rm -rf /var/lib/apt/lists/*

# systemctl/journalctl shims (build runs with no daemon: start/stop are no-ops, enable/disable write boot markers)
COPY docker/unitd/ /usr/local/lib/bluestream/docker/unitd/
RUN chmod 0755 /usr/local/lib/bluestream/docker/unitd/systemctl /usr/local/lib/bluestream/docker/unitd/journalctl \
 && ln -sf /usr/local/lib/bluestream/docker/unitd/systemctl /usr/local/bin/systemctl \
 && ln -sf /usr/local/lib/bluestream/docker/unitd/journalctl /usr/local/bin/journalctl

# nginx has no systemd unit inside a container: provide one so `systemctl enable nginx` (run by the installer) is recorded
COPY docker/units/nginx.service /etc/systemd/system/nginx.service

# The real installer, unchanged: owned-mode nginx (this container's own), no firewall, no SSL, no interactive prompts.
COPY . /opt/bluestream-src
RUN BS_UNITD_OPTIONAL=1 BS_UNITD_STATE=/var/lib/bluestream/units bash /opt/bluestream-src/install.sh --non-interactive --no-ufw --no-ssl \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
 && rm -f /var/lib/bluestream/web/secret_key /var/lib/bluestream/web/admin.json /var/lib/bluestream/web/initial-admin-password

# unit manager + container glue go in AFTER the installer (which owns /usr/local/lib/bluestream/{lib,webapp,...})
COPY docker/unitd/ /usr/local/lib/bluestream/docker/unitd/
COPY docker/units/ /usr/local/lib/bluestream/docker/units/
COPY docker/entrypoint.sh /usr/local/lib/bluestream/docker/entrypoint.sh
COPY docker/bs-in-container /usr/local/bin/bs-in-container
COPY tools/canary/studio_diagnostics.sh /usr/local/lib/bluestream/docker/diag.sh
RUN chmod 0755 /usr/local/lib/bluestream/docker/entrypoint.sh /usr/local/bin/bs-in-container /usr/local/lib/bluestream/docker/diag.sh /usr/local/lib/bluestream/docker/unitd/* \
 && mkdir -p /var/lib/bluestream/unit-logs && chown root:root /var/lib/bluestream/unit-logs && chmod 0750 /var/lib/bluestream/unit-logs \
 && mkdir -p /usr/local/lib/bluestream/docker/units-image && cp -a /etc/systemd/system/*.service /usr/local/lib/bluestream/docker/units-image/ \
 && rm -rf /opt/bluestream-src

ENV BS_UNITD_STATE=/var/lib/bluestream/units BS_UNITD_LOGS=/var/lib/bluestream/unit-logs BS_UNITD_RUN=/run/bsunitd \
    BLUESTREAM_STUDIO_ENABLED=1
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD curl -fsS -o /dev/null http://127.0.0.1/console/login || exit 1
ENTRYPOINT ["/usr/local/lib/bluestream/docker/entrypoint.sh"]
