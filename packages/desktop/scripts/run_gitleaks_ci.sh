#!/usr/bin/env bash
set -euo pipefail

readonly ZERO_SHA="0000000000000000000000000000000000000000"
readonly SHA_PATTERN='^[0-9a-f]{40}$'

event_name="${GITHUB_EVENT_NAME:-}"
base_sha="${GITLEAKS_BASE_SHA:-}"
head_sha="${GITLEAKS_HEAD_SHA:-}"
tree_sha="${GITLEAKS_TREE_SHA:-}"

case "${event_name}" in
  pull_request | push) ;;
  *)
    printf 'Unsupported Gitleaks event: %s\n' "${event_name:-<unset>}" >&2
    exit 2
    ;;
esac

for value_name in base_sha head_sha tree_sha; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ${SHA_PATTERN} ]]; then
    printf '%s must be one full lowercase commit SHA.\n' "${value_name}" >&2
    exit 2
  fi
done

if [[ "${event_name}" == "pull_request" && "${base_sha}" == "${ZERO_SHA}" ]]; then
  printf 'A pull request must have a nonzero base SHA.\n' >&2
  exit 2
fi

for commit in "${head_sha}" "${tree_sha}"; do
  if ! git cat-file -e "${commit}^{commit}" 2>/dev/null; then
    printf 'Required commit is not available: %s\n' "${commit}" >&2
    exit 2
  fi
done
if [[ "${base_sha}" != "${ZERO_SHA}" ]] \
  && ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
  printf 'Required base commit is not available: %s\n' "${base_sha}" >&2
  exit 2
fi

checked_out_sha="$(git rev-parse HEAD)"
if [[ "${checked_out_sha}" != "${tree_sha}" ]]; then
  printf 'Checked-out tree %s does not match tested tree %s.\n' \
    "${checked_out_sha}" "${tree_sha}" >&2
  exit 2
fi

if [[ "${base_sha}" == "${ZERO_SHA}" ]]; then
  # A branch-creation push owns all commits reachable from its new head.
  log_options="${head_sha}"
else
  log_options="${base_sha}..${head_sha}"
fi

printf 'Scanning owned commit range: %s\n' "${log_options}"
gitleaks git . --log-opts="${log_options}" --redact --no-banner

printf 'Scanning checked-out tree: %s\n' "${tree_sha}"
tree_directory="$(mktemp -d)"
trap 'rm -rf "${tree_directory}"' EXIT
git archive "${tree_sha}" | tar -x -C "${tree_directory}"
gitleaks dir "${tree_directory}" --redact --no-banner
