#!/bin/sh
# End-to-end check of the customer-run runner against a staging control plane.
#
#   run_e2e.sh up        private network, synthetic app, egress proxy
#   run_e2e.sh publish   teach, admit and publish the program (author side)
#   run_e2e.sh runner    start the connector (needs $STATE/connector.toml)
#   run_e2e.sh down      remove everything
#
# Topology: the app and the runner sit on an --internal network with no route
# out. The only way out is the egress proxy, which logs every request in full
# and refuses any host not on EGRESS_ALLOWED_HOSTS. The app's name resolves
# only inside the private network. Synthetic data only.
set -eu
CTR="${CTR:-docker}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${STATE:?set STATE to a durable directory}"
APP_HOST="${APP_HOST:-notes.byoc-pilot.openadapt.ai}"
CLOUD_HOST="${CLOUD_HOST:-openadapt-cloud-staging.netlify.app}"
RUNNER_IMAGE="${RUNNER_IMAGE:-openadapt-connector:pilot-dev}"
PUBLISHER_IMAGE="${PUBLISHER_IMAGE:-byoc-publisher:3.1.0}"
PROXY_IMAGE="${PROXY_IMAGE:-mitmproxy/mitmproxy:11.1.3}"
PROXY="http://egress-proxy:8080"

up() {
  mkdir -p "$STATE/certs" "$STATE/mitm" "$STATE/log" "$STATE/notes-data" "$STATE/ca"
  [ -f "$STATE/certs/ca.crt" ] || "$HERE/gen_certs.sh" "$STATE/certs" "$APP_HOST"
  [ -f "$STATE/secrets/notes-reader.env" ] || {
    mkdir -p "$STATE/secrets"
    printf 'NOTES_READER_KEY=ZQX-CANARY-SECRET-%s\n' "$(openssl rand -hex 8)" > "$STATE/secrets/notes-reader.env"
    chmod 600 "$STATE/secrets/notes-reader.env"
  }
  $CTR network inspect byoc-private >/dev/null 2>&1 || $CTR network create --internal byoc-private >/dev/null
  $CTR network inspect byoc-egress >/dev/null 2>&1 || $CTR network create byoc-egress >/dev/null
  $CTR rm -f byoc-notes byoc-egress-proxy >/dev/null 2>&1 || true
  $CTR run -d --name byoc-notes --network byoc-private --network-alias "$APP_HOST" \
    --env-file "$STATE/secrets/notes-reader.env" \
    -e NOTES_CERT=/certs/notes.crt -e NOTES_KEY=/certs/notes.key -e NOTES_STORE=/data/notes.json \
    -v "$STATE/certs:/certs:ro" -v "$STATE/notes-data:/data" -v "$HERE/notes_app.py:/app/notes_app.py:ro" \
    python:3.12-slim-bookworm python /app/notes_app.py >/dev/null
  $CTR run -d --name byoc-egress-proxy --network byoc-egress \
    -e EGRESS_ALLOWED_HOSTS="$CLOUD_HOST" -e EGRESS_LOG=/log/egress.jsonl \
    -v "$STATE/mitm:/home/mitmproxy/.mitmproxy" -v "$STATE/log:/log" -v "$HERE/egress_log.py:/addon/egress_log.py:ro" \
    "$PROXY_IMAGE" mitmdump --listen-port 8080 -s /addon/egress_log.py --set block_global=false >/dev/null
  $CTR network connect --alias egress-proxy byoc-private byoc-egress-proxy
  i=0; until [ -f "$STATE/mitm/mitmproxy-ca-cert.pem" ] || [ $i -gt 30 ]; do i=$((i+1)); sleep 1; done
  cp "$STATE/certs/ca.crt" "$STATE/ca/app-ca.crt"
  cp "$STATE/mitm/mitmproxy-ca-cert.pem" "$STATE/ca/egress-proxy-ca.crt"
  echo "up: app at https://$APP_HOST (private only), egress via $PROXY"
}

publish() {
  mkdir -p "$STATE/work"
  cp "$HERE/teach_admit.py" "$STATE/work/teach_admit.py"
  $CTR run --rm --name byoc-publisher --network byoc-private \
    -e HTTPS_PROXY="$PROXY" -e HTTP_PROXY="$PROXY" -e NO_PROXY="$APP_HOST" \
    -e NOTES_ORIGIN="https://$APP_HOST" \
    --env-file "$STATE/secrets/notes-reader.env" \
    --env-file "$STATE/publisher.env" \
    -v "$STATE/ca:/etc/openadapt/ca:ro" -v "$STATE/work:/work" -w /work \
    "$PUBLISHER_IMAGE" sh -c "${PUBLISH_CMD:?set PUBLISH_CMD}"
}

runner() {
  $CTR rm -f byoc-runner >/dev/null 2>&1 || true
  $CTR run -d --name byoc-runner --network byoc-private \
    --read-only --tmpfs /tmp --tmpfs /home/openadapt:uid=10001,gid=10001,mode=0700 \
    -e HTTPS_PROXY="$PROXY" -e HTTP_PROXY="$PROXY" -e NO_PROXY="$APP_HOST" \
    -e OPENADAPT_HOME=/etc/openadapt \
    --mount "type=bind,source=$STATE/runner-ledger,target=/var/lib/openadapt/ledger" \
    -v "$STATE/runner-etc:/etc/openadapt" -v "$STATE/runner-storage:/srv/openadapt" \
    "$RUNNER_IMAGE" ${RUNNER_CMD:-openadapt-flow connector run} >/dev/null
  echo "runner started; logs: $CTR logs -f byoc-runner"
}

down() {
  $CTR rm -f byoc-runner byoc-publisher byoc-notes byoc-egress-proxy >/dev/null 2>&1 || true
  $CTR network rm byoc-private byoc-egress >/dev/null 2>&1 || true
}

"${1:?up|publish|runner|down}"
