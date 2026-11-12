from __future__ import annotations

import pytest

from scripts.ci.attestation import EvidenceError, parse_review_report

HEAD = "a" * 40
PARENT = "b" * 40


def test_parses_exact_head_approval() -> None:
    body = (
        "From: Claude\n"
        "Reviewed-Head: " + HEAD + "\n\n"
        "## Independent adversarial review: APPROVE\n"
    )
    assert parse_review_report(body, HEAD) == ("anthropic", "APPROVE")


@pytest.mark.parametrize(
    ("vendor", "family"),
    [("Codex", "openai"), ("GPT", "openai"), ("grok", "xai"), ("Gemini", "google")],
)
def test_review_returns_vendor_family(vendor: str, family: str) -> None:
    body = (
        f"From: {vendor} (reviewer)\nReviewed-Head: {HEAD}\n\n"
        "## Independent adversarial review: APPROVE\n"
    )
    assert parse_review_report(body, HEAD) == (family, "APPROVE")


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


@pytest.mark.parametrize("fence", ["```", "~~~~"])
@pytest.mark.parametrize("indent", ["", "   "])
def test_fenced_declarations_are_examples(fence: str, indent: str) -> None:
    declaration = (
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE"
    )
    body = f"Example only.\n{indent}{fence}text\n{declaration}\n{indent}{fence}"
    with pytest.raises(EvidenceError):
        parse_review_report(body, HEAD)


@pytest.mark.parametrize(
    "wrap",
    [
        "<!--\n{}\n-->",
        "<!-- {} -->",
        "<!--\n{}",
    ],
)
def test_html_comment_declarations_are_examples(wrap: str) -> None:
    declaration = (
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE"
    )
    with pytest.raises(EvidenceError):
        parse_review_report(wrap.format(declaration), HEAD)


def test_html_comment_inside_fence_does_not_hide_later_review() -> None:
    body = (
        "```html\n<!--\n```\n"
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: BLOCK"
    )
    with pytest.raises(EvidenceError, match="BLOCK"):
        parse_review_report(body, HEAD)


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
    assert parse_review_report(body, HEAD) == ("anthropic", "APPROVE")


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


def test_bang_closed_html_comment_cannot_hide_block() -> None:
    body = (
        "<!-- x --!>\n"
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: BLOCK"
    )
    with pytest.raises(EvidenceError, match="BLOCK"):
        parse_review_report(body, HEAD)


def test_approval_after_bang_closed_html_comment_does_not_count() -> None:
    body = (
        "<!-- x --!>\n"
        f"From: Claude\nReviewed-Head: {HEAD}\n"
        "## Independent adversarial review: APPROVE"
    )
    with pytest.raises(EvidenceError, match="ambiguous"):
        parse_review_report(body, HEAD)
