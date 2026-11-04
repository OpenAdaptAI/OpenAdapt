#!/usr/bin/env bash
#
# test-offline-install.sh — install, upgrade and roll back the real engine on a
# machine with no network.
#
# Unlike test-update.sh, which fakes the venv build, this installs the real
# openadapt-flow[privacy] wheels pinned by uv.lock, so it proves the offline
# release archive is complete: pip gets no index and the machine has no route
# out, so any missing wheel fails the install.
#
#   stage DIR    on a connected host: build two signed releases (this checkout
#                as V1, the same source as V1.post1 as V2) and a throwaway
#                signing key, with bin/build-release.sh.
#   verify DIR   as root, inside bin/run-egress-blocked.sh: first install from
#                V1's wheelhouse, refuse a tampered V2, update to the signed V2,
#                roll back to V1, then run the air-gap attestation with its
#                egress probe and audit-chain walk. Data must survive each step.
#
# CI runs both (packages.yml, flow on-prem offline install). By hand on Linux:
#   bin/test-offline-install.sh stage /tmp/oa-offline
#   sudo env -u SUDO_UID -u SUDO_GID "PATH=$PATH" \
#     bin/run-egress-blocked.sh bin/test-offline-install.sh verify /tmp/oa-offline
#
# Exit 0 when every check passes.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONPREM="$(cd "$HERE/.." && pwd)"
FLOW_DIR="$(cd "$ONPREM/../.." && pwd)"
CMD="${1:-}"
DIR="${2:-}"
if [[ -z "$CMD" || -z "$DIR" || ( "$CMD" != stage && "$CMD" != verify ) ]]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

stage() {
  set -e
  mkdir -p "$DIR/keys" "$DIR/v1" "$DIR/v2"
  DIR="$(cd "$DIR" && pwd)"
  openssl genrsa -out "$DIR/keys/vendor.pem" 2048 2>/dev/null
  openssl rsa -in "$DIR/keys/vendor.pem" -pubout -out "$DIR/keys/vendor.pub" 2>/dev/null
  bash "$HERE/build-release.sh" --out "$DIR/v1" --sign-key "$DIR/keys/vendor.pem" \
    ${OPENADAPT_ONPREM_PY:+--python "$OPENADAPT_ONPREM_PY"}

  # V2: the same source under a new version, built the same way.
  local v1 src
  v1="$(find "$DIR/v1" -maxdepth 1 -name 'release-*.tar.gz' -print)"
  v1="$(basename "$v1" .tar.gz)"
  v1="${v1#release-}"
  src="$(mktemp -d "${TMPDIR:-/tmp}/oa-offline-src.XXXXXX")"
  for item in pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md \
      public-artifacts.json openadapt_flow schemas; do
    cp -R "$FLOW_DIR/$item" "$src/"
  done
  find "$src" -name __pycache__ -type d -prune -exec rm -rf {} +
  sed -i.bak "s/^version = \"$v1\"$/version = \"$v1.post1\"/" "$src/pyproject.toml"
  sed -i.bak "s/^__version__ = \"$v1\"$/__version__ = \"$v1.post1\"/" "$src/openadapt_flow/__init__.py"
  rm -f "$src/pyproject.toml.bak" "$src/openadapt_flow/__init__.py.bak"
  grep -q "^version = \"$v1.post1\"$" "$src/pyproject.toml"
  (cd "$src" && uv build --wheel --out-dir "$src/dist" >/dev/null)
  bash "$HERE/build-release.sh" --out "$DIR/v2" --sign-key "$DIR/keys/vendor.pem" \
    --flow-wheel "$src/dist/openadapt_flow-$v1.post1-py3-none-any.whl" \
    ${OPENADAPT_ONPREM_PY:+--python "$OPENADAPT_ONPREM_PY"}
  rm -rf "$src"
  # The private key never goes near the install.
  rm -f "$DIR/keys/vendor.pem"
  printf '%s\n' "$v1" > "$DIR/V1"
  printf '%s\n' "$v1.post1" > "$DIR/V2"
  echo "staged $v1 and $v1.post1 in $DIR"
}

PASS=0
FAIL=0
ok() { printf '  PASS %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
check_eq() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi; }

