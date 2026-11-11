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
    older_main = "8" * 40
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
        workflow_source_sha = BASE
        workflow_source_is_ancestor = True
        move_main_mid_run = False
        main_reads = 0

        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if method == "POST" and path == "/check-runs":
                self.check_run = data
                return {"id": 8}
            if path == "":
                return {"full_name": "OpenAdaptAI/OpenAdapt", "default_branch": "main"}
            if path == "/git/ref/heads/main":
                self.main_reads += 1
                if self.move_main_mid_run and self.main_reads == 2:
                    return {"object": {"sha": "7" * 40}}
                return {"object": {"sha": BASE}}
            if path == f"/compare/{self.workflow_source_sha}...{BASE}":
                return {
                    "status": "ahead"
                    if self.workflow_source_is_ancestor
                    else "diverged",
                    "merge_base_commit": {
                        "sha": self.workflow_source_sha
                        if self.workflow_source_is_ancestor
                        else "6" * 40
                    },
                }
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
                    "head_sha": self.workflow_source_sha,
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

    # A verdict from an older main remains usable when its workflow source is
    # still on main; the current trusted run re-evaluates the PR evidence.
    api.workflow_source_sha = older_main
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="merge_group",
        workflow_sha=group_head,
    )
    assert conclusion == "success", errors

    api.workflow_source_is_ancestor = False
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="merge_group",
        workflow_sha=group_head,
    )
    assert conclusion == "failure"
    assert any("not an ancestor of current main" in error for error in errors)

    api.workflow_source_sha = BASE
    api.workflow_source_is_ancestor = True
    api.move_main_mid_run = True
    api.main_reads = 0
    conclusion, errors = run(
        event,
        BASE,
        api,
        event_name="merge_group",
        workflow_sha=group_head,
    )
    assert conclusion == "failure"
    assert any(
        "trusted workflow source changed before status publication" in error
        for error in errors
    )

    api.move_main_mid_run = False
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
    assert errors == ["safety review could not verify GitHub metadata"]
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


@pytest.mark.parametrize("role", ["author", "reviewer", "coordinator"])
@pytest.mark.parametrize(
    "malformation",
    [
        "duplicate-current-head",
        "current-and-other-head",
        "two-heads-one-header",
        "noncanonical-head-spacing",
        "indented-head",
        "uppercase-head",
        "missing-vendor",
        "duplicate-vendor",
        "invalid-vendor",
    ],
)
@pytest.mark.parametrize("event_name", ["issue_comment", "pull_request_target"])
def test_malformed_current_declaration_cannot_restore_older_approval(
    role: str, malformation: str, event_name: str
) -> None:
    comments = _comments()
    coordinator = (
        f"From: Coordinator\nReviewed-Head: {HEAD}\nCoordinator-Approval: APPROVE"
    )
    comments.append(_comment(coordinator, "coordinator-login", 4))
    slot = {"author": 0, "reviewer": 1, "coordinator": 3}[role]
    malformed = comments[slot]["body"]
    if role == "reviewer":
        malformed = malformed.replace("review: APPROVE", "review: BLOCK")
    head_line = f"Reviewed-Head: {HEAD}"
    if malformation == "duplicate-current-head":
        malformed = malformed.replace(head_line, head_line + "\n" + head_line)
    elif malformation == "current-and-other-head":
        malformed = malformed.replace(
            head_line, head_line + f"\nReviewed-Head: {PARENT}"
        )
    elif malformation == "two-heads-one-header":
        malformed = malformed.replace(head_line, head_line + " " + PARENT)
    elif malformation == "noncanonical-head-spacing":
        malformed = malformed.replace(head_line, f"Reviewed-Head : {HEAD}")
    elif malformation == "indented-head":
        malformed = malformed.replace(head_line, " " + head_line)
    elif malformation == "uppercase-head":
        malformed = malformed.replace(head_line, f"Reviewed-Head: {HEAD.upper()}")
    elif malformation == "missing-vendor":
        malformed = "\n".join(
            line for line in malformed.splitlines() if not line.startswith("From:")
        )
    elif malformation == "duplicate-vendor":
        malformed += "\nFrom: Other"
    else:
        assert malformation == "invalid-vendor"
        malformed = malformed.replace("From: ", "From: invalid/vendor ")
    comments.append(_comment(malformed, comments[slot]["user"]["login"], 5))
    files = [{"filename": ".github/agent-safety-manifest.json"}]
    assert any(
        "invalid " in error for error in _evaluate(comments=comments, files=files)
    )

    class MalformedApi(FakeApi):
        def pages(self, path: str) -> list[dict[str, Any]]:
            if path == "/issues/42/comments":
                return comments
            if path == "/pulls/42/files":
                return files
            return super().pages(path)

    api = MalformedApi()
    event = (
        {"issue": {"number": 42, "pull_request": {}}}
        if event_name == "issue_comment"
        else {"pull_request": {"number": 42, "head": {"sha": HEAD}}}
    )
    conclusion, errors = run(event, BASE, api, event_name=event_name)
    assert conclusion == "failure"
    assert any("invalid " in error for error in errors)
    assert api.check_run is not None
    assert api.check_run["head_sha"] == HEAD
    assert api.check_run["conclusion"] == "failure"


