from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.attestation import EvidenceError
from scripts.ci.protected_safety_review import (
    CHECK_NAME,
    MANIFEST_PATH,
    MERGE_QUEUE_QUERY,
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
        "number": 42,
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
        "completed_utc": "2026-11-05T17:59:00Z",
    }
    return "```json\n" + json.dumps(report) + "\n```"


def _comment(body: str, login: str, comment_id: int) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login},
        "author_association": "MEMBER",
        "created_at": "2026-11-05T18:00:00Z",
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


def test_pr_does_not_need_rebase_to_current_main_tip() -> None:
    pull = _pull_request()
    pull["base"]["sha"] = "e" * 40
    assert _evaluate(pull_request=pull) == []


def test_unprotected_path_does_not_require_evidence() -> None:
    assert _evaluate(files=[{"filename": "README.md"}], comments=[]) == []


class FakeApi:
    """Fixed GitHub API responses; this test never calls the network."""

    def __init__(self, *, queue_entries: list[dict[str, Any]] | None = None) -> None:
        self.check_run: dict[str, Any] | None = None
        self.comment_batches = 0
        self.queue_entries = queue_entries or []

    def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
        if method == "POST" and path == "/check-runs":
            self.check_run = data
            return {"id": 7}
        fixed = {
            "": {"full_name": "OpenAdaptAI/OpenAdapt", "default_branch": "main"},
            "/actions/runs/77": {
                "path": ".github/workflows/protected-safety-review.yml",
                "event": "issue_comment",
                "head_sha": BASE,
                "repository": {"full_name": "OpenAdaptAI/OpenAdapt"},
                "html_url": "https://github.com/OpenAdaptAI/OpenAdapt/actions/runs/77",
            },
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

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert query == MERGE_QUEUE_QUERY
        assert variables == {
            "owner": "OpenAdaptAI",
            "name": "OpenAdapt",
            "branch": "main",
        }
        return {
            "repository": {
                "mergeQueue": {
                    "configuration": {"mergeMethod": "MERGE"},
                    "entries": {
                        "totalCount": len(self.queue_entries),
                        "pageInfo": {"hasNextPage": False},
                        "nodes": self.queue_entries,
                    },
                }
            }
        }


def test_orchestrator_uses_fixed_api_responses_and_publishes_exact_head() -> None:
    api = FakeApi()
    event = {"issue": {"number": 42, "pull_request": {}}}
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="issue_comment",
        workflow_run_id="77",
        workflow_sha=BASE,
    )
    assert conclusion == "success", errors
    assert errors == []
    assert api.check_run is not None
    assert api.check_run["name"] == CHECK_NAME
    assert api.check_run["head_sha"] == HEAD
    assert api.check_run["conclusion"] == "success"
    assert api.check_run["external_id"] == "77"
    assert api.check_run["details_url"].endswith("/77")


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


def test_edited_review_comment_is_not_current_evidence() -> None:
    comments = _comments()
    comments[1]["updated_at"] = "2026-11-05T18:01:00Z"
    assert any(
        "exact-head independent review" in e for e in _evaluate(comments=comments)
    )


def test_coordinator_header_must_be_a_whole_line() -> None:
    comments = _comments()
    comments.append(
        _comment(
            f"From: Coordinator\nReviewed-Head: {HEAD}\n\nQuoted: Coordinator-Approval: APPROVE",
            "coordinator-login",
            4,
        )
    )
    errors = _evaluate(
        files=[{"filename": ".github/agent-safety-manifest.json"}],
        comments=comments,
    )
    assert any("coordinator approval" in error for error in errors)
    assert not any("invalid coordinator note" in error for error in errors)


def _queue_entry(
    *, position: int, base: str, head: str, number: int, pr_head: str
) -> dict[str, Any]:
    return {
        "position": position,
        "state": "AWAITING_CHECKS",
        "baseCommit": {"oid": base},
        "headCommit": {"oid": head},
        "pullRequest": {
            "number": number,
            "state": "OPEN",
            "baseRefName": "main",
            "headRefOid": pr_head,
        },
    }