verify() {
  DIR="$(cd "$DIR" && pwd)"
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "test-offline-install.sh verify: run as root (install.sh needs it)" >&2; exit 2
  fi
  if [[ "${OPENADAPT_EGRESS_BLOCKED:-}" != netns ]]; then
    echo "test-offline-install.sh verify: run inside bin/run-egress-blocked.sh" >&2; exit 2
  fi
  local v1 v2 py work root config rc
  v1="$(cat "$DIR/V1")"; v2="$(cat "$DIR/V2")"
  py="${OPENADAPT_ONPREM_PY:-python3}"
  work="$(mktemp -d "${TMPDIR:-/tmp}/oa-offline-verify.XXXXXX")"
  root="$work/srv"
  config="$work/onprem.yaml"
  cat > "$config" <<YAML
name: offline-install-test
storage_root: $root
bundles_dir: $root/bundles
runs_dir: $root/runs
jobs_dir: $root/jobs
audit_log: $root/audit/audit.log
deployment_config: $root/deployment.yaml
scrub: on
egress:
  allow_model_grounding: false
  vlm_url: ""
runner:
  service_user: openadapt-offline-test-absent
updates:
  channel: offline
  vendor_pubkey: $root/keys/vendor.pub
  signature_tool: openssl
YAML
  export OPENADAPT_ONPREM_PY="$py"
  export OPENADAPT_ONPREM_AUDIT_LOG="$root/audit/audit.log"
  engine_version() {
    "$root/current/venv/bin/python" -c \
      'import openadapt_flow, presidio_analyzer, openadapt_privacy; print(openadapt_flow.__version__)' 2>&1
  }

  echo "== offline install, upgrade and rollback ($v1 -> $v2 -> $v1), no network =="
  check "the machine has no route out" "$py" -c '
import socket, sys
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
except OSError:
    sys.exit(0)
sys.exit(1)'

  echo "-- first install from the $v1 wheelhouse --"
  mkdir -p "$work/media"
  tar xzf "$DIR/v1/release-$v1.tar.gz" -C "$work/media"
  bash "$ONPREM/install.sh" --config "$config" --wheelhouse "$work/media/release/wheels" \
    > "$work/install.log" 2>&1
  rc=$?
  check_eq "install: exit 0" 0 "$rc"
  [[ "$rc" -eq 0 ]] || tail -n 40 "$work/install.log"
  check_eq "install: current -> $v1" "$root/releases/$v1" "$(readlink "$root/current")"
  check_eq "install: engine and privacy extra import, version $v1" "$v1" "$(engine_version)"
  check "install: engine CLI runs" "$root/current/venv/bin/openadapt-flow" --help

  mkdir -p "$root/bundles" "$root/jobs/inbox"
  printf 'bundle\n' > "$root/bundles/keepme.txt"
  printf 'job\n' > "$root/jobs/inbox/queued-0001.job"
  local bundle_sha job_sha
  bundle_sha="$(shasum -a 256 "$root/bundles/keepme.txt" | awk '{print $1}')"
  job_sha="$(shasum -a 256 "$root/jobs/inbox/queued-0001.job" | awk '{print $1}')"
  cp "$DIR/keys/vendor.pub" "$root/keys/vendor.pub"

  local arc="$DIR/v2/release-$v2.tar.gz"
  echo "-- a tampered $v2 archive is refused --"
  cp "$arc" "$work/media/tampered.tar.gz"
  printf 'x' >> "$work/media/tampered.tar.gz"
  bash "$ONPREM/install.sh" --update --config "$config" --release "$work/media/tampered.tar.gz" \
    --signature "$arc.sig" > "$work/tampered.log" 2>&1
  rc=$?
  check "tampered: update refused" test "$rc" -ne 0
  check_eq "tampered: current still $v1" "$root/releases/$v1" "$(readlink "$root/current")"
  check "tampered: nothing built for $v2" test ! -e "$root/releases/$v2"

  echo "-- signed update to $v2 --"
  bash "$ONPREM/install.sh" --update --config "$config" --release "$arc" \
    --checksum "$arc.sha256" --signature "$arc.sig" > "$work/update.log" 2>&1
  rc=$?
  check_eq "update: exit 0" 0 "$rc"
  [[ "$rc" -eq 0 ]] || tail -n 40 "$work/update.log"
  check_eq "update: current -> $v2" "$root/releases/$v2" "$(readlink "$root/current")"
  check_eq "update: previous -> $v1" "$root/releases/$v1" "$(readlink "$root/previous")"
  check_eq "update: engine version $v2" "$v2" "$(engine_version)"
  check_eq "update: bundle unchanged" "$bundle_sha" "$(shasum -a 256 "$root/bundles/keepme.txt" | awk '{print $1}')"
  check_eq "update: queued job unchanged" "$job_sha" "$(shasum -a 256 "$root/jobs/inbox/queued-0001.job" | awk '{print $1}')"

  echo "-- rollback to $v1 --"
  bash "$ONPREM/install.sh" --rollback --config "$config" > "$work/rollback.log" 2>&1
  rc=$?
  check_eq "rollback: exit 0" 0 "$rc"
  [[ "$rc" -eq 0 ]] || tail -n 40 "$work/rollback.log"
  check_eq "rollback: current -> $v1" "$root/releases/$v1" "$(readlink "$root/current")"
  check_eq "rollback: engine version $v1" "$v1" "$(engine_version)"
  check_eq "rollback: bundle unchanged" "$bundle_sha" "$(shasum -a 256 "$root/bundles/keepme.txt" | awk '{print $1}')"

  echo "-- air-gap attestation --"
  OPENADAPT_FLOW_SCRUB=on bash "$HERE/verify-airgap.sh" --config "$config" --probe --audit \
    > "$work/airgap.log" 2>&1
  rc=$?
  check_eq "attestation: exit 0" 0 "$rc"
  check "attestation: egress probe could not reach the internet" \
    grep -q "outbound egress is blocked" "$work/airgap.log"
  check "attestation: audit chain intact" grep -q "audit chain intact" "$work/airgap.log"
  [[ "$rc" -eq 0 ]] || cat "$work/airgap.log"

  rm -rf "$work"
  echo
  echo "== result: $PASS passed, $FAIL failed =="
  [[ "$FAIL" -eq 0 ]]
}

"$CMD"
