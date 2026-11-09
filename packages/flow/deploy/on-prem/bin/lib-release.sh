#!/usr/bin/env bash
# Atomic, offline release management for the on-prem installation.
# This file is sourced; callers own shell options and must check return codes.

_rel_err() { printf '  \033[31mERROR\033[0m %s\n' "$1" >&2; }
_rel_warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1" >&2; }
_rel_ok() { printf '  \033[32mOK\033[0m    %s\n' "$1" >&2; }

sha256_of() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    _rel_err "sha256sum or shasum is required"
    return 1
  fi
}

rel_validate_version() {
  local version="$1"
  [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]] || {
    _rel_err "invalid release version: $version"
    return 1
  }
}

rel_wheelhouse_version() {
  local wheelhouse="$1"
  local -a wheels=()
  [[ -d "$wheelhouse" ]] || return 1
  while IFS= read -r wheel; do
    wheels+=("$wheel")
  done < <(find "$wheelhouse" -maxdepth 1 -type f -name 'openadapt_flow-*.whl' -print | sort)
  if [[ "${#wheels[@]}" -ne 1 ]]; then
    _rel_err "wheelhouse must contain exactly one openadapt_flow wheel (found ${#wheels[@]})"
    return 1
  fi
  local filename version
  filename="$(basename "${wheels[0]}")"
  version="${filename#openadapt_flow-}"
  version="${version%%-*}"
  rel_validate_version "$version" || return 1
  printf '%s\n' "$version"
}

rel_declared_version() {
  local source_dir="$1" wheelhouse="$2"
  [[ -f "$source_dir/VERSION" && ! -L "$source_dir/VERSION" ]] || {
    _rel_err "signed release payload must contain a regular VERSION file"
    return 1
  }
  local declared wheel_version
  declared="$(cat "$source_dir/VERSION")"
  if [[ "$declared" == *$'\n'* || "$declared" == *$'\r'* ]]; then
    _rel_err "VERSION must contain exactly one line"
    return 1
  fi
  declared="$(printf '%s' "$declared" \
    | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
  rel_validate_version "$declared" || return 1
  wheel_version="$(rel_wheelhouse_version "$wheelhouse")" || return 1
  if [[ "$declared" != "$wheel_version" ]]; then
    _rel_err "VERSION ($declared) does not match bundled openadapt-flow wheel ($wheel_version)"
    return 1
  fi
  printf '%s\n' "$declared"
}

# Replace only an absent path or an existing symlink. Never remove a directory
# as a fallback: doing so would make the advertised atomicity false.
atomic_symlink() {
  local target="$1" link="$2"
  local dir base tmp
  dir="$(dirname "$link")"
  base="$(basename "$link")"
  if [[ ( -e "$link" || -L "$link" ) && ! -L "$link" ]]; then
    _rel_err "refusing to replace non-symlink path: $link"
    return 1
  fi
  mkdir -p "$dir" || return 1
  tmp="$dir/.$base.tmp.$$.$RANDOM"
  rm -f "$tmp"
  ln -s "$target" "$tmp" || return 1
  if mv -fT "$tmp" "$link" 2>/dev/null; then
    return 0
  fi
  if command -v perl >/dev/null 2>&1 \
    && perl -e 'rename($ARGV[0], $ARGV[1]) or exit 1' "$tmp" "$link" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp"
  _rel_err "no supported atomic rename primitive for $link"
  return 1
}

_rel_verify_gpg() {
  local archive="$1" signature="$2" pubkey="$3"
  local home count rc=1
  home="$(mktemp -d "${TMPDIR:-/tmp}/openadapt-gpg.XXXXXX")" || return 1
  chmod 0700 "$home"
  if gpg --batch --quiet --homedir "$home" --import "$pubkey" >/dev/null 2>&1; then
    count="$(gpg --batch --homedir "$home" --with-colons --list-keys 2>/dev/null \
      | awk -F: '$1 == "pub" {n++} END {print n+0}')"
    if [[ "$count" -gt 0 ]] \
      && gpg --batch --quiet --homedir "$home" --verify "$signature" "$archive" >/dev/null 2>&1; then
      rc=0
    fi
  fi
  rm -rf "$home"
  return "$rc"
}