def test_merge_group_maps_multi_pr_chain_and_publishes_group_sha() -> None:
    group_head = "f" * 40
    first_head = "d" * 40
    prior_group_base = "9" * 40
    queue = [
        _queue_entry(
            position=3,
            base="8" * 40,
            head="7" * 40,
            number=40,
            pr_head="6" * 40,
        ),
        _queue_entry(
            position=1,
            base=prior_group_base,
            head=first_head,
            number=41,
            pr_head="4" * 40,
        ),
        _queue_entry(
            position=2, base=first_head, head=group_head, number=42, pr_head=HEAD
        ),
    ]
    queue[0]["state"] = "LOCKED"

    class GroupApi(FakeApi):
        missing_head_verdict = False

        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if method == "POST" and path == "/check-runs":
                self.check_run = data
                return {"id": 8}
            if path == "":
                return {"full_name": "OpenAdaptAI/OpenAdapt", "default_branch": "main"}
            if path == "/git/ref/heads/main":
                return {"object": {"sha": BASE}}
            if path.startswith("/commits/") and "/check-runs?" in path:
                if self.missing_head_verdict:
                    return {"total_count": 0, "check_runs": []}
                pr_head = path.split("/", 3)[2]
                run_id = "81" if pr_head == "4" * 40 else "82"
                return {
                    "total_count": 1,
                    "check_runs": [
                        {
                            "name": CHECK_NAME,
                            "head_sha": pr_head,
                            "status": "completed",
                            "conclusion": "success",
                            "started_at": "2026-11-05T18:00:00Z",
                            "app": {"slug": "github-actions"},
                            "external_id": f"{run_id}:1",
                            "details_url": f"https://github.com/OpenAdaptAI/OpenAdapt/actions/runs/{run_id}",
                        }
                    ],
                }
            if path in {"/actions/runs/81", "/actions/runs/82"}:
                run_id = path.rsplit("/", 1)[1]
                return {
                    "path": ".github/workflows/protected-safety-review.yml",
                    "event": "issue_comment",
                    "head_sha": BASE,
                    "run_attempt": 1,
                    "repository": {"full_name": "OpenAdaptAI/OpenAdapt"},
                    "html_url": f"https://github.com/OpenAdaptAI/OpenAdapt/actions/runs/{run_id}",
                }
            if path in {"/pulls/41", "/pulls/42"}:
                number = int(path.rsplit("/", 1)[1])
                pr = _pull_request()
                pr["number"] = number
                pr["user"] = {"login": f"author-{number}"}
                pr["head"]["sha"] = "4" * 40 if number == 41 else HEAD
                pr["changed_files"] = 1
                pr["commits"] = 1
                return pr
            raise AssertionError(path)

        def pages(self, path: str) -> list[dict[str, Any]]:
            if path.endswith("/files"):
                return [{"filename": "README.md"}]
            if path.endswith("/commits"):
                return [_commit()]
            if path.endswith("/comments"):
                return []
            raise AssertionError(path)

    api = GroupApi(queue_entries=queue)
    event = {
        "repository": {"full_name": "OpenAdaptAI/OpenAdapt"},
        "merge_group": {
            "base_ref": "refs/heads/main",
            "base_sha": prior_group_base,
            "head_sha": group_head,
        },
    }
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="merge_group",
        workflow_sha=group_head,
    )
    assert conclusion == "success", errors
    assert api.check_run is not None
    assert api.check_run["head_sha"] == group_head

    api.missing_head_verdict = True
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="merge_group",
        workflow_sha=group_head,
    )
    assert conclusion == "failure"
    assert any("no completed protected-safety-review head verdict" in e for e in errors)
    assert api.check_run["head_sha"] == group_head


def test_merge_group_missing_or_ambiguous_mapping_fails_closed() -> None:
    group_head = "f" * 40
    api = FakeApi(queue_entries=[])
    event = {
        "merge_group": {
            "base_ref": "main",
            "base_sha": BASE,
            "head_sha": group_head,
        }
    }
    conclusion, errors = run(event, BASE, api, event_name="merge_group")
    assert conclusion == "failure"
    assert any("queue-entry mapping" in error for error in errors)
    assert api.check_run is not None
    assert api.check_run["head_sha"] == group_head


