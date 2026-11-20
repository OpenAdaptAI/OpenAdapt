#!/usr/bin/env bash
#
# build-release.sh — build an offline release archive from the lock file.
#
# Run on a CONNECTED build host with the same OS, CPU architecture and Python
# minor version as the air-gapped target. Produces, in --out:
#
#   release-<version>.tar.gz          VERSION, wheels/, requirements.lock.txt
#   release-<version>.tar.gz.sha256   checksum (corruption check only)
#   release-<version>.tar.gz.sig      openssl signature, when --sign-key is given
#
# Every third-party wheel is the exact version pinned in packages/flow/uv.lock
# and is checked against the lock's hashes while downloading (pip
# --require-hashes), so the archive holds what CI tested and nothing newer.
# The openadapt-flow wheel is built from this checkout unless --flow-wheel
# names one. Stage the archive with `install.sh --update` (see UPDATE.md), or
# extract it and pass release/wheels to `install.sh --wheelhouse` for a first
# install.
#
# Usage:
#   build-release.sh --out DIR [--flow-wheel FILE] [--sign-key PRIVATE.pem]
#                    [--python PYTHON]
#
# Needs uv and the target Python (default python3). Exit 0 on success.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_DIR="$(cd "$HERE/../../.." && pwd)"
OUT=""
FLOW_WHEEL=""
SIGN_KEY=""
PYTHON="python3"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)        OUT="${2:-}"; shift 2 ;;
    --flow-wheel) FLOW_WHEEL="${2:-}"; shift 2 ;;
    --sign-key)   SIGN_KEY="${2:-}"; shift 2 ;;
    --python)     PYTHON="${2:-}"; shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "build-release.sh: unknown arg $1" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "build-release.sh: --out DIR is required" >&2; exit 2; }
command -v uv >/dev/null || { echo "build-release.sh: uv is required" >&2; exit 2; }
[[ -f "$FLOW_DIR/uv.lock" ]] || { echo "build-release.sh: no uv.lock in $FLOW_DIR" >&2; exit 2; }
if [[ -n "$SIGN_KEY" && ! -f "$SIGN_KEY" ]]; then
  echo "build-release.sh: signing key $SIGN_KEY not found" >&2; exit 2
fi

# shellcheck source=bin/lib-release.sh
source "$HERE/lib-release.sh"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/oa-build-release.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/release/wheels"

if [[ -z "$FLOW_WHEEL" ]]; then
  echo "== building the openadapt-flow wheel from $FLOW_DIR =="
  (cd "$FLOW_DIR" && uv build --wheel --out-dir "$WORK/flow" >/dev/null)
  FLOW_WHEEL="$(find "$WORK/flow" -maxdepth 1 -name 'openadapt_flow-*.whl' -print)"
fi
[[ -f "$FLOW_WHEEL" ]] || { echo "build-release.sh: flow wheel not found" >&2; exit 2; }
cp "$FLOW_WHEEL" "$WORK/release/wheels/"
VERSION="$(rel_wheelhouse_version "$WORK/release/wheels")"
rel_validate_version "$VERSION"

echo "== exporting the locked dependencies of openadapt-flow[privacy,vision] $VERSION =="
(cd "$FLOW_DIR" && uv export --frozen --no-dev --extra privacy --extra vision --no-emit-project \
  --format requirements-txt --no-header -o "$WORK/release/requirements.lock.txt" >/dev/null)

echo "== downloading the locked wheels for $("$PYTHON" -c 'import platform, sys; print(sys.platform, platform.machine(), "py%d.%d" % sys.version_info[:2])') =="
"$PYTHON" -m pip download --quiet --disable-pip-version-check \
  --only-binary=:all: --no-deps --require-hashes \
  -r "$WORK/release/requirements.lock.txt" -d "$WORK/release/wheels"

printf '%s\n' "$VERSION" > "$WORK/release/VERSION"
ARCHIVE="$OUT/release-$VERSION.tar.gz"
COPYFILE_DISABLE=1 tar czf "$ARCHIVE" -C "$WORK" release
sha256_of "$ARCHIVE" > "$ARCHIVE.sha256"
if [[ -n "$SIGN_KEY" ]]; then
  openssl dgst -sha256 -sign "$SIGN_KEY" -out "$ARCHIVE.sig" "$ARCHIVE"
fi

count="$(find "$WORK/release/wheels" -name '*.whl' | wc -l | tr -d ' ')"
echo "built $ARCHIVE ($count wheels)"
[[ -n "$SIGN_KEY" ]] && echo "signed $ARCHIVE.sig" || echo "unsigned: sign it before staging (UPDATE.md)"
