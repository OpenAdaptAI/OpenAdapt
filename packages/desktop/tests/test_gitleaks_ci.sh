#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
runner="${repo_root}/scripts/run_gitleaks_ci.sh"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

new_repo() {
  local path="$1"
  git init --quiet "${path}"
  git -C "${path}" config user.name "Gitleaks CI test"
  git -C "${path}" config user.email "gitleaks-ci@example.invalid"
  printf 'safe\n' > "${path}/tracked.txt"
  git -C "${path}" add tracked.txt
  git -C "${path}" commit --quiet -m "base"
}

run_scan() {
  local path="$1"
  local base_sha="$2"
  local head_sha="$3"
  (
    cd "${path}"
    GITHUB_EVENT_NAME=pull_request \
      GITLEAKS_BASE_SHA="${base_sha}" \
      GITLEAKS_HEAD_SHA="${head_sha}" \
      GITLEAKS_TREE_SHA="${head_sha}" \
      "${runner}"
  )
}

# The range scan must find a secret that was added and then removed by the PR.
# The final tree is clean, so this fails only when the owned commit range is read.
range_repo="${test_root}/owned-range"
new_repo "${range_repo}"
range_base="$(git -C "${range_repo}" rev-parse HEAD)"
printf 'token = ghp_%s%s\n' 'R4ng3Own3dS3cr3tMustFail123456' '789012' \
  > "${range_repo}/temporary-secret.txt"
git -C "${range_repo}" add temporary-secret.txt
git -C "${range_repo}" commit --quiet -m "introduce test secret"
git -C "${range_repo}" rm --quiet temporary-secret.txt
git -C "${range_repo}" commit --quiet -m "remove test secret"
range_head="$(git -C "${range_repo}" rev-parse HEAD)"
if run_scan "${range_repo}" "${range_base}" "${range_head}"; then
  printf 'The owned-range secret was not detected.\n' >&2
  exit 1
fi

# A secret reachable only from an unrelated fetched ref must not poison the PR.
ref_repo="${test_root}/unrelated-ref"
new_repo "${ref_repo}"
ref_base="$(git -C "${ref_repo}" rev-parse HEAD)"
git -C "${ref_repo}" switch --quiet -c unrelated
printf 'token = ghp_%s%s\n' 'Unr3lat3dS3cr3tMustNotFail12345' '678901' \
  > "${ref_repo}/unrelated-secret.txt"
git -C "${ref_repo}" add unrelated-secret.txt
git -C "${ref_repo}" commit --quiet -m "unrelated secret"
git -C "${ref_repo}" switch --quiet -c pull-request "${ref_base}"
printf 'safe PR change\n' >> "${ref_repo}/tracked.txt"
git -C "${ref_repo}" commit --quiet -am "safe pull request"
ref_head="$(git -C "${ref_repo}" rev-parse HEAD)"
run_scan "${ref_repo}" "${ref_base}" "${ref_head}"

# The tree scan remains active even when the owned commit range is empty.
printf 'token = ghp_%s%s\n' 'Curr3ntTr33S3cr3tMustFail123456' '789012' \
  > "${ref_repo}/current-tree-secret.txt"
git -C "${ref_repo}" add current-tree-secret.txt
git -C "${ref_repo}" commit --quiet -m "current tree secret"
tree_head="$(git -C "${ref_repo}" rev-parse HEAD)"
if run_scan "${ref_repo}" "${tree_head}" "${tree_head}"; then
  printf 'The current-tree secret was not detected.\n' >&2
  exit 1
fi

printf 'Gitleaks commit-range and tree self-tests passed.\n'