def test_merge_group_rejects_rewrite_queue_methods() -> None:
    group_head = "f" * 40
    api = FakeApi()
    api.graphql = lambda query, variables: {
        "repository": {
            "mergeQueue": {
                "configuration": {"mergeMethod": "SQUASH"},
                "entries": {
                    "totalCount": 1,
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        _queue_entry(
                            position=1,
                            base=BASE,
                            head=group_head,
                            number=42,
                            pr_head=HEAD,
                        )
                    ],
                },
            }
        }
    }
    event = {
        "merge_group": {
            "base_ref": "main",
            "base_sha": BASE,
            "head_sha": group_head,
        }
    }
    conclusion, errors = run(event, BASE, api, event_name="merge_group")
    assert conclusion == "failure"
    assert any("only for MERGE queues" in error for error in errors)
    assert api.check_run is not None
    assert api.check_run["head_sha"] == group_head


def test_workflow_uses_trusted_main_for_merge_group_not_group_content() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/protected-safety-review.yml"
    )
    text = workflow.read_text(encoding="utf-8")
    assert "merge_group:" in text
    assert "github.event_name == 'merge_group'" in text
    assert "ref: ${{ github.event.pull_request.base.sha || 'refs/heads/main' }}" in text
    assert 'run: echo "sha=$(git rev-parse HEAD)"' in text
    assert "TRUSTED_BASE_SHA: ${{ steps.trusted_base.outputs.sha }}" in text
    assert "github.event.merge_group.base_sha" not in text
    assert "ref: ${{ github.event.merge_group.head_sha }}" not in text


def test_first_api_failure_publishes_failure_for_known_pr_head() -> None:
    class FailedFirstRequestApi(FakeApi):
        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if path == "/actions/runs/77":
                raise RuntimeError("fixed synthetic API failure")
            return super().request(path, method=method, data=data)

    api = FailedFirstRequestApi()
    event = {"pull_request": {"number": 42, "head": {"sha": HEAD}}}
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="pull_request_target",
        workflow_run_id="77",
        workflow_sha=BASE,
    )
    assert conclusion == "failure"
    assert any("fixed synthetic API failure" in error for error in errors)
    assert api.check_run is not None
    assert api.check_run["head_sha"] == HEAD


def test_pr_head_change_during_review_publishes_failure_for_original_head() -> None:
    class HeadChangesApi(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.pull_calls = 0

        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if path == "/pulls/42":
                self.pull_calls += 1
                pull = _pull_request()
                if self.pull_calls > 1:
                    pull["head"]["sha"] = "e" * 40
                return pull
            return super().request(path, method=method, data=data)

    api = HeadChangesApi()
    event = {"issue": {"number": 42, "pull_request": {}}}
    conclusion, errors = run(event, BASE, api, event_name="issue_comment")
    assert conclusion == "failure"
    assert any("head changed during review" in error for error in errors)
    assert api.check_run is not None
    assert api.check_run["head_sha"] == HEAD


def test_untrusted_workflow_provenance_cannot_publish_success() -> None:
    class WrongWorkflowApi(FakeApi):
        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            value = super().request(path, method=method, data=data)
            if path == "/actions/runs/77":
                value["path"] = ".github/workflows/other.yml"
            return value

    api = WrongWorkflowApi()
    event = {"issue": {"number": 42, "pull_request": {}}}
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="issue_comment",
        workflow_run_id="77",
        workflow_sha=BASE,
    )
    assert conclusion == "failure"
    assert any("trusted safety workflow" in error for error in errors)


def test_module_entrypoint_missing_environment_exits_without_network() -> None:
    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_EVENT_PATH", None)
    env.pop("TRUSTED_BASE_SHA", None)
    env.pop("GITHUB_EVENT_NAME", None)
    env.pop("GITHUB_RUN_ID", None)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci.protected_safety_review"],
        cwd=MANIFEST_PATH.parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert (
        "missing GitHub event path, token, event name, run id/attempt, or SHA"
        in result.stderr
    )
