"""Read-only verifier for accidental agent changes to safety-critical paths.

This file is run only from the trusted base by a ``pull_request_target``
workflow. It reads GitHub metadata and comments; it never checks out, imports,
or executes pull-request-head code. The shared-account declarations it checks
are evidence-presence controls, not cryptographic proof or a defense against a
malicious actor.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.ci.attestation import (
    EvidenceError,
    declaration_text,
    head_reference_lines,
    parse_author_declaration,
    parse_precommit_report,
    parse_review_report,
    review_headings,
    validate_independent_review,
)

REPOSITORY = "OpenAdaptAI/OpenAdapt"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "agent-safety-manifest.json"
)
CHECK_NAME = "protected-safety-review"
SHA = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_PATH = ".github/workflows/protected-safety-review.yml"


def _fail(message: str) -> list[str]:
    return [message]


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "description",
        "areas",
    }:
        raise EvidenceError("safety manifest is missing or has unknown fields")
    if manifest["schema_version"] != 1 or not isinstance(manifest["areas"], list):
        raise EvidenceError("unsupported safety manifest schema")
    required_areas = {
        "verification-authority",
        "fail-closed-policy-and-approval",
        "receipts-and-signing",
        "refusal-and-ledger",
        "frozen-study-guard",
        "safety-gate-itself",
    }
    seen: set[str] = set()
    for area in manifest["areas"]:
        if not isinstance(area, dict) or set(area) != {"id", "rationale", "paths"}:
            raise EvidenceError(
                "manifest areas must have exactly id, rationale, and paths"
            )
        area_id = area["id"]
        if not isinstance(area_id, str) or area_id in seen:
            raise EvidenceError("manifest area ids must be unique strings")
        seen.add(area_id)
        if not isinstance(area["rationale"], str) or not area["rationale"].strip():
            raise EvidenceError(f"manifest area {area_id!r} needs a rationale")
        paths = area["paths"]
        if not isinstance(paths, list) or not paths:
            raise EvidenceError(f"manifest area {area_id!r} needs paths")
        if not all(
            isinstance(path, str)
            and path
            and not path.startswith("/")
            and ".." not in Path(path).parts
            for path in paths
        ):
            raise EvidenceError(f"manifest area {area_id!r} contains an invalid path")
    if seen != required_areas:
        raise EvidenceError("manifest must enumerate all six required safety areas")
    required_sources = {
        "verification-authority": {
            "packages/flow/openadapt_flow/verifier.py",
            "packages/flow/openadapt_flow/verify_run.py",
            "packages/flow/openadapt_flow/runtime/identity*.py",
            "packages/flow/openadapt_flow/identity_signals.py",
            "packages/flow/openadapt_flow/ir.py",
            "packages/flow/openadapt_flow/runtime/replayer.py",
            "packages/flow/openadapt_flow/runtime/effects/**",
        },
        "fail-closed-policy-and-approval": {
            "packages/flow/openadapt_flow/policy.py",
            "packages/flow/openadapt_flow/approval_mode/**",
        },
        "receipts-and-signing": {"packages/flow/openadapt_flow/write_receipt.py"},
        "refusal-and-ledger": {"packages/flow/openadapt_flow/write_ledger.py"},
        "frozen-study-guard": {"packages/flow/tests/test_hardening_flywheel.py"},
    }
    for area in manifest["areas"]:
        required_area_sources = required_sources.get(area["id"], set())
        if not required_area_sources <= set(area["paths"]):
            raise EvidenceError(
                f"manifest area {area['id']!r} omits canonical source paths"
            )
    gate_paths = {
        path
        for area in manifest["areas"]
        if area["id"] == "safety-gate-itself"
        for path in area["paths"]
    }
    required_gate_paths = {
        ".github/agent-safety-manifest.json",
        ".github/workflows/protected-safety-review.yml",
        "scripts/ci/protected_safety_review.py",
        "scripts/ci/attestation.py",
        "docs/agent-safety-evidence-v1.md",
        "tests/test_precommit_attestation.py",
        "tests/test_protected_safety_review.py",
    }
    if not required_gate_paths <= gate_paths:
        raise EvidenceError(
            "manifest must protect its implementation, workflow, docs, and tests"
        )
    return manifest


def _manifest_paths(manifest: dict[str, Any]) -> list[str]:
    return [path for area in manifest["areas"] for path in area["paths"]]


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _headers(body: str, key: str) -> list[str]:
    return re.findall(rf"(?m)^{re.escape(key)}: (.*)$", declaration_text(body))


def _current_comment(body: Any, head: str) -> bool:
    if not isinstance(body, str):
        return False
    # Selection must retain ambiguous or malformed declarations that refer to
    # this head. Only the strict parser may decide whether evidence is valid.
    mentions = re.findall(
        r"(?m)^ {0,3}Reviewed-Head[ \t]*:[ \t]*(.*)$", declaration_text(body)
    )
    return any(
        re.search(rf"(?i)(?<![0-9a-f]){re.escape(head)}(?![0-9a-f])", value)
        for value in mentions
    )


def _review_needs_validation(body: str, head: str) -> bool:
    """Ignore only a proven stale review, not an unknown declaration shape."""
    heads = _headers(body, "Reviewed-Head")
    if (
        len(heads) != 1
        or len(head_reference_lines(body)) != 1
        or not SHA.fullmatch(heads[0].strip())
    ):
        return True
    if heads[0].strip() == head:
        return True
    visible = unicodedata.normalize("NFKC", declaration_text(body)).casefold()
    references = re.findall(r"[0-9a-f]{8,}", visible)
    return any(head.startswith(value) or value.startswith(head) for value in references)


TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def _trusted_comment(comment: dict[str, Any]) -> bool:
    user = comment.get("user")
    return (
        isinstance(user, dict)
        and isinstance(user.get("login"), str)
        and comment.get("author_association") in TRUSTED_ASSOCIATIONS
    )


def _unedited_comment(comment: dict[str, Any]) -> bool:
    created = comment.get("created_at")
    return isinstance(created, str) and created == comment.get("updated_at")


def _review_and_attestation_errors(
    *,
    comments: list[dict[str, Any]],
    head: str,
    commits: list[dict[str, Any]],
    coordinator_needed: bool,
    pull_request_author: str,
) -> list[str]:
    errors: list[str] = []
    trusted_comments = [c for c in comments if _trusted_comment(c)]
    all_bodies = [c.get("body") for c in trusted_comments]
    current = [body for body in all_bodies if _current_comment(body, head)]
    authors: list[tuple[str, str]] = []
    reviewers: list[tuple[str, str, str]] = []
    coordinator_notes: list[str] = []
    for comment, body in zip(trusted_comments, all_bodies, strict=True):
        if not isinstance(body, str):
            continue
        if review_headings(body):
            if not _review_needs_validation(body, head):
                continue
            unedited = _unedited_comment(comment)
            if not unedited:
                errors.append(
                    "edited current independent review cannot authorize success"
                )
            try:
                vendor, verdict = parse_review_report(body, head)
                if _headers(body, "Coordinator-Approval"):
                    raise EvidenceError(
                        "review must be separate from a coordinator note"
                    )
                if unedited:
                    reviewers.append((vendor, verdict, body))
            except EvidenceError as exc:
                errors.append(f"invalid independent review: {exc}")
            continue
        if not _unedited_comment(comment) or body not in current:
            continue
        if _headers(body, "Coordinator-Approval"):
            try:
                vendor = parse_author_declaration(body, head)
                approval = _headers(body, "Coordinator-Approval")
                if (
                    len(approval) != 1
                    or approval[0] != "APPROVE"
                    or vendor.casefold() != "coordinator"
                ):
                    raise EvidenceError(
                        "coordinator note needs From: Coordinator and Coordinator-Approval: APPROVE"
                    )
                coordinator_notes.append(body)
            except EvidenceError as exc:
                errors.append(f"invalid coordinator note: {exc}")
            continue
        else:
            try:
                vendor = parse_author_declaration(body, head)
                if comment.get("user", {}).get("login", "").casefold() != (
                    pull_request_author.casefold()
                ):
                    raise EvidenceError(
                        "author declaration must be posted by the pull request author"
                    )
                authors.append((vendor, body))
            except EvidenceError as exc:
                errors.append(f"invalid author declaration: {exc}")

    if len(authors) != 1:
        errors.append("expected exactly one current exact-head author declaration")
    if len(reviewers) != 1:
        errors.append("expected exactly one current exact-head independent review")
    if len(authors) == 1 and len(reviewers) == 1:
        try:
            validate_independent_review(authors[0][1], reviewers[0][2], head)
        except (EvidenceError, StopIteration) as exc:
            errors.append(f"independent review mismatch: {exc}")
    if coordinator_needed and len(coordinator_notes) != 1:
        errors.append(
            "protected gate files require exactly one current coordinator approval"
        )

    # The GitHub API commit list supplies parent/tree/time metadata. Each
    # per-commit declaration must bind to those exact values.
    for commit in commits:
        sha = commit.get("sha")
        metadata = commit.get("commit", {})
        parents = [p.get("sha") for p in commit.get("parents", [])]
        tree = metadata.get("tree", {}).get("sha")
        date_value = metadata.get("committer", {}).get("date")
        if not isinstance(sha, str) or not SHA.fullmatch(sha):
            errors.append("GitHub returned malformed commit metadata")
            continue
        # Python 3.10's fromisoformat rejects the "Z" suffix GitHub uses.
        if isinstance(date_value, str) and date_value.endswith("Z"):
            date_value = date_value[:-1] + "+00:00"
        try:
            commit_time = datetime.fromisoformat(date_value)
        except (TypeError, ValueError):
            errors.append(f"commit {sha} has no valid committer timestamp")
            continue
        matches: list[str] = []
        for comment, body in zip(trusted_comments, all_bodies, strict=True):
            if not isinstance(body, str):
                continue
            if not _unedited_comment(comment):
                continue
            candidates = re.findall(r'"commit"\s*:\s*"([0-9a-f]{40})"', body)
            if sha in candidates:
                matches.append(body)
        if len(matches) != 1:
            errors.append(f"commit {sha} needs exactly one current v1 precommit report")
            continue
        try:
            parse_precommit_report(
                matches[0],
                expected_commit=sha,
                expected_parents=parents,
                expected_tree=tree,
                commit_time=commit_time,
            )
        except EvidenceError as exc:
            errors.append(f"invalid precommit report for {sha}: {exc}")
    return errors


def evaluate_pull_request(
    *,
    repository: dict[str, Any],
    pull_request: dict[str, Any],
    main_sha: str,
    files: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    manifest: Any,
) -> list[str]:
    """Return fail-closed errors for a set of fixed GitHub API responses."""
    try:
        validated = validate_manifest(manifest)
    except EvidenceError as exc:
        return [f"invalid trusted-base manifest: {exc}"]
    errors: list[str] = []
    if repository.get("full_name") != REPOSITORY:
        return _fail("workflow event repository does not match this repository")
    if repository.get("default_branch") != "main":
        errors.append("repository default branch is not main")
    if pull_request.get("state") != "open":
        errors.append("pull request is not open")
    base = pull_request.get("base", {})
    head = pull_request.get("head", {})
    if base.get("repo", {}).get("full_name") != REPOSITORY:
        errors.append("pull request base repository is not this repository")
    if base.get("ref") != "main":
        errors.append("pull request base must be main")
    if not isinstance(main_sha, str) or not SHA.fullmatch(main_sha):
        errors.append("current main reference is missing or malformed")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        errors.append("pull request head SHA is missing or malformed")
    if (
        not isinstance(head.get("repo", {}).get("full_name"), str)
        or not head.get("repo", {}).get("full_name", "").strip()
    ):
        errors.append("pull request head repository metadata is missing")
    author = pull_request.get("user")
    if not isinstance(author, dict) or not isinstance(author.get("login"), str):
        errors.append("pull request author metadata is missing")
    if errors:
        return errors

    expected_file_count = pull_request.get("changed_files")
    expected_commit_count = pull_request.get("commits")
    if type(expected_file_count) is not int or expected_file_count < 0:
        errors.append("pull request changed-file count is missing or malformed")
    elif len(files) != expected_file_count:
        errors.append("changed-file API results are incomplete")
    if type(expected_commit_count) is not int or expected_commit_count < 0:
        errors.append("pull request commit count is missing or malformed")
    elif expected_commit_count > 250 or len(commits) != expected_commit_count:
        errors.append("commit API results are incomplete or exceed the API limit")
    commit_shas = [commit.get("sha") for commit in commits]
    if not all(isinstance(sha, str) and sha for sha in commit_shas):
        errors.append("commit API results contain malformed commit ids")
    elif len(set(commit_shas)) != len(commit_shas):
        errors.append("commit API results contain duplicate commit ids")
    filenames = [item.get("filename") for item in files]
    if not all(isinstance(filename, str) and filename for filename in filenames):
        errors.append("changed-file API results contain malformed paths")
    elif len(set(filenames)) != len(filenames):
        errors.append("changed-file API results contain duplicate paths")
    if errors:
        return errors

    paths = _manifest_paths(validated)
    changed_paths: list[str] = []
    for item in files:
        filename = item.get("filename")
        previous = item.get("previous_filename")
        if not isinstance(filename, str) or not filename:
            return _fail("changed-file metadata is empty or malformed")
        changed_paths.append(filename)
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                return _fail("renamed-file metadata is malformed")
            changed_paths.append(previous)
    if expected_file_count == 0 and files:
        return _fail("changed-file API results are inconsistent with the PR")
    if expected_file_count and not changed_paths:
        return _fail("changed-file metadata is empty or malformed")
    protected = any(
        _matches(path, pattern) for path in changed_paths for pattern in paths
    )
    if not protected:
        return []
    if not commits:
        return _fail("protected change has no commit metadata")
    gate_paths = next(
        area["paths"]
        for area in validated["areas"]
        if area["id"] == "safety-gate-itself"
    )
    coordinator_needed = any(
        _matches(path, pattern) for path in changed_paths for pattern in gate_paths
    )
    errors.extend(
        _review_and_attestation_errors(
            comments=comments,
            head=head_sha,
            commits=commits,
            coordinator_needed=coordinator_needed,
            pull_request_author=pull_request.get("user", {}).get("login", ""),
        )
    )
    return errors


class GitHubApi:
    def __init__(self, token: str, repository: str = REPOSITORY):
        self.token = token
        self.repository = repository
        self.base_url = f"https://api.github.com/repos/{repository}"

    def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
        payload = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except Exception:  # noqa: BLE001 - transport errors can contain credentials
            raise RuntimeError("GitHub API request failed") from None

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except Exception:  # noqa: BLE001 - transport errors can contain credentials
            raise RuntimeError("GitHub GraphQL request failed") from None
        if not isinstance(result, dict) or result.get("errors"):
            raise RuntimeError("GitHub GraphQL returned errors")
        return result.get("data")

    def pages(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(1, 21):
            batch = self.request(
                f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
            )
            if not isinstance(batch, list):
                raise TypeError(f"GitHub API returned a non-list for {path}")
            results.extend(batch)
            if len(batch) < 100:
                return results
        raise RuntimeError(f"GitHub API pagination limit reached for {path}")


def _pull_request_number(event: dict[str, Any]) -> int:
    if isinstance(event.get("pull_request"), dict):
        number = event["pull_request"].get("number")
    else:
        issue = event.get("issue", {})
        if not isinstance(issue, dict) or not isinstance(
            issue.get("pull_request"), dict
        ):
            raise EvidenceError("event is not associated with a pull request")
        number = issue.get("number")
    if type(number) is not int or number <= 0:
        raise EvidenceError("pull request number is missing")
    return number


def _comment_snapshot(comments: list[dict[str, Any]]) -> list[str]:
    fields = ("id", "body", "author_association", "created_at", "updated_at")
    snapshot = []
    for comment in comments:
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        value = {field: comment.get(field) for field in fields}
        value["login"] = user.get("login")
        snapshot.append(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return sorted(snapshot)


MERGE_QUEUE_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      configuration { mergeMethod }
      entries(first: 100) {
        totalCount
        pageInfo { hasNextPage }
        nodes {
          position
          state
          baseCommit { oid }
          headCommit { oid }
          pullRequest { number state baseRefName headRefOid }
        }
      }
    }
  }
}
"""


