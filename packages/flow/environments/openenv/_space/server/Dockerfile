# OpenEnv Space image for one OpenAdapt environment. Build context: the staged
# environment directory (environments/openenv/stage.py writes it).
#
# The base image is Microsoft's Playwright image (Apache-2.0). Upstream
# application images (OpenEMR, openIMIS, Frappe) are never built into this
# image: the external environments connect to an app the operator starts from
# upstream images pulled by digest.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ARG FLOW_REQUIREMENT="openadapt-flow[openenv]"
WORKDIR /app
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --find-links /tmp/wheels \
        "$FLOW_REQUIREMENT" "playwright==1.55.0" \
    && rm -rf /tmp/wheels
COPY . /app/env
RUN rm -rf /app/env/wheels
ENV OPENADAPT_ENV_DIR=/app/env PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "openadapt_flow.rl_env.server", "/app/env", "--host", "0.0.0.0", "--port", "8000"]
