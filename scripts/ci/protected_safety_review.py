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
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.ci.attestation import (
    EvidenceError,
    parse_author_declaration,
    parse_precommit_report,
    parse_review_report,
    validate_independent_review,
)

REPOSITORY = "OpenAdaptAI/OpenAdapt"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "agent-safety-manifest.json"
)
CHECK_NAME = "protected-safety-review"
SHA = re.compile(r"^[0-9a-f]{40}$")


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
        "verification-authority": "packages/flow/openadapt_flow/verify_run.py",
        "fail-closed-policy-and-approval": "packages/flow/openadapt_flow/policy.py",
        "receipts-and-signing": "packages/flow/openadapt_flow/write_receipt.py",
        "refusal-and-ledger": "packages/flow/openadapt_flow/write_ledger.py",
        "frozen-study-guard": "packages/flow/tests/test_hardening_flywheel.py",
    }
    for area in manifest["areas"]:
        required_source = required_sources.get(area["id"])
        if required_source and required_source not in area["paths"]:
            raise EvidenceError(
                f"manifest area {area['id']!r} omits canonical source {required_source}"
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
    return re.findall(rf"(?m)^{re.escape(key)}: (.*)$", body)


def _current_comment(body: Any, head: str) -> bool:
    return isinstance(body, str) and _headers(body, "Reviewed-Head") == [head]


def _review_and_attestation_errors(
    *,
    comments: list[dict[str, Any]],
    head: str,
    commits: list[dict[str, Any]],
    coordinator_needed: bool,
) -> list[str]:
    errors: list[str] = []
    all_bodies = [c.get("body") for c in comments]
    current = [body for body in all_bodies if _current_comment(body, head)]
    authors: list[tuple[str, str]] = []
    reviewers: list[tuple[str, str]] = []
    coordinator_notes: list[str] = []
    for body in current:
        assert isinstance(body, str)
        if "Coordinator-Approval:" in body:
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
        if "## Independent adversarial review:" in body:
            try:
                vendor, verdict = parse_review_report(body, head)
                reviewers.append((vendor, verdict))
            except EvidenceError as exc:
                errors.append(f"invalid independent review: {exc}")
        elif _headers(body, "From") and _headers(body, "Reviewed-Head"):
            try:
                vendor = parse_author_declaration(body, head)
                authors.append((vendor, body))
            except EvidenceError as exc:
                errors.append(f"invalid author declaration: {exc}")

    if len(authors) != 1:
        errors.append("expected exactly one current exact-head author declaration")
    if len(reviewers) != 1:
        errors.append("expected exactly one current exact-head independent review")
    if len(authors) == 1 and len(reviewers) == 1:
        try:
            reviewer_body = next(
                body
                for body in current
                if isinstance(body, str)
                and "## Independent adversarial review:" in body
            )
            validate_independent_review(authors[0][1], reviewer_body, head)
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
        try:
            commit_time = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            errors.append(f"commit {sha} has no valid committer timestamp")
            continue
        matches: list[str] = []
        for body in all_bodies:
            if not isinstance(body, str):
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
    if base.get("sha") != main_sha:
        errors.append("pull request base is stale; update it to current main")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        errors.append("pull request head SHA is missing or malformed")
    if (
        not isinstance(head.get("repo", {}).get("full_name"), str)
        or not head.get("repo", {}).get("full_name", "").strip()
    ):
        errors.append("pull request head repository metadata is missing")
    if errors:
        return errors

    paths = _manifest_paths(validated)
    changed_paths = [item.get("filename") for item in files]
    if not changed_paths or not all(isinstance(p, str) and p for p in changed_paths):
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
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"GitHub API request failed for {path}: {exc}") from exc

    def pages(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for page in range(1, 21):
            batch = self.request(
                f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
            )
            if not isinstance(batch, list):
                raise RuntimeError(f"GitHub API returned a non-list for {path}")
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


def run(
    event: dict[str, Any], trusted_base_sha: str, api: Any
) -> tuple[str, list[str]]:
    """Fetch GitHub metadata, evaluate it, and publish the exact-head check."""
    number = _pull_request_number(event)
    pull_request = api.request(f"/pulls/{number}")
    head_sha = pull_request.get("head", {}).get("sha")
    errors: list[str]
    try:
        if pull_request.get("base", {}).get("ref") != "main":
            errors = ["pull request base must be main"]
        else:
            repository = api.request("")
            main = api.request("/git/ref/heads/main")
            files = api.pages(f"/pulls/{number}/files")
            commits = api.pages(f"/pulls/{number}/commits")
            comments = api.pages(f"/issues/{number}/comments")
            manifest_path = Path(os.environ.get("SAFETY_MANIFEST", str(MANIFEST_PATH)))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            errors = evaluate_pull_request(
                repository=repository,
                pull_request=pull_request,
                main_sha=main.get("object", {}).get("sha"),
                files=files,
                commits=commits,
                comments=comments,
                manifest=manifest,
            )
        if pull_request.get("base", {}).get("sha") != trusted_base_sha:
            errors.append("workflow did not run from the exact PR base SHA")
        latest_repository = api.request("")
        latest_pull_request = api.request(f"/pulls/{number}")
        latest_main = api.request("/git/ref/heads/main")
        if latest_repository.get("full_name") != REPOSITORY:
            errors.append("repository identity changed before status publication")
        if latest_repository.get("default_branch") != "main":
            errors.append("repository default branch is no longer main")
        if (
            latest_pull_request.get("base", {}).get("repo", {}).get("full_name")
            != REPOSITORY
        ):
            errors.append(
                "pull request base repository changed before status publication"
            )
        if latest_pull_request.get("head", {}).get("sha") != head_sha:
            errors.append(
                "pull request head changed during review; rerun on the current head"
            )
        if latest_pull_request.get("base", {}).get(
            "ref"
        ) != "main" or latest_pull_request.get("base", {}).get(
            "sha"
        ) != latest_main.get("object", {}).get("sha"):
            errors.append("pull request is no longer based on current main")
    except Exception as exc:  # fail closed on unavailable or malformed metadata
        errors = [f"safety review could not verify GitHub metadata: {exc}"]
    if not isinstance(head_sha, str) or not SHA.fullmatch(head_sha):
        raise EvidenceError("cannot publish status: pull request head SHA is malformed")
    conclusion = "failure" if errors else "success"
    summary = (
        "\n".join(f"- {error}" for error in errors)
        if errors
        else "All protected-path evidence is present for the exact current PR head."
    )
    api.request(
        "/check-runs",
        method="POST",
        data={
            "name": CHECK_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": "Protected safety review",
                "summary": summary,
            },
        },
    )
    return conclusion, errors


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    token = os.environ.get("GITHUB_TOKEN")
    trusted_base_sha = os.environ.get("TRUSTED_BASE_SHA", "")
    if not event_path or not token:
        print("missing GitHub event path or token", file=sys.stderr)
        return 2
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        conclusion, errors = run(event, trusted_base_sha, GitHubApi(token))
    except Exception as exc:
        print(f"unable to publish protected safety review: {exc}", file=sys.stderr)
        return 2
    print("protected-safety-review: " + conclusion)
    for error in errors:
        print("- " + error)
    return 0 if conclusion == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
