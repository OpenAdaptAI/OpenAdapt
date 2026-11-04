#!/usr/bin/env bash
#
# build-image.sh — build the on-prem runner image from a signed release
# archive, with no network.
#
# Checks the archive against the pinned vendor key (the same check as
# `install.sh --update`), extracts it with the bounded extractor, and runs
# `docker build --network none` on ../Dockerfile, so the image holds exactly
# the release's wheels. A missing wheel fails the build instead of reaching
# PyPI. The base image must already be loaded locally.
#
# Usage:
#   build-image.sh --archive release-V.tar.gz --signature release-V.tar.gz.sig
#                  --pubkey vendor.pub [--sig-tool openssl|gpg|minisign|signify]
#                  [--checksum FILE] [--tag NAME:TAG] [--base IMAGE]
#                  [--save image.tar]
#
# The default tag is openadapt-flow-onprem:<release version>. Run the image
# with docker-compose.yml (OPENADAPT_FLOW_IMAGE=<tag>). Exit 0 on success.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONPREM="$(cd "$HERE/.." && pwd)"
ARCHIVE=""
SIGNATURE=""
PUBKEY=""
SIG_TOOL=""
CHECKSUM=""
TAG=""
BASE=""
SAVE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)   ARCHIVE="${2:-}"; shift 2 ;;
    --signature) SIGNATURE="${2:-}"; shift 2 ;;
    --pubkey)    PUBKEY="${2:-}"; shift 2 ;;
    --sig-tool)  SIG_TOOL="${2:-}"; shift 2 ;;
    --checksum)  CHECKSUM="${2:-}"; shift 2 ;;
    --tag)       TAG="${2:-}"; shift 2 ;;
    --base)      BASE="${2:-}"; shift 2 ;;
    --save)      SAVE="${2:-}"; shift 2 ;;
    -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "build-image.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done
[[ -n "$ARCHIVE" ]] || { echo "build-image.sh: --archive is required" >&2; exit 2; }
command -v docker >/dev/null || { echo "build-image.sh: docker is required" >&2; exit 2; }

# shellcheck source=bin/lib-release.sh
source "$HERE/lib-release.sh"

rel_verify_integrity "$ARCHIVE" "$CHECKSUM" "$SIGNATURE" "$PUBKEY" "$SIG_TOOL" || {
  echo "build-image.sh: refusing to build from an unverified archive" >&2; exit 1
}

CTX="$(mktemp -d "${TMPDIR:-/tmp}/oa-build-image.XXXXXX")"
trap 'rm -rf "$CTX"' EXIT
SRC="$(rel_extract "$ARCHIVE" "$CTX/extract")" || {
  echo "build-image.sh: release archive could not be extracted" >&2; exit 1
}
VERSION="$(rel_declared_version "$SRC" "$SRC/wheels")" || exit 1
mv "$SRC" "$CTX/release"
rm -rf "$CTX/extract"

# The deploy scripts, without anything a site may have left beside them.
mkdir -p "$CTX/on-prem"
tar -C "$ONPREM" --exclude=./wheels --exclude=./storage --exclude=./release \
  --exclude='*.pem' --exclude='*.env' -cf - . | tar -C "$CTX/on-prem" -xf -

TAG="${TAG:-openadapt-flow-onprem:$VERSION}"
args=(build --network none --pull=false -f "$CTX/on-prem/Dockerfile" -t "$TAG")
[[ -n "$BASE" ]] && args+=(--build-arg "BASE_IMAGE=$BASE")
echo "== building $TAG from release $VERSION with no network =="
docker "${args[@]}" "$CTX"

if [[ -n "$SAVE" ]]; then
  docker save -o "$SAVE" "$TAG"
  echo "saved $TAG to $SAVE"
fi
echo "built $TAG"
