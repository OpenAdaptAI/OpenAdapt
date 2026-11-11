"""Parse independent safety review comments without executing PR code.

A review comment guards against accidental omissions in a shared-account
workflow. It is not a signature, and its vendor line is self-reported. An
author declaration names the author vendor for a shared or legacy branch. It
has no review heading and is never counted as a review.
"""

from __future__ import annotations

import re
import unicodedata

_SHA = re.compile(r"^[0-9a-f]{40}$")
# A From line names the vendor first, optionally followed by free text such as
# "Claude (independent reviewer)". Aliases map to one vendor family.
_VENDOR = re.compile(r"^([A-Za-z]+)(?:[ \t]+\S.{0,79})?$")
VENDOR_FAMILIES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "codex": "openai",
    "gpt": "openai",
    "openai": "openai",
    "grok": "xai",
    "xai": "xai",
    "gemini": "google",
    "google": "google",
    "human": "human",
}
_REVIEW_HEADING = "## Independent adversarial review: APPROVE"
_BLOCK_HEADING = "## Independent adversarial review: BLOCK"


class EvidenceError(ValueError):
    """Raised when evidence is missing, ambiguous, stale, or malformed."""


def declaration_text(body: str) -> str:
    """Exclude Markdown fenced examples and HTML comments from declarations.

    Backtick and tilde fences may be indented by up to three spaces. A fence
    closes only with the same character and at least its opening length.
    Outside fences, ``<!-- ... -->`` is removed; an unclosed HTML comment hides
    the rest of the body.
    """
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    in_html_comment = False
    for line in body.splitlines():
        if in_html_comment:
            end = line.find("-->")
            if end < 0:
                continue
            line = line[end + 3 :]
            in_html_comment = False
        if fence_character is None:
            line = re.sub(r"<!--.*?-->", "", line)
            start = line.find("<!--")
            if start >= 0:
                line = line[:start]
                in_html_comment = True
                if not line.strip():
                    continue
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


def vendor_family(name: str) -> str | None:
    """Return the vendor family for a known vendor or alias, else None."""
    return VENDOR_FAMILIES.get(name.casefold())


def parse_review_report(body: str, expected_head: str) -> tuple[str, str]:
    """Return (vendor family, verdict) for an exact-head review comment."""
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    match = _VENDOR.fullmatch(_one_header(body, "From"))
    family = vendor_family(match[1]) if match else None
    if family is None:
        raise EvidenceError("From must start with a known vendor name")
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
    return family, "APPROVE"


# A declaration never carries a verdict. Any Markdown heading or verdict word
# makes the comment a (possibly mistyped) review, so a mistyped BLOCK cannot be
# read as a declaration and disappear.
_VERDICT_LIKE = re.compile(r"(?im)^ {0,3}#|\bblock|\bapprove")


def is_legacy_coordinator_note(body: str) -> bool:
    """Return True for an old ``From: Coordinator`` approval note.

    Coordinator approval is no longer required. Old notes grant nothing and are
    ignored so they do not fail open pull requests.
    """
    if review_headings(body):
        return False
    text = declaration_text(body)
    froms = re.findall(r"(?m)^From: (.*)$", text)
    approvals = re.findall(r"(?m)^Coordinator-Approval: (.*)$", text)
    return (
        len(froms) == 1
        and froms[0].strip() == "Coordinator"
        and len(approvals) == 1
        and approvals[0].strip() == "APPROVE"
    )


def parse_author_declaration(body: str, expected_head: str) -> str:
    """Return the author vendor family for an exact-head author declaration.

    The format is unchanged: one ``From: <vendor>`` line and one
    ``Reviewed-Head: <sha>`` line, with no review heading.
    """
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    if review_headings(body) or _VERDICT_LIKE.search(declaration_text(body)):
        raise EvidenceError("author declaration must not contain a heading or verdict")
    match = _VENDOR.fullmatch(_one_header(body, "From"))
    family = vendor_family(match[1]) if match else None
    if family is None:
        raise EvidenceError("From must start with a known vendor name")
    declared_head = _one_header(body, "Reviewed-Head")
    if not _SHA.fullmatch(declared_head) or declared_head != expected_head:
        raise EvidenceError("author declaration is malformed or stale")
    return family
