#!/usr/bin/env bash
# Hermetic offline tests for signed update, atomic activation, and rollback.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/on-prem/bin/lib-release.sh
source "$HERE/lib-release.sh"

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
assert() {
  local message="$1"
  shift
  if "$@"; then pass "$message"; else fail "$message"; fi
}
assert_eq() {
  local message="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    pass "$message"
  else
    fail "$message (expected '$expected', got '$actual')"
  fi
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/onprem-update-test.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

ROOT="$WORK/storage"
MEDIA="$WORK/media"
mkdir -p "$ROOT/releases" "$ROOT/bundles" "$ROOT/jobs/inbox" "$ROOT/audit" \
  "$ROOT/keys" "$MEDIA"

export OPENADAPT_ONPREM_AUDIT_BIN="$HERE/audit-log.sh"
export OPENADAPT_ONPREM_AUDIT_LOG="$ROOT/audit/audit.log"

echo "identity evidence PLACEHOLDER" > "$ROOT/bundles/keepme.txt"
printf 'bundle=/x\nparams=k=v\n' > "$ROOT/jobs/inbox/queued-0001.job"
bash "$OPENADAPT_ONPREM_AUDIT_BIN" queued job-0001 --note seeded >/dev/null
DATA_BUNDLE_SHA="$(sha256_of "$ROOT/bundles/keepme.txt")"
DATA_JOB_SHA="$(sha256_of "$ROOT/jobs/inbox/queued-0001.job")"

HOOK="$WORK/build-hook.sh"
cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
set -euo pipefail
release_dir="$1"
mkdir -p "$release_dir/venv/bin"
if [[ "${OPENADAPT_TEST_BROKEN_CLI:-0}" == "1" ]]; then
  printf '#!/usr/bin/env bash\nexit 1\n' > "$release_dir/venv/bin/openadapt-flow"
else
  printf '#!/usr/bin/env bash\necho "openadapt-flow (fake)"\nexit 0\n' \
    > "$release_dir/venv/bin/openadapt-flow"
fi
chmod +x "$release_dir/venv/bin/openadapt-flow"
HOOK_EOF
chmod +x "$HOOK"
export OPENADAPT_ONPREM_BUILD_HOOK="$HOOK"

PRIVATE_KEY="$WORK/vendor-private.pem"
PUBLIC_KEY="$WORK/vendor-public.pem"
OTHER_PRIVATE_KEY="$WORK/other-private.pem"
OTHER_PUBLIC_KEY="$WORK/other-public.pem"
openssl genrsa -out "$PRIVATE_KEY" 2048 >/dev/null 2>&1
openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY" >/dev/null 2>&1
openssl genrsa -out "$OTHER_PRIVATE_KEY" 2048 >/dev/null 2>&1
openssl rsa -in "$OTHER_PRIVATE_KEY" -pubout -out "$OTHER_PUBLIC_KEY" >/dev/null 2>&1

sign_archive() {
  local archive="$1"
  sha256_of "$archive" > "$archive.sha256"
  openssl dgst -sha256 -sign "$PRIVATE_KEY" -out "$archive.sig" "$archive"
}

make_release() {
  local version="$1"
  local build="$WORK/build-$version" archive="$MEDIA/release-$version.tar.gz"
  rm -rf "$build"
  mkdir -p "$build/release/wheels"
  printf '%s\n' "$version" > "$build/release/VERSION"
  printf 'placeholder\n' > "$build/release/wheels/openadapt_flow-${version}-py3-none-any.whl"
  COPYFILE_DISABLE=1 tar czf "$archive" -C "$build" release
  sign_archive "$archive"
  printf '%s\n' "$archive"
}

apply_release() {
  local root="$1" archive="$2"
  rel_do_update "$root" "$archive" "$archive.sha256" "$archive.sig" \
    "$PUBLIC_KEY" openssl "" "${3:-}"
}

echo "== on-prem signed update/rollback tests (offline, hermetic) =="

V1=1.0.0
R1="$ROOT/releases/$V1"
mkdir -p "$R1"
"$HOOK" "$R1" unused "$V1"
printf 'version=%s\n' "$V1" > "$R1/RELEASE"
rel_activate "$ROOT" "$R1" >/dev/null
assert_eq "seed: current -> v1" "$R1" "$(readlink "$ROOT/current")"
assert "seed: compatibility venv alias resolves" test -x "$ROOT/venv/bin/openadapt-flow"

echo "-- signed update v1 -> v2 --"
ARC2="$(make_release 2.0.0)"
apply_release "$ROOT" "$ARC2"
rc=$?
assert_eq "update: exit 0" 0 "$rc"
assert_eq "update: current -> v2" "$ROOT/releases/2.0.0" "$(readlink "$ROOT/current")"
assert_eq "update: previous -> v1" "$R1" "$(readlink "$ROOT/previous")"
assert "update: RELEASE records pinned verification" \
  grep -q '^verify_method=openssl+sha256$' "$ROOT/releases/2.0.0/RELEASE"
assert_eq "data: bundle unchanged" "$DATA_BUNDLE_SHA" "$(sha256_of "$ROOT/bundles/keepme.txt")"
assert_eq "data: queued job unchanged" "$DATA_JOB_SHA" "$(sha256_of "$ROOT/jobs/inbox/queued-0001.job")"
assert "audit: prepared and completed update recorded" \
  grep -q '"event":"updated".*version=2.0.0' "$OPENADAPT_ONPREM_AUDIT_LOG"

echo "-- rollback --"
rel_do_rollback "$ROOT" "" >/dev/null 2>&1
rc=$?
assert_eq "rollback: exit 0" 0 "$rc"
assert_eq "rollback: current -> v1" "$R1" "$(readlink "$ROOT/current")"
assert_eq "rollback: previous -> v2" "$ROOT/releases/2.0.0" "$(readlink "$ROOT/previous")"
assert "rollback: completion audit recorded" \
  grep -q '"event":"rolledback".*version=1.0.0' "$OPENADAPT_ONPREM_AUDIT_LOG"

# Simulate power loss after rollback changed current but before it restored the
# roll-forward pointer. Recovery must make both releases reachable again.
atomic_symlink "$R1" "$ROOT/rollback-forward"
atomic_symlink "$ROOT/releases/2.0.0" "$ROOT/current"
atomic_symlink "$ROOT/releases/2.0.0" "$ROOT/previous"
rel_recover_transition "$ROOT"
assert "interrupted rollback transition is recovered" test "$?" -eq 0
assert_eq "recovery leaves activated target current" "$ROOT/releases/2.0.0" \
  "$(readlink "$ROOT/current")"
assert_eq "recovery restores roll-forward release" "$R1" "$(readlink "$ROOT/previous")"
assert "recovery marker is removed" test ! -e "$ROOT/rollback-forward"
rel_do_rollback "$ROOT" "" >/dev/null 2>&1
assert_eq "post-recovery rollback returns to v1" "$R1" "$(readlink "$ROOT/current")"

CURRENT_BEFORE="$(readlink "$ROOT/current")"

echo "-- authenticity and integrity refusal cases --"
ARC3="$(make_release 3.0.0)"
printf 'corruption\n' >> "$ARC3"
apply_release "$ROOT" "$ARC3" >/dev/null 2>&1
assert "tampered archive is refused" test "$?" -ne 0
assert_eq "tamper leaves current unchanged" "$CURRENT_BEFORE" "$(readlink "$ROOT/current")"

ARC4="$(make_release 4.0.0)"
rel_do_update "$ROOT" "$ARC4" "$ARC4.sha256" "" "" "" "" "" >/dev/null 2>&1
assert "checksum without signature is refused" test "$?" -ne 0
rel_do_update "$ROOT" "$ARC4" "$ARC4.sha256" "$ARC4.sig" \
  "$OTHER_PUBLIC_KEY" openssl "" "" >/dev/null 2>&1
assert "signature from a non-pinned key is refused" test "$?" -ne 0
rel_do_update "$ROOT" "$ARC4" "$ARC4.sha256" "$ARC4.sig" \
  "$PUBLIC_KEY" missing-tool "" "" >/dev/null 2>&1
assert "missing configured signature tool is refused" test "$?" -ne 0
if command -v gpg >/dev/null 2>&1; then
  GPG_VENDOR_HOME="$WORK/gpg-vendor"
  GPG_OTHER_HOME="$WORK/gpg-other"
  mkdir -m 0700 "$GPG_VENDOR_HOME" "$GPG_OTHER_HOME"
  if gpg --batch --quiet --homedir "$GPG_VENDOR_HOME" --passphrase '' \
    --quick-generate-key 'Vendor Release <release@example.invalid>' rsa2048 sign 0 \
    >/dev/null 2>&1 \
    && gpg --batch --quiet --homedir "$GPG_OTHER_HOME" --passphrase '' \
      --quick-generate-key 'Other Release <other@example.invalid>' rsa2048 sign 0 \
      >/dev/null 2>&1; then
    gpg --batch --quiet --homedir "$GPG_VENDOR_HOME" --armor \
      --export > "$WORK/gpg-vendor.asc"
    gpg --batch --quiet --homedir "$GPG_OTHER_HOME" --armor \
      --export > "$WORK/gpg-other.asc"
    gpg --batch --quiet --homedir "$GPG_VENDOR_HOME" --detach-sign \
      --output "$ARC4.gpg" "$ARC4"
    rel_verify_integrity "$ARC4" "$ARC4.sha256" "$ARC4.gpg" \
      "$WORK/gpg-vendor.asc" gpg >/dev/null 2>&1
    assert "GPG signature verifies in the pinned isolated keyring" test "$?" -eq 0
    rel_verify_integrity "$ARC4" "$ARC4.sha256" "$ARC4.gpg" \
      "$WORK/gpg-other.asc" gpg >/dev/null 2>&1
    assert "GPG signature from a non-pinned key is refused" test "$?" -ne 0
  fi
fi
assert_eq "authenticity failures leave current unchanged" "$CURRENT_BEFORE" "$(readlink "$ROOT/current")"

echo "-- extraction and version refusal cases --"
UNSAFE="$MEDIA/release-unsafe.tar.gz"
python3 - "$UNSAFE" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as archive:
    data = b"escape"
    member = tarfile.TarInfo("../escaped.txt")
    member.size = len(data)
    archive.addfile(member, io.BytesIO(data))
PY
sign_archive "$UNSAFE"
apply_release "$ROOT" "$UNSAFE" >/dev/null 2>&1
assert "path-traversal member is refused" test "$?" -ne 0
assert "path-traversal member writes nothing outside staging" test ! -e "$ROOT/releases/escaped.txt"

LINK_ARCHIVE="$MEDIA/release-link.tar.gz"
python3 - "$LINK_ARCHIVE" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as archive:
    version = b"5.0.0\n"
    member = tarfile.TarInfo("release/VERSION")
    member.size = len(version)
    archive.addfile(member, io.BytesIO(version))
    link = tarfile.TarInfo("release/wheels")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp"
    archive.addfile(link)
PY
sign_archive "$LINK_ARCHIVE"
apply_release "$ROOT" "$LINK_ARCHIVE" >/dev/null 2>&1
assert "symlink archive member is refused" test "$?" -ne 0

ARC6="$(make_release 6.0.0)"
apply_release "$ROOT" "$ARC6" ../../outside >/dev/null 2>&1
assert "unsafe release-version override is refused" test "$?" -ne 0
apply_release "$ROOT" "$ARC2" >/dev/null 2>&1
assert "duplicate immutable version is refused" test "$?" -ne 0
assert_eq "extraction/version failures leave current unchanged" "$CURRENT_BEFORE" "$(readlink "$ROOT/current")"

echo "-- pointer, lock, and rollback-target safety --"
REAL_DIR="$WORK/real-directory"
mkdir -p "$REAL_DIR/keep"
atomic_symlink "$R1" "$REAL_DIR" >/dev/null 2>&1
assert "atomic helper refuses to replace a real directory" test "$?" -ne 0
assert "refused directory remains intact" test -d "$REAL_DIR/keep"

mkdir "$ROOT/releases/.update.lock"
printf '%s\n' "$$" > "$ROOT/releases/.update.lock/pid"
apply_release "$ROOT" "$ARC6" >/dev/null 2>&1
assert "concurrent update lock is refused" test "$?" -ne 0
rm -f "$ROOT/releases/.update.lock/pid"
rmdir "$ROOT/releases/.update.lock"

chmod -x "$ROOT/releases/2.0.0/venv/bin/openadapt-flow"
rel_do_rollback "$ROOT" "" >/dev/null 2>&1
assert "incomplete rollback target is refused" test "$?" -ne 0
assert_eq "failed rollback leaves current unchanged" "$CURRENT_BEFORE" "$(readlink "$ROOT/current")"
chmod +x "$ROOT/releases/2.0.0/venv/bin/openadapt-flow"

mv "$ROOT/releases/2.0.0/RELEASE" "$ROOT/releases/2.0.0/RELEASE.real"
ln -s RELEASE.real "$ROOT/releases/2.0.0/RELEASE"
rel_do_rollback "$ROOT" "" >/dev/null 2>&1
assert "symlinked release metadata is refused" test "$?" -ne 0
rm "$ROOT/releases/2.0.0/RELEASE"
mv "$ROOT/releases/2.0.0/RELEASE.real" "$ROOT/releases/2.0.0/RELEASE"

echo "-- pre-versioned layout migration --"
LEGACY_ROOT="$WORK/legacy-storage"
mkdir -p "$LEGACY_ROOT/venv/bin" "$LEGACY_ROOT/releases" "$LEGACY_ROOT/audit" \
  "$LEGACY_ROOT/bundles"
cp "$R1/venv/bin/openadapt-flow" "$LEGACY_ROOT/venv/bin/openadapt-flow"
printf 'legacy data\n' > "$LEGACY_ROOT/bundles/keep.txt"
LEGACY_DATA_SHA="$(sha256_of "$LEGACY_ROOT/bundles/keep.txt")"
ROOT_AUDIT="$OPENADAPT_ONPREM_AUDIT_LOG"
export OPENADAPT_ONPREM_AUDIT_LOG="$LEGACY_ROOT/audit/audit.log"
ARC7="$(make_release 7.0.0)"
apply_release "$LEGACY_ROOT" "$ARC7"
rc=$?
assert_eq "legacy migration + update exits 0" 0 "$rc"
assert "legacy root/venv becomes a symlink" test -L "$LEGACY_ROOT/venv"
assert_eq "legacy migration activates v7" "$LEGACY_ROOT/releases/7.0.0" \
  "$(readlink "$LEGACY_ROOT/current")"
LEGACY_PREVIOUS="$(readlink "$LEGACY_ROOT/previous")"
assert "legacy venv is retained as rollback target" test -x "$LEGACY_PREVIOUS/venv/bin/openadapt-flow"
assert_eq "legacy migration preserves data" "$LEGACY_DATA_SHA" \
  "$(sha256_of "$LEGACY_ROOT/bundles/keep.txt")"
export OPENADAPT_ONPREM_AUDIT_LOG="$ROOT_AUDIT"

echo "-- audit serialization --"
for index in 1 2 3 4 5 6 7 8 9 10 11 12; do
  bash "$OPENADAPT_ONPREM_AUDIT_BIN" started "parallel-$index" --note test >/dev/null &
done
wait
audit_output="$(OPENADAPT_ONPREM_AUDIT_LOG="$OPENADAPT_ONPREM_AUDIT_LOG" \
  bash "$HERE/verify-airgap.sh" --config "$WORK/missing.yaml" --audit 2>&1 || true)"
if grep -q 'audit chain intact' <<< "$audit_output"; then
  pass "concurrent audit writers preserve one hash chain"
else
  fail "concurrent audit writers preserve one hash chain"
fi
if ! grep -q 'identity evidence' "$OPENADAPT_ONPREM_AUDIT_LOG"; then
  pass "release audit remains PHI-free"
else
  fail "release audit remains PHI-free"
fi

echo
echo "== result: $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
