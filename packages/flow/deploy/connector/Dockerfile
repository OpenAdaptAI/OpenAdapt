# OpenAdapt connector (customer-run runner) for Linux hosts.
#
# The container makes outbound HTTPS requests only and listens on no port.
# Build it from a pinned, signed Flow release wheel placed in ./wheels:
#
#   mkdir -p wheels && cp openadapt_flow-<version>-py3-none-any.whl wheels/
#   docker build -t openadapt-connector:<version> .
#
# Run it with three mounts (see docker-compose.yml): /etc/openadapt holds
# connector.toml (0600, the connector token) and the local secret store;
# /srv/openadapt is the encrypted customer storage volume (programs in, reports
# out); nothing else is written outside the container's tmpfs.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENADAPT_HOME=/etc/openadapt \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libnss3-tools libatomic1 tini

COPY wheels /tmp/wheels
RUN set -eu; \
    python -m venv /opt/openadapt; \
    wheel="$(ls /tmp/wheels/openadapt_flow-*.whl | head -1)"; \
    /opt/openadapt/bin/pip install --no-cache-dir --find-links /tmp/wheels "${wheel}[browser,vision]"; \
    apt-get update; /opt/openadapt/bin/python -m playwright install --with-deps chromium; \
    /opt/openadapt/bin/pip check; \
    rm -rf /tmp/wheels /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 openadapt \
 && mkdir -p /etc/openadapt /srv/openadapt \
 && chown openadapt:openadapt /srv/openadapt
COPY entrypoint.sh /usr/local/bin/openadapt-connector-entrypoint
RUN chmod 0755 /usr/local/bin/openadapt-connector-entrypoint

USER openadapt
ENV PATH="/opt/openadapt/bin:${PATH}"
WORKDIR /srv/openadapt
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/openadapt-connector-entrypoint"]
CMD ["openadapt-flow", "connector", "run"]