# A detached signature against a locally pinned public key is mandatory.
# A checksum is an additional corruption check, never an authenticity substitute.
rel_verify_integrity() {
  local archive="$1" checksum="${2:-}" signature="${3:-}" pubkey="${4:-}" tool="${5:-}"
  [[ -f "$archive" && ! -L "$archive" ]] || {
    _rel_err "release archive must be a regular non-symlink file: $archive"
    return 1
  }
  [[ -n "$signature" ]] || {
    _rel_err "detached release signature is required"
    return 1
  }
  [[ -n "$pubkey" ]] || {
    _rel_err "pinned vendor public key is required"
    return 1
  }
  [[ -f "$signature" && ! -L "$signature" ]] || {
    _rel_err "signature must be a regular non-symlink file: $signature"
    return 1
  }
  [[ -f "$pubkey" && ! -L "$pubkey" ]] || {
    _rel_err "vendor public key must be a regular non-symlink file: $pubkey"
    return 1
  }

  if [[ -z "$checksum" && -f "$archive.sha256" && ! -L "$archive.sha256" ]]; then
    checksum="$archive.sha256"
  fi
  if [[ -n "$checksum" ]]; then
    [[ -f "$checksum" && ! -L "$checksum" ]] || {
      _rel_err "checksum must be a regular non-symlink file: $checksum"
      return 1
    }
    local want have
    want="$(awk 'NR == 1 {print $1}' "$checksum")"
    have="$(sha256_of "$archive")" || return 1
    want="$(printf '%s' "$want" | tr 'A-F' 'a-f')"
    if [[ ! "$want" =~ ^[a-f0-9]{64}$ || "$want" != "$have" ]]; then
      _rel_err "checksum mismatch: expected $want, got $have"
      return 1
    fi
    _rel_ok "checksum matches ($have)"
  fi

  if [[ -z "$tool" ]]; then
    case "$signature" in
      *.minisig) tool=minisign ;;
      *.asc | *.gpg) tool=gpg ;;
      *)
        _rel_err "--sig-tool is required for an ambiguous signature extension"
        return 1
        ;;
    esac
  fi
  command -v "$tool" >/dev/null 2>&1 || {
    _rel_err "configured signature tool is not installed: $tool"
    return 1
  }

  local rc=1
  case "$tool" in
    minisign)
      minisign -V -m "$archive" -p "$pubkey" -x "$signature" >/dev/null 2>&1 && rc=0
      ;;
    signify)
      signify -V -p "$pubkey" -m "$archive" -x "$signature" >/dev/null 2>&1 && rc=0
      ;;
    gpg)
      _rel_verify_gpg "$archive" "$signature" "$pubkey" && rc=0
      ;;
    openssl)
      openssl dgst -sha256 -verify "$pubkey" -signature "$signature" "$archive" \
        >/dev/null 2>&1 && rc=0
      ;;
    *)
      _rel_err "unknown signature tool: $tool"
      return 1
      ;;
  esac
  if [[ "$rc" -ne 0 ]]; then
    _rel_err "signature verification failed with $tool against the pinned vendor key"
    return 1
  fi
  REL_VERIFY_METHOD="$tool"
  [[ -n "$checksum" ]] && REL_VERIFY_METHOD="$tool+sha256"
  _rel_ok "signature verified with $tool against the pinned vendor key"
}

