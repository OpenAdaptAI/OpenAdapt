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


_HTML_END = re.compile(r"--!?>")


def _scan(body: str, *, keep_comments: bool) -> tuple[str, bool]:
    """Return text outside Markdown code fences and whether HTML was ambiguous.

    Backtick and tilde fences may be indented by up to three spaces. A fence
    closes only with the same character and at least its opening length. A
    fence-like line inside an HTML comment never opens a fence.

    With ``keep_comments`` (the veto view), HTML comment text is kept and a
    comment is taken to end only at ``-->``, the longest reading, so a fence
    line it might contain cannot hide later text. Without it (the approval
    view), a comment ends at ``-->`` or ``--!>`` and is removed. The flag
    reports a comment closed by ``--!>``, opened by ``<!-->`` or ``<!--->``,
    holding a nested ``<!--``, or never closed; an unclosed comment hides the
    rest of the approval view.
    """
    lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    in_comment = False
    ambiguous = False
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
        if (
            not in_comment
            and fence is not None
            and (fence[1][0] != "`" or "`" not in fence[2])
        ):
            fence_character, fence_length = fence[1][0], len(fence[1])
            continue
        out: list[str] = []
        position = 0
        while True:
            if in_comment:
                if keep_comments:
                    found = line.find("-->", position)
                    end = None if found < 0 else (found, found + 3, "-->")
                else:
                    match = _HTML_END.search(line, position)
                    end = (
                        None
                        if match is None
                        else (match.start(), match.end(), match[0])
                    )
                inner = line[position : len(line) if end is None else end[0]]
                if "<!--" in inner:
                    ambiguous = True
                if end is None:
                    if keep_comments:
                        out.append(line[position:])
                    break
                if end[2] != "-->":
                    ambiguous = True
                if keep_comments:
                    out.append(line[position : end[1]])
                position = end[1]
                in_comment = False
                continue
            start = line.find("<!--", position)
            if start < 0:
                out.append(line[position:])
                break
            out.append(line[position:start])
            if line.startswith(">", start + 4) or line.startswith("->", start + 4):
                ambiguous = True
            if keep_comments:
                out.append("<!--")
            position = start + 4
            in_comment = True
        lines.append("".join(out))
    if in_comment:
        ambiguous = True
    return "\n".join(lines), ambiguous


def veto_text(body: str) -> str:
    """Text outside code fences, with HTML comments kept.

    Used to find vetoes and current-head evidence: hiding text in an HTML
    comment can only remove approval, never a BLOCK.
    """
    return _scan(body, keep_comments=True)[0]


def declaration_text(body: str) -> str:
    """Text that can grant approval: outside fences and HTML comments."""
    return _scan(body, keep_comments=False)[0]


def has_ambiguous_html_comment(body: str) -> bool:
    return _scan(body, keep_comments=False)[1]


def _headings(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if re.match(
            r"^ {0,3}##[ \t]+independent[ \t]+adversarial[ \t]+review[ \t]*:",
            unicodedata.normalize("NFKC", line).casefold(),
        )
    ]


def review_headings(body: str) -> list[str]:
    """Find review headings for validation, including inside HTML comments."""
    return _headings(veto_text(body))


def visible_review_headings(body: str) -> list[str]:
    """Review headings outside fences and HTML comments."""
    return _headings(declaration_text(body))


def is_hidden_approval_example(body: str) -> bool:
    """True when every review heading is an APPROVE hidden in an HTML comment.

    Such a comment grants nothing and is ignored. A comment with any
    BLOCK-like text is never ignored.
    """
    raw = veto_text(body)
    return (
        bool(review_headings(body))
        and not visible_review_headings(body)
        and all(line == _REVIEW_HEADING for line in review_headings(body))
        and not re.search(r"(?i)\bblock", raw)
    )