@pytest.mark.parametrize("role", ["author", "reviewer", "coordinator"])
@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_format_examples_cannot_supply_current_declarations(
    role: str, fence: str
) -> None:
    comments = _comments()
    comments.append(
        _comment(
            f"From: Coordinator\nReviewed-Head: {HEAD}\nCoordinator-Approval: APPROVE",
            "coordinator-login",
            4,
        )
    )
    slot = {"author": 0, "reviewer": 1, "coordinator": 3}[role]
    declaration = comments[slot]["body"]
    comments[slot]["body"] = (
        f"Format example, not a declaration.\n{fence}text\n{declaration}\n{fence}\n"
    )
    assert _evaluate(
        comments=comments, files=[{"filename": ".github/agent-safety-manifest.json"}]
    )


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_examples_do_not_change_valid_declaration_roles(fence: str) -> None:
    comments = _comments()
    example = (
        f"\n{fence}text\nFrom: Other\nReviewed-Head: {HEAD}\n"
        "Coordinator-Approval: APPROVE\n"
        f"## Independent adversarial review: BLOCK\n{fence}\n"
    )
    for comment in comments[:2]:
        comment["body"] += example
    assert _evaluate(comments=comments) == []


@pytest.mark.parametrize(
    "reference",
    [
        "canonical",
        "bold",
        "list",
        "lowercase",
        "prefix",
        "unicode-hyphen",
        "unicode-colon",
        "prose",
        "prose-prefix",
        "missing",
        "stale-plus-current-prose",
        "stale-plus-prefix-prose",
        "extra-head-character",
        "backticks",
    ],
)
@pytest.mark.parametrize("verdict", ["APPROVE", "BLOCK"])
@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("event_name", ["issue_comment", "pull_request_target"])
def test_current_or_ambiguous_reviews_never_disappear_before_validation(
    reference: str, verdict: str, edited: bool, event_name: str
) -> None:
    head_lines = {
        "canonical": f"Reviewed-Head: {HEAD}",
        "bold": f"**Reviewed-Head:** {HEAD}",
        "list": f"- Reviewed-Head: {HEAD}",
        "lowercase": f"reviewed-head: {HEAD}",
        "prefix": f"Reviewed-Head: {HEAD[:8]}",
        "unicode-hyphen": f"Reviewed‐Head: {HEAD}",
        "unicode-colon": f"Reviewed-Head： {HEAD}",
        "prose": f"I reviewed the current commit {HEAD}.",
        "prose-prefix": f"I reviewed the current commit {HEAD[:8]}.",
        "missing": "The review has no head binding.",
        "stale-plus-current-prose": f"Reviewed-Head: {PARENT}\nNow reviewing {HEAD}.",
        "stale-plus-prefix-prose": f"Reviewed-Head: {PARENT}\nNow reviewing {HEAD[:8]}.",
        "extra-head-character": f"Reviewed-Head: {HEAD}0",
        "backticks": f"Reviewed-Head: `{HEAD}`",
    }
    comments = _comments()
    review = _comment(
        f"## Independent adversarial review: {verdict}\nFrom: Claude\n{head_lines[reference]}",
        "reviewer-login",
        4,
    )
    if edited:
        review["updated_at"] = "2026-11-05T18:01:00Z"
    comments.append(review)
    errors = _evaluate(comments=comments)
    assert errors, "ambiguous or edited evidence must not restore an older approval"

    class ReviewApi(FakeApi):
        def pages(self, path: str) -> list[dict[str, Any]]:
            if path == "/issues/42/comments":
                return comments
            return super().pages(path)

    api = ReviewApi()
    event = (
        {"issue": {"number": 42, "pull_request": {}}}
        if event_name == "issue_comment"
        else {"pull_request": {"number": 42, "head": {"sha": HEAD}}}
    )
    conclusion, published_errors = run(event, BASE, api, event_name=event_name)
    assert conclusion == "failure"
    assert published_errors
    assert api.check_run is not None
    assert api.check_run["head_sha"] == HEAD
    assert api.check_run["conclusion"] == "failure"