rel_extract() {
  local archive="$1" destination="$2"
  local here python extractor
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  extractor="$here/safe-extract-release.py"
  python="${OPENADAPT_ONPREM_PY:-python3}"
  command -v "$python" >/dev/null 2>&1 || {
    _rel_err "python3 is required for bounded release extraction"
    return 1
  }
  "$python" "$extractor" "$archive" "$destination" || return 1

  local entries top
  if [[ -f "$destination/VERSION" && -d "$destination/wheels" ]]; then
    printf '%s\n' "$destination"
    return 0
  fi
  entries="$(find "$destination" -mindepth 1 -maxdepth 1 -print | wc -l | tr -d ' ')"
  top="$(find "$destination" -mindepth 1 -maxdepth 1 -type d -print | head -n 1)"
  if [[ "$entries" != "1" || -z "$top" || ! -f "$top/VERSION" || ! -d "$top/wheels" ]]; then
    _rel_err "release archive must be flat or have one top-level directory containing VERSION and wheels/"
    return 1
  fi
  printf '%s\n' "$top"
}

rel_build_release() {
  local release_dir="$1" wheelhouse="$2" version="${3:-}"
  if [[ -n "${OPENADAPT_ONPREM_BUILD_HOOK:-}" ]]; then
    "$OPENADAPT_ONPREM_BUILD_HOOK" "$release_dir" "$wheelhouse" "$version"
    return $?
  fi
  local python="${OPENADAPT_ONPREM_PY:-python3}"
  [[ -d "$wheelhouse" ]] || {
    _rel_err "wheelhouse not found in release bundle: $wheelhouse"
    return 1
  }
  "$python" -m venv "$release_dir/venv" || return 1
  local requirement='openadapt-flow[privacy]'
  [[ -n "$version" ]] && requirement="openadapt-flow[privacy]==$version"
  "$release_dir/venv/bin/pip" install --no-index --only-binary=:all: \
    --find-links "$wheelhouse" "$requirement"
}

rel_smoke() {
  local release_dir="$1" config="${2:-}"
  if [[ -n "${OPENADAPT_ONPREM_SMOKE_HOOK:-}" ]]; then
    "$OPENADAPT_ONPREM_SMOKE_HOOK" "$release_dir" "$config"
    return $?
  fi
  local cli="$release_dir/venv/bin/openadapt-flow"
  [[ -x "$cli" ]] || {
    _rel_err "smoke: engine CLI missing in release ($cli)"
    return 1
  }
  if ! "$cli" --version >/dev/null 2>&1 && ! "$cli" --help >/dev/null 2>&1; then
    _rel_err "smoke: engine CLI does not run in release"
    return 1
  fi
  local here airgap
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  airgap="$here/verify-airgap.sh"
  if [[ -x "$airgap" && -n "$config" && -f "$config" ]]; then
    OPENADAPT_FLOW_SCRUB="${OPENADAPT_FLOW_SCRUB:-on}" \
      bash "$airgap" --config "$config" >/dev/null 2>&1 || {
        _rel_err "smoke: air-gap verification failed"
        return 1
      }
  fi
  _rel_ok "smoke passed (CLI runs, air-gap gate green)"
}

_rel_json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/ }"
  value="${value//$'\r'/ }"
  value="${value//$'\t'/ }"
  printf '%s' "$value"
}

_rel_history_append() {
  local root="$1" event="$2" version="$3" release_path="$4"
  local history="$root/releases/HISTORY" line
  line="$(printf '{"ts":"%s","event":"%s","version":"%s","release":"%s","actor":"%s"}' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(_rel_json_escape "$event")" \
    "$(_rel_json_escape "$version")" \
    "$(_rel_json_escape "$release_path")" \
    "$(_rel_json_escape "$(id -un 2>/dev/null || echo unknown)")")"
  printf '%s\n' "$line" >> "$history" || {
    _rel_err "could not append release history: $history"
    return 1
  }
  chmod 0600 "$history" 2>/dev/null || true
}

