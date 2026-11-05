from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest

from scripts.ci.attestation import EvidenceError
from scripts.ci.protected_safety_review import (
    CHECK_NAME,
    MANIFEST_PATH,
    evaluate_pull_request,
    run,
    validate_manifest,
)

BASE = "a" * 40
HEAD = "b" * 40
TREE = "c" * 40
PARENT = BASE
COMMIT_TIME = "2026-11-05T18:00:00Z"


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _pull_request() -> dict[str, Any]:
    return {
        "state": "open",
        "base": {
            "ref": "main",
            "sha": BASE,
            "repo": {"full_name": "OpenAdaptAI/OpenAdapt"},
        },
        "head": {
            "sha": HEAD,
            "repo": {"full_name": "OpenAdaptAI/OpenAdapt"},
        },
        "user": {"login": "author-login"},
        "changed_files": 1,
        "commits": 1,
    }


def _commit() -> dict[str, Any]:
    return {
        "sha": HEAD,
        "parents": [{"sha": PARENT}],
        "commit": {
            "tree": {"sha": TREE},
            "committer": {"date": COMMIT_TIME},
        },
    }


def _precommit_comment() -> str:
    report = {
        "schema_version": 1,
        "commit": HEAD,
        "parents": [PARENT],
        "tree": TREE,
        "commands": [{"argv": ["pytest", "-q"], "exit_code": 0}],
        "completed_before_commit": True,
        "hook_bypass": False,
        "completed_utc": "2026-11-05T17:59:00Z",
    }
    return "```json\n" + json.dumps(report) + "\n```"


def _comment(body: str, login: str, comment_id: int) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "author_association": "MEMBER",
        "updated_at": "2026-11-05T18:00:00Z",
    }


def _comments(
    *, head: str = HEAD, author: str = "Codex", reviewer: str = "Claude"
) -> list[dict[str, Any]]:
    return [
        _comment(f"From: {author}\nReviewed-Head: {head}", "author-login", 1),
        _comment(
            (
                f"From: {reviewer}\nReviewed-Head: {head}\n\n"
                "## Independent adversarial review: APPROVE\n"
            ),
            "reviewer-login",
            2,
        ),
        _comment(_precommit_comment(), "reviewer-login", 3),
    ]


def _evaluate(
    *,
    comments: list[dict[str, Any]] | None = None,
    files: list[dict[str, str]] | None = None,
    pull_request: dict[str, Any] | None = None,
    commits: list[dict[str, Any]] | None = None,
    manifest: Any = None,
) -> list[str]:
    return evaluate_pull_request(
        repository={"full_name": "OpenAdaptAI/OpenAdapt", "default_branch": "main"},
        pull_request=pull_request or _pull_request(),
        main_sha=BASE,
        files=files or [{"filename": "packages/flow/openadapt_flow/verify_run.py"}],
        commits=commits or [_commit()],
        comments=_comments() if comments is None else comments,
        manifest=_manifest() if manifest is None else manifest,
    )


def test_manifest_covers_six_required_areas_and_gate_self() -> None:
    validated = validate_manifest(_manifest())
    assert len(validated["areas"]) == 6


def test_missing_manifest_area_fails_closed() -> None:
    manifest = _manifest()
    manifest["areas"].pop()
    with pytest.raises(EvidenceError):
        validate_manifest(manifest)


def test_protected_change_passes_with_exact_head_independent_evidence() -> None:
    assert _evaluate() == []


def test_same_vendor_and_stale_review_fail_closed() -> None:
    same_vendor = _comments(reviewer="Codex")
    assert any(
        "different vendors" in error for error in _evaluate(comments=same_vendor)
    )
    stale = _comments(head=PARENT)
    assert any("exact-head" in error for error in _evaluate(comments=stale))


def test_duplicate_commit_report_fails_closed() -> None:
    comments = _comments()
    comments.append(_comment(_precommit_comment(), "reviewer-login", 4))
    assert any(
        "exactly one current v1" in error for error in _evaluate(comments=comments)
    )


def test_gate_change_requires_exact_head_coordinator_note() -> None:
    files = [{"filename": ".github/agent-safety-manifest.json"}]
    assert any("coordinator approval" in error for error in _evaluate(files=files))
    comments = _comments()
    comments.append(
        _comment(
            (
                f"From: Coordinator\nReviewed-Head: {HEAD}\n"
                "Coordinator-Approval: APPROVE"
            ),
            "coordinator-login",
            4,
        )
    )
    assert _evaluate(files=files, comments=comments) == []


def test_stale_base_fails_closed() -> None:
    pull = _pull_request()
    pull["base"]["sha"] = "e" * 40
    assert any("stale" in error for error in _evaluate(pull_request=pull))


