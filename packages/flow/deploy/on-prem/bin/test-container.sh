#!/usr/bin/env bash
#
# test-container.sh — build the on-prem runner image from a signed release
# with no network, and run it on the compose file's internal network.
#
#   test-container.sh DIR
#
# DIR is what `bin/test-offline-install.sh stage DIR` writes (a signed V1
# release and the vendor public key). Needs Linux, Docker with compose, and a
# connected host only to pull the pinned base image. Checks:
#   - build-image.sh refuses a tampered archive and builds no image;
#   - the image build has no network: the same release with one dependency
#     wheel removed fails to build;
#   - the signed release builds, and inside the image (no network) the engine
#     and privacy extra import from the image's venv at the release version;
#   - the default command resolves, and run-queue.sh files a job and writes
#     the audit log on a bind-mounted storage root; it refuses to start with
#     scrubbing off;
#   - docker-compose.yml needs the image named, never pulls, publishes no
#     ports, and its network is internal: a TCP connect to the internet that
#     works from the same image on Docker's default network fails from the
#     compose runner.
#
# CI runs it in packages.yml (flow on-prem offline install). Exit 0 when every
# check passes.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONPREM="$(cd "$HERE/.." && pwd)"
DIR="${1:-}"
if [[ -z "$DIR" || ! -f "$DIR/V1" ]]; then
  grep '^#' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
DIR="$(cd "$DIR" && pwd)"
command -v docker >/dev/null || { echo "test-container.sh: docker is required" >&2; exit 2; }

PASS=0
FAIL=0
ok() { printf '  PASS %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
check_eq() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi; }

v1="$(cat "$DIR/V1")"
archive="$DIR/v1/release-$v1.tar.gz"
sig="$archive.sig"
pub="$DIR/keys/vendor.pub"
base="$(awk -F= '/^ARG BASE_IMAGE=/ {print $2; exit}' "$ONPREM/Dockerfile")"
tag="openadapt-flow-onprem-test:$v1"
project="oa-onprem-test-$$"
compose=(docker compose -p "$project" -f "$ONPREM/docker-compose.yml")
work="$(mktemp -d "${TMPDIR:-/tmp}/oa-test-container.XXXXXX")"
store="$work/storage"
mkdir -p "$store"