def _merge_group_entries(api: Any, group: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a merge-group SHA through GitHub's queue-entry commit chain."""
    base_sha = group.get("base_sha")
    head_sha = group.get("head_sha")
    base_ref = group.get("base_ref")
    if base_ref not in {"main", "refs/heads/main"}:
        raise EvidenceError("merge group base ref must be main")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        raise EvidenceError("merge group base SHA is missing or malformed")
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise EvidenceError("merge group head SHA is missing or malformed")

    data = api.graphql(
        MERGE_QUEUE_QUERY,
        {"owner": "OpenAdaptAI", "name": "OpenAdapt", "branch": "main"},
    )
    repository = data.get("repository") if isinstance(data, dict) else None
    queue = repository.get("mergeQueue") if isinstance(repository, dict) else None
    entries = queue.get("entries") if isinstance(queue, dict) else None
    if not isinstance(entries, dict):
        raise EvidenceError("GitHub returned no merge queue entry mapping")
    configuration = queue.get("configuration")
    if (
        not isinstance(configuration, dict)
        or configuration.get("mergeMethod") != "MERGE"
    ):
        raise EvidenceError(
            "merge queue commit-chain mapping is supported only for MERGE queues"
        )
    nodes = entries.get("nodes")
    total = entries.get("totalCount")
    page_info = entries.get("pageInfo")
    if (
        not isinstance(nodes, list)
        or type(total) is not int
        or total != len(nodes)
        or not isinstance(page_info, dict)
        or page_info.get("hasNextPage") is not False
    ):
        raise EvidenceError("merge queue API results are incomplete")

    by_head: dict[str, list[dict[str, Any]]] = {}
    for entry in nodes:
        if not isinstance(entry, dict):
            raise EvidenceError("merge queue contains a malformed entry")
        base = entry.get("baseCommit", {}).get("oid")
        head = entry.get("headCommit", {}).get("oid")
        if not isinstance(base, str) or not SHA.fullmatch(base):
            raise EvidenceError("merge queue entry has a malformed base commit")
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise EvidenceError("merge queue entry has a malformed head commit")
        by_head.setdefault(head, []).append(entry)

    chain: list[dict[str, Any]] = []
    cursor = head_sha
    seen: set[str] = set()
    while cursor != base_sha:
        if cursor in seen:
            raise EvidenceError("merge queue entry chain contains a cycle")
        seen.add(cursor)
        matches = by_head.get(cursor, [])
        if len(matches) != 1:
            raise EvidenceError("merge group SHA has no unique queue-entry mapping")
        entry = matches[0]
        chain.append(entry)
        cursor = entry["baseCommit"]["oid"]
        if len(chain) > len(nodes):
            raise EvidenceError("merge queue entry chain is incomplete")
    if not chain:
        raise EvidenceError("merge group contains no queued pull requests")

    selected: list[dict[str, Any]] = []
    for entry in reversed(chain):
        if entry.get("state") not in {"AWAITING_CHECKS", "MERGEABLE", "LOCKED"}:
            raise EvidenceError("merge group contains an inactive or blocked entry")
        pull_request = entry.get("pullRequest")
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        if type(number) is not int or number <= 0:
            raise EvidenceError("merge queue entry has no pull request")
        if (
            pull_request.get("state") != "OPEN"
            or pull_request.get("baseRefName") != "main"
        ):
            raise EvidenceError(
                "merge queue contains a closed or non-main pull request"
            )
        if any(item["pullRequest"]["number"] == number for item in selected):
            raise EvidenceError("merge queue maps a pull request more than once")
        selected.append(entry)
    return selected


def _workflow_provenance_errors(
    workflow_run: Any,
    *,
    event_name: str | None,
    workflow_attempt: str | None,
    workflow_sha: str | None,
) -> list[str]:
    if workflow_run is None:
        return []
    errors: list[str] = []
    if not isinstance(workflow_run, dict):
        return ["current workflow run metadata is malformed"]
    if not isinstance(workflow_sha, str) or not SHA.fullmatch(workflow_sha):
        errors.append("workflow event SHA is missing or malformed")
    if workflow_run.get("path") != WORKFLOW_PATH:
        errors.append("check was not produced by the trusted safety workflow")
    if workflow_run.get("repository", {}).get("full_name") != REPOSITORY:
        errors.append("workflow run belongs to a different repository")
    if event_name and workflow_run.get("event") != event_name:
        errors.append("workflow run event does not match the triggering event")
    if workflow_attempt:
        try:
            expected_attempt = int(workflow_attempt)
        except ValueError:
            expected_attempt = -1
        if workflow_run.get("run_attempt") != expected_attempt:
            errors.append("workflow run attempt does not match this execution")
    if workflow_sha and workflow_run.get("head_sha") != workflow_sha:
        errors.append("workflow run SHA does not match the triggering event SHA")
    return errors


def _trusted_workflow_source_errors(
    api: Any, *, workflow_sha: str, current_main_sha: str
) -> list[str]:
    """Require the recorded trusted workflow source to belong to current main.

    The merge-group run re-evaluates current PR evidence with its trusted
    checkout, so a verdict need not have run at main's latest tip.
    """
    if not SHA.fullmatch(workflow_sha) or not SHA.fullmatch(current_main_sha):
        return ["safety verdict workflow source SHA is missing or malformed"]
    comparison = api.request(f"/compare/{workflow_sha}...{current_main_sha}")
    merge_base_commit = (
        comparison.get("merge_base_commit") if isinstance(comparison, dict) else None
    )
    merge_base = (
        merge_base_commit.get("sha") if isinstance(merge_base_commit, dict) else None
    )
    if (
        not isinstance(comparison, dict)
        or comparison.get("status") not in {"ahead", "identical"}
        or merge_base != workflow_sha
    ):
        return ["safety verdict workflow source is not an ancestor of current main"]
    return []


def _publish_check(
    api: Any,
    *,
    head_sha: str,
    errors: list[str],
    workflow_run_id: str | None,
    workflow_url: str | None,
) -> tuple[str, list[str]]:
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise EvidenceError("cannot publish status: target head SHA is malformed")
    conclusion = "failure" if errors else "success"
    summary = (
        "\n".join(f"- {error}" for error in errors)
        if errors
        else "Current protected-path evidence is present. This is an evidence-presence check, not proof of execution."
    )
    payload: dict[str, Any] = {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": "Protected safety review", "summary": summary},
    }
    if workflow_run_id:
        payload["external_id"] = workflow_run_id
    if workflow_url:
        payload["details_url"] = workflow_url
    api.request("/check-runs", method="POST", data=payload)
    return conclusion, errors


def _fetch_pr_evidence(
    api: Any,
    number: int,
    *,
    repository: dict[str, Any],
    main_sha: str,
    manifest: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    pull_request = api.request(f"/pulls/{number}")
    files = api.pages(f"/pulls/{number}/files")
    commits = api.pages(f"/pulls/{number}/commits")
    comments = api.pages(f"/issues/{number}/comments")
    errors = evaluate_pull_request(
        repository=repository,
        pull_request=pull_request,
        main_sha=main_sha,
        files=files,
        commits=commits,
        comments=comments,
        manifest=manifest,
    )
    return pull_request, comments, errors


def _passed_pr_head_verdict(
    api: Any,
    pull_request: dict[str, Any],
    *,
    head_sha: str,
    main_sha: str,
) -> list[str]:
    """Require a current trusted workflow success for one exact PR head."""
    number = pull_request.get("number")
    base = pull_request.get("base", {})
    if type(number) is not int or number <= 0:
        return ["pull request number is missing or malformed"]
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        return [f"PR #{number} head SHA is missing or malformed"]
    if base.get("repo", {}).get("full_name") != REPOSITORY or base.get("ref") != "main":
        return [f"PR #{number} is not based on this repository's main branch"]
    if pull_request.get("state") != "open":
        return [f"PR #{number} is not open"]

    result = api.request(
        f"/commits/{head_sha}/check-runs?check_name={CHECK_NAME}&per_page=100&page=1"
    )
    runs = result.get("check_runs") if isinstance(result, dict) else None
    total = result.get("total_count") if isinstance(result, dict) else None
    if (
        not isinstance(runs, list)
        or type(total) is not int
        or total != len(runs)
        or total > 100
    ):
        return [f"PR #{number} safety check results are incomplete"]
    named = [
        run for run in runs if isinstance(run, dict) and run.get("name") == CHECK_NAME
    ]
    if not named:
        return [f"PR #{number} has no completed {CHECK_NAME} head verdict"]
    latest = max(
        named,
        key=lambda run: str(run.get("started_at") or run.get("created_at") or ""),
    )
    if (
        latest.get("head_sha") != head_sha
        or latest.get("status") != "completed"
        or latest.get("conclusion") != "success"
        or latest.get("app", {}).get("slug") != "github-actions"
    ):
        return [f"PR #{number} latest {CHECK_NAME} head verdict is not trusted success"]

    external_id = latest.get("external_id")
    match = (
        re.fullmatch(r"([0-9]+):([0-9]+)", external_id)
        if isinstance(external_id, str)
        else None
    )
    if match is None:
        return [f"PR #{number} safety check lacks a workflow run binding"]
    run_id, attempt = match.groups()
    workflow_run = api.request(f"/actions/runs/{run_id}")
    event_name = workflow_run.get("event") if isinstance(workflow_run, dict) else None
    if event_name not in {"pull_request_target", "issue_comment"}:
        return [f"PR #{number} safety verdict came from an unsupported event"]
    source_sha = (
        workflow_run.get("head_sha") if isinstance(workflow_run, dict) else None
    )
    errors = _workflow_provenance_errors(
        workflow_run,
        event_name=event_name,
        workflow_attempt=attempt,
        workflow_sha=source_sha if isinstance(source_sha, str) else None,
    )
    if isinstance(source_sha, str) and isinstance(main_sha, str):
        errors.extend(
            _trusted_workflow_source_errors(
                api, workflow_sha=source_sha, current_main_sha=main_sha
            )
        )
    else:
        errors.append("safety verdict workflow source SHA is missing or malformed")
    if latest.get("details_url") != workflow_run.get("html_url"):
        errors.append("safety check details do not match its trusted workflow run")
    if errors:
        return [
            f"PR #{number} safety verdict provenance is invalid: {error}"
            for error in errors
        ]
    return []


def run(
    event: dict[str, Any],
    trusted_base_sha: str,
    api: Any,
    *,
    event_name: str | None = None,
    workflow_run_id: str | None = None,
    workflow_attempt: str | None = None,
    workflow_sha: str | None = None,
) -> tuple[str, list[str]]:
    """Evaluate PR-head evidence and publish a check from a trusted workflow."""
    if event_name is None:
        event_name = (
            "merge_group"
            if isinstance(event.get("merge_group"), dict)
            else "pull_request_target"
            if isinstance(event.get("pull_request"), dict)
            else "issue_comment"
        )
    if workflow_run_id and workflow_attempt:
        workflow_run_id = f"{workflow_run_id}:{workflow_attempt}"
    run_id = workflow_run_id.split(":", 1)[0] if workflow_run_id else None
    group = event.get("merge_group") if event_name == "merge_group" else None
    known_head = (
        group.get("head_sha")
        if isinstance(group, dict)
        else event.get("pull_request", {}).get("head", {}).get("sha")
    )
    head_sha = known_head if isinstance(known_head, str) else None
    errors: list[str] = []
    workflow_run: Any = None
    comments_by_pr: dict[int, list[str]] = {}
    initial_heads: dict[int, str] = {}
    try:
        if run_id:
            workflow_run = api.request(f"/actions/runs/{run_id}")
            errors.extend(
                _workflow_provenance_errors(
                    workflow_run,
                    event_name=event_name,
                    workflow_attempt=workflow_attempt,
                    workflow_sha=workflow_sha,
                )
            )
        if event.get("repository", {}).get("full_name", REPOSITORY) != REPOSITORY:
            errors.append("event repository does not match this repository")
        if not isinstance(trusted_base_sha, str) or not SHA.fullmatch(trusted_base_sha):
            errors.append("trusted workflow base SHA is missing or malformed")
        repository = api.request("")
        main = api.request("/git/ref/heads/main")
        main_sha = main.get("object", {}).get("sha")
        if event_name == "merge_group":
            if not isinstance(group, dict):
                raise EvidenceError("merge-group payload is missing")
            if main_sha != trusted_base_sha:
                errors.append("trusted workflow source is not the current main commit")
            group_entries = _merge_group_entries(api, group)
            numbers = [entry["pullRequest"]["number"] for entry in group_entries]
        else:
            group_entries = []
            number = _pull_request_number(event)
            numbers = [number]

        manifest_path = Path(os.environ.get("SAFETY_MANIFEST", str(MANIFEST_PATH)))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for number in numbers:
            pull_request, comments, pr_errors = _fetch_pr_evidence(
                api,
                number,
                repository=repository,
                main_sha=main_sha,
                manifest=manifest,
            )
            pr_head = pull_request.get("head", {}).get("sha")
            if not isinstance(pr_head, str) or not SHA.fullmatch(pr_head):
                errors.append(f"pull request {number} has a malformed head SHA")
            else:
                initial_heads[number] = pr_head
                if len(numbers) == 1 and head_sha is None:
                    head_sha = pr_head
            if (
                event_name == "pull_request_target"
                and pull_request.get("base", {}).get("sha") != trusted_base_sha
            ):
                errors.append("workflow did not run from the exact PR base SHA")
            if event_name == "merge_group":
                entry = next(
                    item
                    for item in group_entries
                    if item["pullRequest"]["number"] == number
                )
                if entry["pullRequest"].get("headRefOid") != pr_head:
                    errors.append(
                        f"pull request {number} head changed from its queue entry"
                    )
                errors.extend(
                    _passed_pr_head_verdict(
                        api,
                        pull_request,
                        head_sha=pr_head,
                        main_sha=main_sha,
                    )
                )
            comments_by_pr[number] = _comment_snapshot(comments)
            errors.extend(f"PR #{number}: {error}" for error in pr_errors)

        latest_repository = api.request("")
        latest_main = api.request("/git/ref/heads/main")
        if latest_repository.get("full_name") != REPOSITORY:
            errors.append("repository identity changed before status publication")
        if latest_repository.get("default_branch") != "main":
            errors.append("repository default branch is no longer main")
        if (
            event_name == "merge_group"
            and latest_main.get("object", {}).get("sha") != trusted_base_sha
        ):
            errors.append("trusted workflow source changed before status publication")
        for number in numbers:
            latest_pull_request = api.request(f"/pulls/{number}")
            if latest_pull_request.get("head", {}).get("sha") != initial_heads.get(
                number
            ):
                errors.append(
                    f"PR #{number} head changed during review; rerun on current head"
                )
            if latest_pull_request.get("state") != "open":
                errors.append(f"PR #{number} is no longer open")
            if (
                latest_pull_request.get("base", {}).get("repo", {}).get("full_name")
                != REPOSITORY
            ):
                errors.append(f"PR #{number} base repository changed")
            if latest_pull_request.get("base", {}).get("ref") != "main":
                errors.append(f"PR #{number} is no longer based on main")
            latest_comments = api.pages(f"/issues/{number}/comments")
            if _comment_snapshot(latest_comments) != comments_by_pr[number]:
                errors.append(
                    f"PR #{number} comments changed during review; rerun on current evidence"
                )
        if event_name == "issue_comment" and trusted_base_sha != main_sha:
            errors.append(
                "workflow was not checked out from its trusted default-branch SHA"
            )
        if event_name == "merge_group":
            for number in numbers:
                latest_pull_request = api.request(f"/pulls/{number}")
                latest_head = latest_pull_request.get("head", {}).get("sha")
                if isinstance(latest_head, str) and SHA.fullmatch(latest_head):
                    if latest_head != initial_heads.get(number):
                        errors.append(
                            f"PR #{number} head changed before merge-group publication"
                        )
                    errors.extend(
                        _passed_pr_head_verdict(
                            api,
                            latest_pull_request,
                            head_sha=latest_head,
                            main_sha=main_sha,
                        )
                    )
            latest_entries = _merge_group_entries(api, group)
            if [
                (
                    entry["baseCommit"]["oid"],
                    entry["headCommit"]["oid"],
                    entry["pullRequest"]["number"],
                    entry["pullRequest"].get("headRefOid"),
                    entry.get("state"),
                )
                for entry in latest_entries
            ] != [
                (
                    entry["baseCommit"]["oid"],
                    entry["headCommit"]["oid"],
                    entry["pullRequest"]["number"],
                    entry["pullRequest"].get("headRefOid"),
                    entry.get("state"),
                )
                for entry in group_entries
            ]:
                errors.append("merge-group queue mapping changed before publication")
    except EvidenceError as exc:
        # First-party validation errors contain canonical diagnostics, not transport values.
        errors.append(f"safety review could not verify GitHub metadata: {exc}")
    except Exception:  # noqa: BLE001 - metadata errors can contain response values
        errors.append("safety review could not verify GitHub metadata")
    if head_sha is None:
        raise EvidenceError("cannot publish failure: event contains no known check SHA")
    return _publish_check(
        api,
        head_sha=head_sha,
        errors=errors,
        workflow_run_id=workflow_run_id,
        workflow_url=(
            workflow_run.get("html_url") if isinstance(workflow_run, dict) else None
        ),
    )


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    token = os.environ.get("GITHUB_TOKEN")
    trusted_base_sha = os.environ.get("TRUSTED_BASE_SHA", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    workflow_run_id = os.environ.get("GITHUB_RUN_ID")
    workflow_attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    workflow_sha = os.environ.get("GITHUB_SHA")
    if (
        not event_path
        or not token
        or not workflow_run_id
        or not workflow_attempt
        or not event_name
        or not workflow_sha
    ):
        print(
            "missing GitHub event path, token, event name, run id/attempt, or SHA",
            file=sys.stderr,
        )
        return 2
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        conclusion, errors = run(
            event,
            trusted_base_sha,
            GitHubApi(token),
            event_name=event_name,
            workflow_run_id=workflow_run_id,
            workflow_attempt=workflow_attempt,
            workflow_sha=workflow_sha,
        )
    except Exception:  # noqa: BLE001 - never print transport or input exception values
        print("unable to publish protected safety review", file=sys.stderr)
        return 2
    print("protected-safety-review: " + conclusion)
    for error in errors:
        print("- " + error)
    return 0 if conclusion == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