_rel_audit() {
  local event="$1" version="$2" from_version="${3:-}"
  if [[ -z "${OPENADAPT_ONPREM_AUDIT_BIN:-}" || -z "${OPENADAPT_ONPREM_AUDIT_LOG:-}" ]]; then
    _rel_warn "release audit is not configured"
    return 0
  fi
  local note="version=$version"
  [[ -n "$from_version" ]] && note="$note;from=$from_version"
  OPENADAPT_ONPREM_AUDIT_LOG="$OPENADAPT_ONPREM_AUDIT_LOG" \
    bash "$OPENADAPT_ONPREM_AUDIT_BIN" "$event" release --note "$note" >/dev/null 2>&1 || {
      _rel_err "could not append release event to the audit log"
      return 1
    }
}

rel_current_path() { readlink "$1/current" 2>/dev/null || true; }
rel_previous_path() { readlink "$1/previous" 2>/dev/null || true; }

rel_version_of() {
  local release_dir="$1" version
  if [[ -f "$release_dir/RELEASE" && ! -L "$release_dir/RELEASE" ]]; then
    version="$(awk -F= '$1 == "version" {print substr($0, 9); exit}' "$release_dir/RELEASE")"
  else
    version="$(basename "$release_dir")"
  fi
  rel_validate_version "$version" >/dev/null 2>&1 || return 1
  printf '%s\n' "$version"
}