@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("verdict", ["APPROVE", "BLOCK"])
def test_proven_stale_reviews_do_not_veto_current_evidence(
    edited: bool, verdict: str
) -> None:
    comments = _comments()
    stale = _comment(
        f"## Independent adversarial review: {verdict}\nFrom: Claude\nReviewed-Head: {PARENT}",
        "reviewer-login",
        4,
    )
    if edited:
        stale["updated_at"] = "2026-11-05T18:01:00Z"
    comments.append(stale)
    assert _evaluate(comments=comments) == []


@pytest.mark.parametrize("fence", ["```", "~~~~"])
@pytest.mark.parametrize("edited", [False, True])
def test_fenced_later_review_example_is_neither_approval_nor_veto(
    fence: str, edited: bool
) -> None:
    comments = _comments()
    example = _comment(
        f"{fence}text\n## Independent adversarial review: BLOCK\n"
        f"From: Claude\nReviewed-Head: {HEAD}\n{fence}",
        "reviewer-login",
        4,
    )
    if edited:
        example["updated_at"] = "2026-11-05T18:01:00Z"
    comments.append(example)
    assert _evaluate(comments=comments) == []


@pytest.mark.parametrize(
    "heading",
    [
        "## INDEPENDENT ADVERSARIAL REVIEW: BLOCK",
        "＃＃ Independent adversarial review： BLOCK",
        "## Independent adversarial review: APPROVE\n## INDEPENDENT ADVERSARIAL REVIEW: BLOCK",
    ],
)
@pytest.mark.parametrize("edited", [False, True])
def test_normalized_heading_discovery_cannot_normalize_approval(
    heading: str, edited: bool
) -> None:
    comments = _comments()
    review = _comment(
        f"{heading}\nFrom: Claude\nReviewed-Head: {HEAD}",
        "reviewer-login",
        4,
    )
    if edited:
        review["updated_at"] = "2026-11-05T18:01:00Z"
    comments.append(review)
    assert any(
        "invalid independent review" in error for error in _evaluate(comments=comments)
    )


@pytest.mark.parametrize("edited", [False, True])
def test_untrusted_review_does_not_grant_or_veto(edited: bool) -> None:
    comments = _comments()
    review = _comment(
        f"## Independent adversarial review: BLOCK\nFrom: Claude\nReviewed-Head: {HEAD}",
        "untrusted-login",
        4,
    )
    review["author_association"] = "NONE"
    if edited:
        review["updated_at"] = "2026-11-05T18:01:00Z"
    comments.append(review)
    assert _evaluate(comments=comments) == []


@pytest.mark.parametrize("role", ["author", "reviewer", "coordinator"])
@pytest.mark.parametrize(
    "alias",
    [
        "bold",
        "italic",
        "link",
        "list",
        "lowercase",
        "unicode-hyphen",
        "unicode-colon",
        "prefix",
    ],
)
def test_noncanonical_extra_head_binding_is_rejected_for_every_declaration(
    role: str, alias: str
) -> None:
    comments = _comments()
    comments.append(
        _comment(
            f"From: Coordinator\nReviewed-Head: {HEAD}\nCoordinator-Approval: APPROVE",
            "coordinator-login",
            4,
        )
    )
    slot = {"author": 0, "reviewer": 1, "coordinator": 3}[role]
    aliases = {
        "bold": f"**Reviewed-Head:** {PARENT}",
        "italic": f"*Reviewed-Head:* {PARENT}",
        "link": f"[Reviewed-Head](#example): {PARENT}",
        "list": f"- Reviewed-Head: {PARENT}",
        "lowercase": f"reviewed-head: {PARENT}",
        "unicode-hyphen": f"Reviewed‐Head: {PARENT}",
        "unicode-colon": f"Reviewed-Head： {PARENT}",
        "prefix": f"Reviewed-Head: {PARENT[:8]}",
    }
    comments[slot]["body"] += "\n" + aliases[alias]
    assert any(
        "invalid " in error
        for error in _evaluate(
            comments=comments,
            files=[{"filename": ".github/agent-safety-manifest.json"}],
        )
    )


@pytest.mark.parametrize(
    "explanation",
    [
        "The Reviewed-Head field matches the trusted metadata.",
        "> Reviewed-Head: example\n> ## Independent adversarial review: BLOCK",
    ],
)
def test_ordinary_prose_and_quoted_example_do_not_create_extra_bindings(
    explanation: str,
) -> None:
    comments = _comments()
    comments[1]["body"] += "\n" + explanation
    assert _evaluate(comments=comments) == []


