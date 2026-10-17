# openadapt-flow on-prem runner image — built OFFLINE from a local wheelhouse.
#
# Air-gapped hosts do not reach PyPI. Build this on a connected staging host (or
# with a pre-populated ./wheels) and copy the resulting image tar into the
# clinic via removable media (`docker save` / `docker load`).
#
# Prepare the wheelhouse off-site:
#   pip download 'openadapt-flow[privacy]' -d deploy/on-prem/wheels
# Then build (context = repo root, per docker-compose.yml):
#   docker build -f deploy/on-prem/Dockerfile -t openadapt-flow-onprem:local .
#
# [STUB] the ./wheels wheelhouse is NOT committed (it is large and version-
# specific). Without it the build fails LOUDLY rather than reaching the network.

FROM python:3.11-slim

# Never let pip reach the internet from inside the image build.
ENV PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENADAPT_FLOW_SCRUB=on

WORKDIR /opt/openadapt-flow

# Copy the offline wheelhouse (staged off-site) and the deploy scripts.
COPY deploy/on-prem/wheels /tmp/wheels
COPY deploy/on-prem /opt/openadapt-flow/deploy/on-prem

# Install the engine + privacy extra from local wheels only.
RUN pip install --no-index --find-links /tmp/wheels 'openadapt-flow[privacy]' \
    && rm -rf /tmp/wheels

# Storage root is a bind-mounted, host-encrypted volume at runtime.
WORKDIR /srv/openadapt
ENV PATH="/opt/openadapt-flow/deploy/on-prem:${PATH}"

# Default: process the local job queue. Overridden by docker-compose command.
CMD ["deploy/on-prem/bin/run-queue.sh", "watch"]