rel_validate_release_dir() {
  local root="$1" release_dir="$2"
  local releases_real parent_real base
  [[ "$release_dir" = /* && -d "$release_dir" && ! -L "$release_dir" ]] || return 1
  releases_real="$(cd "$root/releases" 2>/dev/null && pwd -P)" || return 1
  parent_real="$(cd "$(dirname "$release_dir")" 2>/dev/null && pwd -P)" || return 1
  base="$(basename "$release_dir")"
  [[ "$parent_real" == "$releases_real" ]] || return 1
  rel_validate_version "$base" >/dev/null 2>&1 || return 1
  [[ -f "$release_dir/RELEASE" && ! -L "$release_dir/RELEASE" ]] || return 1
  [[ "$(rel_version_of "$release_dir")" == "$base" ]] || return 1
  [[ -d "$release_dir/venv" && ! -L "$release_dir/venv" ]] || return 1
  [[ -f "$release_dir/venv/bin/openadapt-flow" \
    && ! -L "$release_dir/venv/bin/openadapt-flow" \
    && -x "$release_dir/venv/bin/openadapt-flow" ]]
}

_rel_lock_acquire() {
  local root="$1" owner=""
  local lock="$root/releases/.update.lock"
  mkdir -p "$root/releases" || return 1
  if mkdir "$lock" 2>/dev/null; then
    printf '%s\n' "$$" > "$lock/pid" || {
      rmdir "$lock" 2>/dev/null || true
      return 1
    }
    REL_LOCK_DIR="$lock"
    return 0
  fi
  if [[ -d "$lock" && ! -L "$lock" && -r "$lock/pid" ]]; then
    owner="$(cat "$lock/pid" 2>/dev/null || true)"
  fi
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    _rel_err "another update or rollback is active (pid $owner)"
    return 1
  fi
  if [[ -d "$lock" && ! -L "$lock" ]]; then
    rm -f "$lock/pid" 2>/dev/null || true
    if rmdir "$lock" 2>/dev/null && mkdir "$lock" 2>/dev/null; then
      printf '%s\n' "$$" > "$lock/pid" || {
        rmdir "$lock" 2>/dev/null || true
        return 1
      }
      REL_LOCK_DIR="$lock"
      return 0
    fi
  fi
  _rel_err "could not acquire release lock: $lock"
  return 1
}

_rel_lock_release() {
  local lock="${REL_LOCK_DIR:-}"
  [[ -n "$lock" ]] || return 0
  rm -f "$lock/pid" 2>/dev/null || true
  rmdir "$lock" 2>/dev/null || true
  REL_LOCK_DIR=""
}

# Migrate the pre-versioned root/venv layout without changing its absolute
# shebang path: after the move, root/venv becomes an alias through current.
rel_migrate_legacy_layout() {
  local root="$1"
  local current legacy_version legacy_dir
  current="$(rel_current_path "$root")"
  if [[ -n "$current" ]]; then
    if [[ -d "$root/venv" && ! -L "$root/venv" ]]; then
      _rel_err "current exists but venv is a real directory; refusing ambiguous layout"
      return 1
    fi
    return 0
  fi
  if [[ ! -d "$root/venv" || -L "$root/venv" ]]; then
    return 0
  fi
  [[ -x "$root/venv/bin/openadapt-flow" ]] || {
    _rel_err "legacy venv does not contain an executable openadapt-flow CLI"
    return 1
  }
  legacy_version="legacy-$(date -u +%Y%m%dT%H%M%SZ)"
  legacy_dir="$root/releases/$legacy_version"
  [[ ! -e "$legacy_dir" ]] || {
    _rel_err "legacy migration target already exists: $legacy_dir"
    return 1
  }
  _rel_audit layout_migration_prepared "$legacy_version" || return 1
  mkdir -p "$legacy_dir" || return 1
  if ! mv "$root/venv" "$legacy_dir/venv"; then
    rmdir "$legacy_dir" 2>/dev/null || true
    return 1
  fi
  if ! {
    printf 'version=%s\n' "$legacy_version"
    printf 'applied_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'source_archive=legacy-layout\nsource_sha256=\nverify_method=legacy-migration\n'
  } > "$legacy_dir/RELEASE"; then
    mv "$legacy_dir/venv" "$root/venv" 2>/dev/null || true
    rm -f "$legacy_dir/RELEASE" 2>/dev/null || true
    rmdir "$legacy_dir" 2>/dev/null || true
    return 1
  fi
  if ! chmod 0600 "$legacy_dir/RELEASE"; then
    mv "$legacy_dir/venv" "$root/venv" 2>/dev/null || true
    rm -f "$legacy_dir/RELEASE" 2>/dev/null || true
    rmdir "$legacy_dir" 2>/dev/null || true
    return 1
  fi
  if ! atomic_symlink "current/venv" "$root/venv" \
    || ! atomic_symlink "$legacy_dir" "$root/current"; then
    rm -f "$root/venv" "$root/current" 2>/dev/null || true
    mv "$legacy_dir/venv" "$root/venv" 2>/dev/null || true
    rm -f "$legacy_dir/RELEASE" 2>/dev/null || true
    rmdir "$legacy_dir" 2>/dev/null || true
    _rel_err "legacy layout migration failed; restored root/venv"
    return 1
  fi
  if ! _rel_history_append "$root" migrated "$legacy_version" "$legacy_dir" \
    || ! _rel_audit layout_migrated "$legacy_version"; then
    _rel_err "legacy layout migrated, but its completion record failed; inspect before retrying"
    return 4
  fi
  _rel_ok "migrated legacy venv into $legacy_dir; rollback target is preserved"
}

# A rollback changes two pointers. The extra root/rollback-forward marker keeps
# the pre-rollback release reachable if the host loses power between the two
# atomic renames. The next serialized lifecycle command reconciles it.
rel_recover_transition() {
  local root="$1"
  local marker="$root/rollback-forward"
  [[ -L "$marker" ]] || return 0
  local forward current version rc=0
  forward="$(readlink "$marker")"
  rel_validate_release_dir "$root" "$forward" || {
    _rel_err "invalid rollback recovery marker: $marker -> $forward"
    return 1
  }
  current="$(rel_current_path "$root")"
  if [[ "$current" != "$forward" ]]; then
    atomic_symlink "$forward" "$root/previous" || return 1
  fi
  rm -f "$marker" || return 1
  version="$(rel_version_of "$forward")" || return 1
  _rel_history_append "$root" transition_recovered "$version" "$forward" || rc=4
  _rel_audit transition_recovered "$version" || rc=4
  if [[ "$rc" -ne 0 ]]; then
    _rel_err "rollback pointers were recovered, but completion logging failed"
    return "$rc"
  fi
  _rel_ok "recovered interrupted rollback pointer state"
}

rel_activate() {
  local root="$1" release_dir="$2"
  local old old_version="" version rc
  rel_validate_release_dir "$root" "$release_dir" || {
    _rel_err "refusing unmanaged or incomplete release directory: $release_dir"
    return 1
  }
  old="$(rel_current_path "$root")"
  if [[ -n "$old" ]]; then
    rel_validate_release_dir "$root" "$old" || {
      _rel_err "current release pointer is outside the managed release directory"
      return 1
    }
    [[ "$old" != "$release_dir" ]] || {
      _rel_err "release is already current: $release_dir"
      return 3
    }
    old_version="$(rel_version_of "$old")" || return 1
  fi
  version="$(rel_version_of "$release_dir")" || return 1
  _rel_audit update_prepared "$version" "$old_version" || return 1

  # Establish the compatibility alias and rollback pointer before current. A
  # crash can therefore leave an extra safe pointer, never lose the old target.
  atomic_symlink "current/venv" "$root/venv" || return 1
  if [[ -n "$old" ]]; then
    atomic_symlink "$old" "$root/previous" || return 1
  fi
  atomic_symlink "$release_dir" "$root/current" || return 1

  rc=0
  _rel_history_append "$root" activated "$version" "$release_dir" || rc=4
  _rel_audit updated "$version" "$old_version" || rc=4
  if [[ "$rc" -ne 0 ]]; then
    _rel_err "release is active, but completion logging failed; reconcile current and audit history"
    return "$rc"
  fi
  _rel_ok "current -> $release_dir (version $version)"
}

_rel_do_update_locked() {
  local root="$1" archive="$2" checksum="${3:-}" signature="${4:-}" pubkey="${5:-}"
  local tool="${6:-}" config="${7:-}" version_override="${8:-}"
  [[ -n "$archive" && -f "$archive" ]] || {
    _rel_err "release archive not found: $archive"
    return 2
  }
  rel_recover_transition "$root" || return $?

  echo "== [1/5] verify signed offline release bundle =="
  rel_verify_integrity "$archive" "$checksum" "$signature" "$pubkey" "$tool" || return 1

  echo "== [2/5] safely extract (local, bounded, no links) =="
  local stage source_dir wheelhouse version release_dir rc
  stage="$(mktemp -d "$root/releases/.stage.XXXXXX")" || return 1
  source_dir="$(rel_extract "$archive" "$stage")" || {
    rm -rf "$stage"
    return 1
  }
  wheelhouse="$source_dir/wheels"
  version="$(rel_declared_version "$source_dir" "$wheelhouse")" || {
    rm -rf "$stage"
    return 1
  }
  if [[ -n "$version_override" ]]; then
    rel_validate_version "$version_override" || {
      rm -rf "$stage"
      return 1
    }
    if [[ "$version_override" != "$version" ]]; then
      _rel_err "--release-version must match the signed VERSION ($version)"
      rm -rf "$stage"
      return 1
    fi
  fi
  release_dir="$root/releases/$version"
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    _rel_err "immutable release version already exists: $release_dir"
    rm -rf "$stage"
    return 3
  fi

  echo "== [3/5] build new release in place: $release_dir =="
  mkdir -p "$release_dir" || {
    rm -rf "$stage"
    return 1
  }
  if ! rel_build_release "$release_dir" "$wheelhouse" "$version"; then
    _rel_err "build failed; removing incomplete release and keeping current"
    rm -rf "$release_dir" "$stage"
    return 1
  fi
  if ! {
    printf 'version=%s\n' "$version"
    printf 'applied_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'source_archive=%s\n' "$(basename "$archive")"
    printf 'source_sha256=%s\n' "$(sha256_of "$archive")"
    printf 'verify_method=%s\n' "${REL_VERIFY_METHOD:-unknown}"
  } > "$release_dir/RELEASE"; then
    _rel_err "could not write release metadata"
    rm -rf "$release_dir" "$stage"
    return 1
  fi
  chmod 0600 "$release_dir/RELEASE" || {
    rm -rf "$release_dir" "$stage"
    return 1
  }
  chmod -R go-w "$release_dir" 2>/dev/null || true

  echo "== [4/5] smoke-check new release before flip =="
  if ! rel_smoke "$release_dir" "$config"; then
    _rel_err "smoke failed; removing incomplete release and keeping current"
    rm -rf "$release_dir" "$stage"
    return 1
  fi

  if rel_migrate_legacy_layout "$root"; then
    :
  else
    rc=$?
    _rel_err "legacy layout migration failed; removing the inactive new release"
    rm -rf "$release_dir" "$stage"
    return "$rc"
  fi

  echo "== [5/5] atomic activation =="
  if rel_activate "$root" "$release_dir"; then
    rc=0
  else
    rc=$?
  fi
  rm -rf "$stage"
  if [[ "$rc" -ne 0 ]]; then
    if [[ "$(rel_current_path "$root")" != "$release_dir" ]]; then
      rm -rf "$release_dir"
    fi
    return "$rc"
  fi
  printf '\nUPDATE OK: now running %s. Roll back with install.sh --rollback --config <cfg>\n' "$version"
}

rel_do_update() {
  local root="$1" rc
  _rel_lock_acquire "$root" || return 1
  if _rel_do_update_locked "$@"; then
    rc=0
  else
    rc=$?
  fi
  _rel_lock_release
  return "$rc"
}

_rel_do_rollback_locked() {
  local root="$1" config="${2:-}"
  local previous current previous_version current_version rc=0
  rel_recover_transition "$root" || return $?
  previous="$(rel_previous_path "$root")"
  current="$(rel_current_path "$root")"
  [[ -n "$previous" && -n "$current" ]] || {
    _rel_err "both current and previous releases are required for rollback"
    return 1
  }
  rel_validate_release_dir "$root" "$previous" || {
    _rel_err "previous release pointer is outside the managed release directory or incomplete"
    return 1
  }
  rel_validate_release_dir "$root" "$current" || {
    _rel_err "current release pointer is outside the managed release directory or incomplete"
    return 1
  }
  [[ "$previous" != "$current" ]] || {
    _rel_err "current and previous point to the same release"
    return 1
  }
  previous_version="$(rel_version_of "$previous")" || return 1
  current_version="$(rel_version_of "$current")" || return 1
  rel_smoke "$previous" "$config" || {
    _rel_err "rollback target failed smoke validation; current is unchanged"
    return 1
  }
  _rel_audit rollback_prepared "$previous_version" "$current_version" || return 1

  atomic_symlink "current/venv" "$root/venv" || return 1
  atomic_symlink "$current" "$root/rollback-forward" || return 1
  if ! atomic_symlink "$previous" "$root/current"; then
    rm -f "$root/rollback-forward" 2>/dev/null || true
    return 1
  fi
  if ! atomic_symlink "$current" "$root/previous"; then
    _rel_err "rollback current changed but roll-forward pointer needs recovery; rerun the command"
    return 4
  fi
  rm -f "$root/rollback-forward" || {
    _rel_err "rollback completed but recovery marker could not be removed"
    return 4
  }

  _rel_history_append "$root" rolledback "$previous_version" "$previous" || rc=4
  _rel_audit rolledback "$previous_version" "$current_version" || rc=4
  if [[ "$rc" -ne 0 ]]; then
    _rel_err "rollback is active, but completion logging failed; reconcile current and audit history"
    return "$rc"
  fi
  _rel_ok "rolled back: current -> $previous (version $previous_version)"
  printf '\nROLLBACK OK: now running %s.\n' "$previous_version"
}

rel_do_rollback() {
  local root="$1" rc
  _rel_lock_acquire "$root" || return 1
  if _rel_do_rollback_locked "$@"; then
    rc=0
  else
    rc=$?
  fi
  _rel_lock_release
  return "$rc"
}
