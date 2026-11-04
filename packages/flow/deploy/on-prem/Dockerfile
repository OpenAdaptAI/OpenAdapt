# openadapt-flow on-prem runner image, built with no network from a signed
# release archive (bin/build-release.sh).
#
# Do not run `docker build` on this file directly. bin/build-image.sh checks the
# archive's signature against the pinned vendor key, extracts it, lays out the
# build context below and builds with `--network none`, so pip can only use
# the release's own wheels (the ones pinned by uv.lock):
#
#   release/   the extracted release: VERSION, wheels/, requirements.lock.txt
#   on-prem/   this directory (scripts, units, this Dockerfile)
#
# The base image must already be in the local image store (`docker load` it
# on an air-gapped host). Its default is pinned by digest; the release must be
# built for the same Python minor version and CPU architecture (see
# build-release.sh). Move the result in with `docker save` / `docker load`.

ARG BASE_IMAGE=python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
FROM ${BASE_IMAGE}

ENV PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENADAPT_FLOW_SCRUB=on

# The same install as install.sh (lib-release.sh rel_build_release): only the
# release's wheels, the exact version the archive declares, then a dependency
# consistency check.
COPY release /tmp/release
RUN set -eu; \
    version="$(cat /tmp/release/VERSION)"; \
    python -m venv /opt/openadapt-flow/venv; \
    /opt/openadapt-flow/venv/bin/pip install --no-index --only-binary=:all: \
        --find-links /tmp/release/wheels "openadapt-flow[privacy]==${version}"; \
    /opt/openadapt-flow/venv/bin/pip check; \
    printf '%s\n' "$version" > /opt/openadapt-flow/VERSION; \
    rm -rf /tmp/release

COPY on-prem /opt/openadapt-flow/deploy/on-prem

ENV PATH="/opt/openadapt-flow/venv/bin:/opt/openadapt-flow/deploy/on-prem/bin:${PATH}"

# Storage root is a bind-mounted, host-encrypted volume at runtime.
WORKDIR /srv/openadapt

# Absolute path: the working directory is the storage volume, not the scripts.
CMD ["/opt/openadapt-flow/deploy/on-prem/bin/run-queue.sh", "watch"]