cleanup() {
  OPENADAPT_FLOW_IMAGE="$tag" OPENADAPT_ONPREM_STORAGE="$store" \
    "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
  # The container writes the storage root as root; empty it from inside.
  docker run --rm --network none -v "$store:/s" "$tag" \
    sh -c 'rm -rf /s/* /s/.[!.]*' >/dev/null 2>&1 || true
  docker image rm -f "$tag" "$tag-tampered" "$tag-missing" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "== pulling the pinned base image (connected; the build itself has no network) =="
docker pull -q "$base" >/dev/null || { echo "test-container.sh: cannot pull $base" >&2; exit 1; }

echo "== build-image.sh refuses a tampered archive =="
cp "$archive" "$work/tampered.tar.gz"
printf 'x' >> "$work/tampered.tar.gz"
if bash "$HERE/build-image.sh" --archive "$work/tampered.tar.gz" --signature "$sig" \
    --pubkey "$pub" --sig-tool openssl --tag "$tag-tampered" >"$work/tampered.log" 2>&1; then
  bad "tampered archive refused"
else
  ok "tampered archive refused"
fi
if docker image inspect "$tag-tampered" >/dev/null 2>&1; then
  bad "no image built from the tampered archive"
else
  ok "no image built from the tampered archive"
fi

echo "== the image build has no network: a release missing one wheel fails =="
mkdir -p "$work/missing/on-prem"
tar xzf "$archive" -C "$work/missing"
victim="$(find "$work/missing/release/wheels" -name 'presidio_analyzer-*.whl' -print | head -n 1)"
if [[ -z "$victim" ]]; then
  bad "release holds the presidio_analyzer wheel to remove"
else
  rm -f "$victim"
  tar -C "$ONPREM" -cf - . | tar -C "$work/missing/on-prem" -xf -
  # --progress=plain keeps pip's own error in the log; -q would drop it.
  if docker build --network none --pull=false --progress=plain \
      -f "$work/missing/on-prem/Dockerfile" \
      -t "$tag-missing" "$work/missing" >"$work/missing.log" 2>&1; then
    bad "build without $(basename "$victim") fails (it reached a package index?)"
  else
    if grep -q "presidio" "$work/missing.log"; then
      ok "build without $(basename "$victim") fails on that package"
    else
      bad "build without $(basename "$victim") failed for another reason"
      tail -n 20 "$work/missing.log"
    fi
  fi
fi

echo "== build the signed release =="
if bash "$HERE/build-image.sh" --archive "$archive" --signature "$sig" --pubkey "$pub" \
    --sig-tool openssl --tag "$tag" >"$work/build.log" 2>&1; then
  ok "signed release $v1 builds with no network"
else
  bad "signed release $v1 builds with no network"
  tail -n 40 "$work/build.log"
  echo "== result: $PASS passed, $FAIL failed =="
  exit 1
fi

run() { docker run --rm --network none "$@"; }

check_eq "image: engine CLI reports $v1" "openadapt-flow $v1" \
  "$(run "$tag" openadapt-flow --version 2>&1)"
check_eq "image: engine and privacy extra import from the image venv, version $v1" "$v1" \
  "$(run -w / "$tag" python -I -c '
import importlib.metadata, os, sys
import openadapt_flow, openadapt_privacy, presidio_analyzer
here = os.path.realpath(openadapt_flow.__file__)
if not here.startswith("/opt/openadapt-flow/venv/"):
    sys.exit("openadapt_flow imported from outside the image venv: " + here)
if openadapt_flow.__version__ != importlib.metadata.version("openadapt-flow"):
    sys.exit("module and distribution versions differ")
print(openadapt_flow.__version__)
' 2>&1)"
check_eq "image: VERSION file" "$v1" "$(run "$tag" cat /opt/openadapt-flow/VERSION 2>&1)"
check_eq "image: default command is the absolute run-queue path" \
  '["/opt/openadapt-flow/deploy/on-prem/bin/run-queue.sh","watch"]' \
  "$(docker image inspect -f '{{json .Config.Cmd}}' "$tag")"

echo "== run-queue.sh in the image, on a bind-mounted storage root =="
mkdir -p "$store/jobs/inbox"
printf 'params=none\n' > "$store/jobs/inbox/no-bundle.job"
qenv=(-e OPENADAPT_ONPREM_ROOT=/srv/openadapt
      -e OPENADAPT_ONPREM_AUDIT_LOG=/srv/openadapt/audit/audit.log
      -v "$store:/srv/openadapt")
if run "${qenv[@]}" "$tag" /opt/openadapt-flow/deploy/on-prem/bin/run-queue.sh once \
    >"$work/queue.log" 2>&1; then
  ok "run-queue.sh once exits 0"
else
  bad "run-queue.sh once exits 0"; cat "$work/queue.log"
fi
if [[ -f "$store/jobs/failed/no-bundle.job" ]]; then
  ok "malformed job filed under failed/"
else
  bad "malformed job filed under failed/"
fi
# The runner is root in the image and audit-log.sh makes the audit directory
# 0700, so read the log the way the deployment does: from inside the image.
if run -v "$store:/srv/openadapt" "$tag" cat /srv/openadapt/audit/audit.log 2>/dev/null \
    | grep -q '"job":"no-bundle"'; then
  ok "audit log records the job"
else
  bad "audit log records the job"
fi
if run "${qenv[@]}" -e OPENADAPT_FLOW_SCRUB=off "$tag" \
    /opt/openadapt-flow/deploy/on-prem/bin/run-queue.sh once >/dev/null 2>&1; then
  bad "run-queue.sh refuses to start with scrubbing off"
else
  ok "run-queue.sh refuses to start with scrubbing off"
fi

echo "== docker-compose.yml =="
if OPENADAPT_ONPREM_STORAGE="$store" "${compose[@]}" config >/dev/null 2>&1; then
  bad "compose refuses to run without OPENADAPT_FLOW_IMAGE"
else
  ok "compose refuses to run without OPENADAPT_FLOW_IMAGE"
fi
export OPENADAPT_FLOW_IMAGE="$tag" OPENADAPT_ONPREM_STORAGE="$store"
cfg="$("${compose[@]}" config --format json 2>"$work/config.err")"
check_eq "compose: runner never pulls, publishes no ports, network internal" \
  "never|none|True" \
  "$(printf '%s' "$cfg" | python3 -c '
import json, sys
c = json.load(sys.stdin)
r = c["services"]["runner"]
nets = [c["networks"][n].get("internal", False) for n in r["networks"]]
print("%s|%s|%s" % (r.get("pull_policy"), r.get("ports") or "none", all(nets) and bool(nets)))
' 2>&1)"

probe='import socket; socket.create_connection(("1.1.1.1", 443), timeout=5).close()'
if docker run --rm "$tag" python -c "$probe" >/dev/null 2>&1; then
  ok "control: the probe connects from this image on Docker's default network"
  if "${compose[@]}" run --rm -T --no-deps runner python -c "$probe" >"$work/probe.log" 2>&1; then
    bad "compose runner cannot connect to the internet"
  else
    ok "compose runner cannot connect to the internet"
  fi
else
  bad "control: the probe connects from this image on Docker's default network (cannot tell a blocked network from no network)"
fi

"${compose[@]}" up -d runner >"$work/up.log" 2>&1 || { bad "compose up runner"; cat "$work/up.log"; }
sleep 5
state="$(docker inspect -f '{{.State.Status}}' "$("${compose[@]}" ps -q runner)" 2>/dev/null)"
check_eq "compose: runner keeps watching the queue" "running" "$state"
if "${compose[@]}" logs runner 2>&1 | grep -q "watching /srv/openadapt/jobs/inbox"; then
  ok "compose: runner logs that it is watching the inbox"
else
  bad "compose: runner logs that it is watching the inbox"; "${compose[@]}" logs runner
fi

echo "== result: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