def test_unprotected_path_does_not_require_evidence() -> None:
    assert _evaluate(files=[{"filename": "README.md"}], comments=[]) == []


class FakeApi:
    """Fixed GitHub API responses; this test never calls the network."""

    def __init__(self) -> None:
        self.check_run: dict[str, Any] | None = None
        self.comment_batches = 0

    def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
        if method == "POST" and path == "/check-runs":
            self.check_run = data
            return {"id": 7}
        fixed = {
            "": {"full_name": "OpenAdaptAI/OpenAdapt", "default_branch": "main"},
            "/pulls/42": _pull_request(),
            "/git/ref/heads/main": {"object": {"sha": BASE}},
            "/pulls/42/files?per_page=100&page=1": [
                {"filename": "packages/flow/openadapt_flow/verify_run.py"}
            ],
            "/pulls/42/commits?per_page=100&page=1": [_commit()],
            "/issues/42/comments?per_page=100&page=1": _comments(),
        }
        return fixed[path]

    def pages(self, path: str) -> list[dict[str, Any]]:
        fixed = {
            "/pulls/42/files": [
                {"filename": "packages/flow/openadapt_flow/verify_run.py"}
            ],
            "/pulls/42/commits": [_commit()],
            "/issues/42/comments": _comments(),
        }
        if path == "/issues/42/comments":
            self.comment_batches += 1
            return fixed[path]
        return fixed[path]


def test_orchestrator_uses_fixed_api_responses_and_publishes_exact_head() -> None:
    api = FakeApi()
    event = {"pull_request": {"number": 42}}
    conclusion, errors = run(event, BASE, api)
    assert conclusion == "success", errors
    assert errors == []
    assert api.check_run is not None
    assert api.check_run["name"] == CHECK_NAME
    assert api.check_run["head_sha"] == HEAD
    assert api.check_run["conclusion"] == "success"


def test_rename_from_protected_path_remains_protected() -> None:
    errors = _evaluate(
        files=[
            {
                "filename": "docs/renamed-verifier.md",
                "previous_filename": "packages/flow/openadapt_flow/verifier.py",
            }
        ]
    )
    assert errors == []


def test_manifest_covers_runtime_authority_and_future_approval_bindings() -> None:
    paths = {path for area in _manifest()["areas"] for path in area["paths"]}
    assert "packages/flow/openadapt_flow/verifier.py" in paths
    assert "packages/flow/openadapt_flow/runtime/effects/**" in paths
    assert "packages/flow/openadapt_flow/approval_mode/**" in paths


def test_untrusted_or_non_author_author_declaration_fails_closed() -> None:
    comments = _comments()
    comments[0]["author_association"] = "NONE"
    assert any(
        "exact-head author declaration" in e for e in _evaluate(comments=comments)
    )
    comments = _comments()
    comments[0]["user"]["login"] = "someone-else"
    assert any("pull request author" in e for e in _evaluate(comments=comments))


def test_incomplete_api_counts_fail_closed() -> None:
    pull = _pull_request()
    pull["commits"] = 2
    assert any(
        "commit API results are incomplete" in e for e in _evaluate(pull_request=pull)
    )
    pull = _pull_request()
    pull["changed_files"] = 2
    assert any(
        "changed-file API results are incomplete" in e
        for e in _evaluate(pull_request=pull)
    )


def test_duplicate_commit_ids_and_untrusted_reports_fail_closed() -> None:
    assert any(
        "duplicate commit ids" in e for e in _evaluate(commits=[_commit(), _commit()])
    )
    comments = _comments()
    comments[2]["author_association"] = "NONE"
    assert any(
        "exactly one current v1 precommit report" in e
        for e in _evaluate(comments=comments)
    )


def test_comment_change_during_review_fails_closed() -> None:
    class ChangedCommentsApi(FakeApi):
        def pages(self, path: str) -> list[dict[str, Any]]:
            batch = super().pages(path)
            if path == "/issues/42/comments" and self.comment_batches == 2:
                batch = [dict(item) for item in batch]
                batch[0]["body"] += " edited"
            return batch

    api = ChangedCommentsApi()
    conclusion, errors = run({"pull_request": {"number": 42}}, BASE, api)
    assert conclusion == "failure"
    assert any("comments changed during review" in error for error in errors)


def test_module_entrypoint_missing_environment_exits_without_network() -> None:
    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_EVENT_PATH", None)
    env.pop("TRUSTED_BASE_SHA", None)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci.protected_safety_review"],
        cwd=MANIFEST_PATH.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "missing GitHub event path or token" in result.stderr