@pytest.mark.parametrize("method", ["request", "graphql"])
def test_api_sanitizes_genuine_malformed_authorization_before_connection(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.client

    from scripts.ci import protected_safety_review as safety

    canary = "synthetic-header-canary-not-a-credential"
    connected = []
    rejected = []

    def connect_forbidden(_connection):
        connected.append(True)
        raise AssertionError("this control must not connect")

    def validate_header(request, **_kwargs):
        connection = http.client.HTTPConnection("127.0.0.1", 1)
        connection.putrequest("GET", "/")
        try:
            connection.putheader("Authorization", request.get_header("Authorization"))
        except ValueError as error:
            # The genuine stdlib header validator includes the rejected value.
            rejected.append(canary in str(error))
            raise
        raise AssertionError("the malformed header was not rejected")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect_forbidden)
    monkeypatch.setattr(safety.urllib.request, "urlopen", validate_header)
    api = safety.GitHubApi(canary + "\ninvalid-header-continuation")
    with pytest.raises(RuntimeError) as failure:
        if method == "request":
            api.request("/fixed")
        else:
            api.graphql("query { viewer { login } }", {})
    assert rejected == [True]
    assert connected == []
    assert str(failure.value) == (
        "GitHub API request failed"
        if method == "request"
        else "GitHub GraphQL request failed"
    )
    assert canary not in str(failure.value)
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize("method", ["request", "graphql"])
@pytest.mark.parametrize("kind", ["url", "timeout", "decode", "other"])
def test_api_transport_exception_values_never_become_diagnostics(
    method: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import urllib.error

    from scripts.ci import protected_safety_review as safety

    canary = "synthetic-response-canary-not-a-credential"
    errors = {
        "url": urllib.error.URLError(canary),
        "timeout": TimeoutError(canary),
        "decode": UnicodeDecodeError("utf-8", b"\xff", 0, 1, canary),
        "other": ValueError(canary),
    }
    observed = []

    def fail_transport(_request, **_kwargs):
        observed.append(kind)
        raise errors[kind]

    monkeypatch.setattr(safety.urllib.request, "urlopen", fail_transport)
    api = safety.GitHubApi("synthetic-not-a-credential")
    with pytest.raises(RuntimeError) as failure:
        if method == "request":
            api.request("/" + canary)
        else:
            api.graphql("query { viewer { login } }", {})
    assert observed == [kind]
    assert str(failure.value) == (
        "GitHub API request failed"
        if method == "request"
        else "GitHub GraphQL request failed"
    )
    assert canary not in str(failure.value)
    assert failure.value.__suppress_context__ is True


def test_metadata_exception_cannot_enter_errors_or_published_check() -> None:
    canary = "synthetic-metadata-canary-not-a-credential"

    class FailedMetadataApi(FakeApi):
        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if method == "GET":
                raise ValueError(canary)
            return super().request(path, method=method, data=data)

    api = FailedMetadataApi()
    event = {"pull_request": {"number": 42, "head": {"sha": HEAD}}}
    conclusion, errors = run(event, BASE, api, event_name="pull_request_target")
    assert conclusion == "failure"
    assert errors == ["safety review could not verify GitHub metadata"]
    assert api.check_run is not None
    assert api.check_run["conclusion"] == "failure"
    assert api.check_run["head_sha"] == HEAD
    assert canary not in json.dumps(api.check_run)
    assert canary not in json.dumps(errors)


@pytest.mark.parametrize("stage", ["metadata", "publication", "event"])
def test_main_never_prints_exception_values(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.ci import protected_safety_review as safety

    canary = "synthetic-main-canary-not-a-credential"
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps({"pull_request": {"number": 42, "head": {"sha": HEAD}}}),
        encoding="utf-8",
    )

    class FailedApi(FakeApi):
        def request(self, path: str, *, method: str = "GET", data: Any = None) -> Any:
            if (stage == "metadata" and method == "GET") or (
                stage == "publication" and method == "POST"
            ):
                raise ValueError(canary)
            return super().request(path, method=method, data=data)

    api = FailedApi()
    monkeypatch.setattr(safety, "GitHubApi", lambda _token: api)
    for name, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_TOKEN": "synthetic-not-a-credential",
        "TRUSTED_BASE_SHA": BASE,
        "GITHUB_EVENT_NAME": "pull_request_target",
        "GITHUB_RUN_ID": "77",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": BASE,
    }.items():
        monkeypatch.setenv(name, value)
    if stage == "event":
        original = Path.read_text

        def fail_event(path, *args, **kwargs):
            if path == event_file:
                raise ValueError(canary)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_event)
    status = safety.main()
    output = capsys.readouterr()
    assert canary not in output.out
    assert canary not in output.err
    if stage == "metadata":
        assert status == 1
        assert api.check_run is not None
        assert api.check_run["head_sha"] == HEAD
        assert api.check_run["conclusion"] == "failure"
        assert canary not in json.dumps(api.check_run)
        assert output.out == (
            "protected-safety-review: failure\n"
            "- safety review could not verify GitHub metadata\n"
        )
        assert output.err == ""
    else:
        assert status == 2
        assert api.check_run is None
        assert output.out == ""
        assert output.err == "unable to publish protected safety review\n"
