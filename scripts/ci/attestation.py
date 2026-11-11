"""Parse v1 human-reported safety evidence without executing PR code.

These records guard against accidental omissions in a shared-account workflow.
They are not signatures and do not prove that a command ran or that a hook was
not bypassed. Callers must supply commit metadata obtained from trusted Git
plumbing and compare it with the report.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")
_VENDOR = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]{0,39}$")
_REVIEW_HEADING = "## Independent adversarial review: APPROVE"
_BLOCK_HEADING = "## Independent adversarial review: BLOCK"


class EvidenceError(ValueError):
    """Raised when evidence is missing, ambiguous, stale, or malformed."""


def declaration_text(body: str) -> str:
    """Exclude Markdown fenced examples from declaration syntax.

    Backtick and tilde fences may be indented by up to three spaces. A fence
    closes only with the same character and at least its opening length.
    """
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in body.splitlines():
        fence = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", line)
        if fence_character is not None:
            if (
                fence is not None
                and fence[1][0] == fence_character
                and len(fence[1]) >= fence_length
                and not fence[2].strip()
            ):
                fence_character = None
            continue
        if fence is not None and (fence[1][0] != "`" or "`" not in fence[2]):
            fence_character, fence_length = fence[1][0], len(fence[1])
            continue
        lines.append(line)
    return "\n".join(lines)


def review_headings(body: str) -> list[str]:
    """Find review declarations for validation, never normalize approval."""
    return [
        line
        for line in declaration_text(body).splitlines()
        if re.match(
            r"^ {0,3}##[ \t]+independent[ \t]+adversarial[ \t]+review[ \t]*:",
            unicodedata.normalize("NFKC", line).casefold(),
        )
    ]


def head_reference_lines(body: str) -> list[str]:
    """Retain head-like bindings for rejection, including decorated aliases."""
    return [
        line
        for line in declaration_text(body).splitlines()
        if re.match(
            r"^(?! {4}) {0,3}(?:\d+[.)][ \t]+)?[^a-z0-9>\s]*[ \t]*"
            r"reviewed[^a-z0-9]*head(?![a-z0-9])",
            unicodedata.normalize("NFKC", line).casefold(),
        )
    ]


def _one_header(body: str, name: str) -> str:
    values = re.findall(rf"(?m)^{re.escape(name)}: (.*)$", declaration_text(body))
    if name == "Reviewed-Head" and len(head_reference_lines(body)) != len(values):
        raise EvidenceError(
            "head-like bindings must use the exact Reviewed-Head header"
        )
    if len(values) != 1 or not values[0].strip():
        raise EvidenceError(f"expected exactly one nonempty {name} header")
    return values[0].strip()


def parse_review_report(body: str, expected_head: str) -> tuple[str, str]:
    """Return (vendor, verdict) for an exact-head review comment."""
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    vendor = _one_header(body, "From")
    if not _VENDOR.fullmatch(vendor):
        raise EvidenceError("invalid From vendor declaration")
    reviewed_head = _one_header(body, "Reviewed-Head")
    if not _SHA.fullmatch(reviewed_head) or reviewed_head != expected_head:
        raise EvidenceError("review declaration is malformed or stale")
    lines = declaration_text(body).splitlines()
    approve_count = lines.count(_REVIEW_HEADING)
    block_count = lines.count(_BLOCK_HEADING)
    if len(review_headings(body)) != 1 or approve_count + block_count != 1:
        raise EvidenceError(
            "expected exactly one existing review heading: APPROVE or BLOCK"
        )
    if block_count:
        raise EvidenceError("independent reviewer returned BLOCK")
    return vendor, "APPROVE"


def parse_author_declaration(body: str, expected_head: str) -> str:
    """Return the declared author vendor for a separate exact-head comment."""
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    vendor = _one_header(body, "From")
    if not _VENDOR.fullmatch(vendor):
        raise EvidenceError("invalid From vendor declaration")
    declared_head = _one_header(body, "Reviewed-Head")
    if not _SHA.fullmatch(declared_head) or declared_head != expected_head:
        raise EvidenceError("author declaration is malformed or stale")
    if review_headings(body):
        raise EvidenceError(
            "author declaration must be separate from the review comment"
        )
    return vendor


def validate_independent_review(
    author_body: str, reviewer_body: str, expected_head: str
) -> tuple[str, str]:
    """Require distinct author and reviewer vendors on the exact current head."""
    author = parse_author_declaration(author_body, expected_head)
    reviewer, verdict = parse_review_report(reviewer_body, expected_head)
    if author.casefold() == reviewer.casefold():
        raise EvidenceError("author and reviewer must declare different vendors")
    return reviewer, verdict


def _json_object(body: str) -> dict[str, Any]:
    matches = re.findall(r"(?ms)^```json\s*\n(.*?)\n```\s*$", body.strip())
    if len(matches) != 1:
        raise EvidenceError("expected exactly one fenced JSON v1 report")
    try:
        value = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise EvidenceError("report contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError("report JSON must be an object")
    return value


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_precommit_report(
    body: str,
    *,
    expected_commit: str,
    expected_parents: list[str],
    expected_tree: str,
    commit_time: datetime,
) -> dict[str, Any]:
    """Validate one v1 report against exact metadata from trusted Git plumbing.

    The returned value is parsed evidence only; it is not proof that reported
    commands ran before the commit or that hooks were used.
    """
    for name, sha in [
        ("commit", expected_commit),
        ("tree", expected_tree),
        *[("parent", p) for p in expected_parents],
    ]:
        if not _SHA.fullmatch(sha):
            raise EvidenceError(f"expected {name} must be a full lowercase commit SHA")
    if commit_time.tzinfo is None:
        raise EvidenceError("commit_time must include a timezone")
    report = _json_object(body)
    required = {
        "schema_version",
        "commit",
        "parents",
        "tree",
        "commands",
        "completed_before_commit",
    }
    optional = {"completed_utc"}
    if set(report) - (required | optional) or required - set(report):
        raise EvidenceError("report fields do not match the documented v1 schema")
    if report["schema_version"] != 1:
        raise EvidenceError("unsupported precommit evidence schema version")
    if (
        report["commit"] != expected_commit
        or report["parents"] != expected_parents
        or report["tree"] != expected_tree
    ):
        raise EvidenceError("report does not match the exact commit, parents, and tree")
    commands = report["commands"]
    if not isinstance(commands, list) or not commands:
        raise EvidenceError("commands must be a nonempty array")
    for command in commands:
        if not isinstance(command, dict) or set(command) != {"argv", "exit_code"}:
            raise EvidenceError("each command must contain only argv and exit_code")
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) and arg for arg in argv)
        ):
            raise EvidenceError(
                "command argv must be a nonempty array of nonempty strings"
            )
        if type(command["exit_code"]) is not int or command["exit_code"] != 0:
            raise EvidenceError("every reported command must have exit_code 0")
    if report["completed_before_commit"] is not True:
        raise EvidenceError("report must state completed_before_commit=true")
    if "completed_utc" in report:
        completed = _parse_time(report["completed_utc"], "completed_utc")
        if completed > commit_time.astimezone(timezone.utc):
            raise EvidenceError(
                "reported completion time is later than the commit time"
            )
    return report