def head_reference_lines(body: str) -> list[str]:
    """Retain head-like bindings for rejection, including decorated aliases.

    HTML comments are not stripped, so a hidden binding still counts here.
    """
    return [
        line
        for line in veto_text(body).splitlines()
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
    """Return (vendor family, verdict) for an exact-head review comment.

    Vetoes are found with HTML comments kept, before anything else, so a
    BLOCK can never be hidden. Approval must be outside any HTML comment, and
    a body with an ambiguous or unclosed comment never approves.
    """
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    headings = review_headings(body)
    if any(line == _BLOCK_HEADING for line in headings):
        raise EvidenceError("independent reviewer returned BLOCK")
    if len(headings) != 1 or headings[0] != _REVIEW_HEADING:
        raise EvidenceError(
            "expected exactly one existing review heading: APPROVE or BLOCK"
        )
    if has_ambiguous_html_comment(body):
        raise EvidenceError("review contains an ambiguous or unclosed HTML comment")
    match = _VENDOR.fullmatch(_one_header(body, "From"))
    family = vendor_family(match[1]) if match else None
    if family is None:
        raise EvidenceError("From must start with a known vendor name")
    reviewed_head = _one_header(body, "Reviewed-Head")
    if not _SHA.fullmatch(reviewed_head) or reviewed_head != expected_head:
        raise EvidenceError("review declaration is malformed or stale")
    if declaration_text(body).splitlines().count(_REVIEW_HEADING) != 1:
        raise EvidenceError("review approval is hidden in an HTML comment")
    return family, "APPROVE"


# A declaration never carries a verdict. Any Markdown heading or verdict word
# makes the comment a (possibly mistyped) review, so a mistyped BLOCK cannot be
# read as a declaration and disappear.
_VERDICT_LIKE = re.compile(r"(?im)^ {0,3}#|\bblock|\bapprove")


def is_legacy_coordinator_note(body: str, expected_head: str) -> bool:
    """Return True only for a clean old exact-head coordinator approval note.

    Coordinator approval is no longer required. A note with exactly one
    ``From: Coordinator``, one exact ``Reviewed-Head`` for this head, one
    ``Coordinator-Approval: APPROVE`` and nothing verdict-like besides is
    ignored. Any other shape returns False so the caller fails closed.
    """
    if not _SHA.fullmatch(expected_head) or review_headings(body):
        return False
    text = veto_text(body)
    approvals = re.findall(r"(?m)^Coordinator-Approval: (.*)$", text)
    visible = re.findall(r"(?m)^Coordinator-Approval: (.*)$", declaration_text(body))
    if len(approvals) != 1 or approvals[0] != "APPROVE" or visible != approvals:
        return False
    rest = re.sub(r"(?m)^Coordinator-Approval: APPROVE$", "", text)
    if _VERDICT_LIKE.search(rest) or re.search(r"(?i)coordinator-approval", rest):
        return False
    try:
        return (
            _one_header(body, "From") == "Coordinator"
            and _one_header(body, "Reviewed-Head") == expected_head
        )
    except EvidenceError:
        return False


def parse_author_declaration(body: str, expected_head: str) -> str:
    """Return the author vendor family for an exact-head author declaration.

    The format is unchanged: one ``From: <vendor>`` line and one
    ``Reviewed-Head: <sha>`` line, with no review heading.
    """
    if not _SHA.fullmatch(expected_head):
        raise EvidenceError("expected_head must be a full lowercase commit SHA")
    if review_headings(body) or _VERDICT_LIKE.search(veto_text(body)):
        raise EvidenceError("author declaration must not contain a heading or verdict")
    match = _VENDOR.fullmatch(_one_header(body, "From"))
    family = vendor_family(match[1]) if match else None
    if family is None:
        raise EvidenceError("From must start with a known vendor name")
    declared_head = _one_header(body, "Reviewed-Head")
    if not _SHA.fullmatch(declared_head) or declared_head != expected_head:
        raise EvidenceError("author declaration is malformed or stale")
    return family
