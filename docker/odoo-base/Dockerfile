# Odoo PaaS base image - one shared image for every branch container.
#
# Contents
#   /opt/odoo   Odoo core (git checkout of the requested version)
#   system-wide Python packages from Odoo's requirements.txt, so that the
#   per-branch venv (created with --system-site-packages at runtime) only ever
#   installs the repository's own extra requirements.
#   user 'odoo' (uid/gid 1000) - containers run as this user; the platform
#   chowns host mount directories to 1000:1000 (see docker_ctl.py).
#
# At runtime the platform bind-mounts:
#   {branch}/repo      -> /mnt/user       (working dir, custom addons)
#   {branch}/filestore -> /mnt/filestore  (Odoo --data-dir)
#   {branch}/venv      -> /mnt/venv       (per-branch venv)
#
# Build:
#   docker build -t odoo-paas/base:17.0 docker/odoo-base
#   docker build --build-arg ODOO_VERSION=16.0 -t odoo-paas/base:16.0 docker/odoo-base
#
# Odoo >= 20 requires Python >= 3.12, so install.sh builds those series from
# the official python image instead of Debian's Python 3.11:
#   docker build --build-arg BASE_IMAGE=python:3.12-slim-bookworm ...
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG ODOO_VERSION=17.0
# Optional: URL of a wkhtmltopdf .deb for this architecture (needed on arm64 -
# Debian publishes none; community builds exist). Empty = skip PDF support.
ARG WKHTMLTOPDF_DEB=""

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Runtime libraries + build dependencies + Node tooling (asset compilation)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
        python3 python3-dev python3-pip python3-venv \
        build-essential zlib1g-dev \
        libpq-dev libldap2-dev libsasl2-dev libxml2-dev libxslt1-dev \
        libjpeg-dev libfreetype6-dev libpng-dev libtiff-dev libopenjp2-7-dev libzip-dev \
        libpq5 libldap-2.5-0 libsasl2-2 libxml2 libxslt1.1 libjpeg62-turbo \
        libfreetype6 libpng16-16 libtiff6 libopenjp2-7 libzip4 \
        nodejs npm fonts-noto \
    && rm -rf /var/lib/apt/lists/*

# wkhtmltopdf (Odoo PDF reports): distro package when available (amd64),
# otherwise install the .deb given via --build-arg WKHTMLTOPDF_DEB=URL.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends wkhtmltopdf \
        || { [ -n "$WKHTMLTOPDF_DEB" ] \
             && curl -fsSL "$WKHTMLTOPDF_DEB" -o /tmp/wkhtmltox.deb \
             && apt-get install -y --no-install-recommends /tmp/wkhtmltox.deb; } \
        || echo "wkhtmltopdf unavailable on $(dpkg --print-architecture): PDF report printing disabled") \
    && rm -f /tmp/wkhtmltox.deb \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit less less-plugin-clean-css rtlcss \
    && rm -rf /root/.npm

# Odoo core pinned to a release branch
RUN git clone --depth 1 --branch "${ODOO_VERSION}" https://github.com/odoo/odoo.git /opt/odoo \
    && pip3 install --break-system-packages --no-cache-dir -r /opt/odoo/requirements.txt

# Container user (uid/gid 1000 - must match CONTAINER_UID in app/services/docker_ctl.py)
RUN useradd --create-home --uid 1000 --shell /bin/bash odoo

WORKDIR /opt/odoo

# Default command is replaced by the platform (see docker_ctl.server_command).
CMD ["bash"]
