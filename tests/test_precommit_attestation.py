from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.ci.attestation import (
    EvidenceError,
    parse_author_declaration,
    parse_precommit_report,
    parse_review_report,
    validate_independent_review,
)

HEAD = "a" * 40
PARENT = "b" * 40
TREE = "c" * 40
COMMIT_TIME = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)


def _precommit_body(**updates: object) -> str:
    report: dict[str, object] = {
        "schema_version": 1,
        "commit": HEAD,
        "parents": [PARENT],
        "tree": TREE,
        "commands": [{"argv": ["uv", "run", "pytest", "-q"], "exit_code": 0}],
        "completed_before_commit": True,
    }
    report.update(updates)
    return f"```json\n{json.dumps(report)}\n```"


def test_parses_exact_head_approval() -> None:
    body = (
        "From: Claude\n"
        "Reviewed-Head: " + HEAD + "\n\n"
        "## Independent adversarial review: APPROVE\n"
    )
    assert parse_review_report(body, HEAD) == ("Claude", "APPROVE")


def test_requires_separate_author_and_independent_reviewer_vendors() -> None:
    author = f"From: Codex\nReviewed-Head: {HEAD}\n"
    reviewer = (
        f"From: Claude\nReviewed-Head: {HEAD}\n\n"
        "## Independent adversarial review: APPROVE\n"
    )
    assert parse_author_declaration(author, HEAD) == "Codex"
    assert validate_independent_review(author, reviewer, HEAD) == (
        "Claude",
        "APPROVE",
    )
    same_vendor = reviewer.replace("From: Claude", "From: Codex")
    with pytest.raises(EvidenceError):
        validate_independent_review(author, same_vendor, HEAD)


@pytest.mark.parametrize(
    "body",
    [
        f"From: Claude\nReviewed-Head: {PARENT}\n\n## Independent adversarial review: APPROVE\n",
        f"From: Claude\nReviewed-Head: {HEAD}\n\n## Independent adversarial review: BLOCK\n",
        f"From: Claude\nReviewed-Head: {HEAD}\n\n## Independent adversarial review: APPROVE\n## Independent adversarial review: APPROVE\n",
        f"From: Claude\nFrom: Grok\nReviewed-Head: {HEAD}\n\n## Independent adversarial review: APPROVE\n",
        f"From: Claude\nReviewed-Head: {HEAD}\n\n## Independent adversarial review: APPROVE\n## Independent adversarial review: BLOCK\n",
    ],
)
def test_rejects_stale_ambiguous_or_blocked_review(body: str) -> None:
    with pytest.raises(EvidenceError):
        parse_review_report(body, HEAD)


def test_validates_all_precommit_bindings() -> None:
    report = parse_precommit_report(
        _precommit_body(completed_utc="2026-11-05T17:59:00Z"),
        expected_commit=HEAD,
        expected_parents=[PARENT],
        expected_tree=TREE,
        commit_time=COMMIT_TIME,
    )
    assert report["commit"] == HEAD


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": 2},
        {"commit": PARENT},
        {"parents": []},
        {"tree": PARENT},
        {"commands": []},
        {"commands": [{"argv": ["pytest"], "exit_code": 1}]},
        {"commands": [{"argv": ["pytest"], "exit_code": True}]},
        {"completed_before_commit": False},
        {"hook_bypass": False},
        {"unrecognized": "field"},
        {"completed_utc": "2026-11-05T18:01:00Z"},
    ],
)
def test_rejects_missing_invalid_or_nonmatching_evidence(
    updates: dict[str, object],
) -> None:
    with pytest.raises(EvidenceError):
        parse_precommit_report(
            _precommit_body(**updates),
            expected_commit=HEAD,
            expected_parents=[PARENT],
            expected_tree=TREE,
            commit_time=COMMIT_TIME,
        )


def test_rejects_malformed_report() -> None:
    with pytest.raises(EvidenceError):
        parse_precommit_report(
            "no report",
            expected_commit=HEAD,
            expected_parents=[PARENT],
            expected_tree=TREE,
            commit_time=COMMIT_TIME,
        )


@pytest.mark.parametrize("fence", ["```", "~~~~"])
@pytest.mark.parametrize("indent", ["", "   "])
def test_fenced_declarations_are_examples_for_both_parsers(
    fence: str, indent: str
) -> None:
    declaration = (
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE"
    )
    body = f"Example only.\n{indent}{fence}text\n{declaration}\n{indent}{fence}"
    with pytest.raises(EvidenceError):
        parse_review_report(body, HEAD)
    with pytest.raises(EvidenceError):
        parse_author_declaration(body, HEAD)


@pytest.mark.parametrize("fence", ["````", "~~~~"])
@pytest.mark.parametrize("false_close", ["```", "~~~", "````text", "~~~~text"])
def test_incomplete_or_mismatched_fence_cannot_expose_example(
    fence: str, false_close: str
) -> None:
    body = (
        f"{fence}text\n{false_close}\nFrom: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE"
    )
    with pytest.raises(EvidenceError):
        parse_review_report(body, HEAD)


def test_visible_review_is_independent_of_fenced_example() -> None:
    body = (
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE\n"
        "```text\nFrom: Codex\nReviewed-Head: invalid\n"
        "## Independent adversarial review: BLOCK\n```"
    )
    assert parse_review_report(body, HEAD) == ("Claude", "APPROVE")


@pytest.mark.parametrize(
    "heading",
    [
        "## INDEPENDENT ADVERSARIAL REVIEW: APPROVE",
        "＃＃ Independent adversarial review： APPROVE",
        "## Independent adversarial review: APPROVE\n## INDEPENDENT ADVERSARIAL REVIEW: BLOCK",
    ],
)
def test_heading_discovery_does_not_make_noncanonical_or_extra_heading_valid(
    heading: str,
) -> None:
    body = f"From: Claude\nReviewed-Head: {HEAD}\n{heading}"
    with pytest.raises(EvidenceError):
        parse_review_report(body, HEAD)
    with pytest.raises(EvidenceError):
        parse_author_declaration(body, HEAD)
