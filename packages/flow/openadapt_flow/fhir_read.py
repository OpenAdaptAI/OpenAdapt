"""Shared FHIR read directives and fail-closed page metadata checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional


def fhir_read_headers(
    private_read_headers: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """One coherent no-cache directive; private preferences cannot replace it.

    This requests origin validation. It does not certify search visibility,
    completeness, or a learned server cache-window bound.
    """
    headers = {"Accept": "application/fhir+json"}
    seen: set[str] = set()
    if private_read_headers is not None:
        if not isinstance(private_read_headers, Mapping):
            raise ValueError("FHIR read headers must be a mapping")
        for name, value in private_read_headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("FHIR read header names and values must be text")
            folded = name.casefold()
            if folded in seen:
                raise ValueError("FHIR read header is repeated case-insensitively")
            seen.add(folded)
            if folded == "cache-control":
                if value.strip().casefold() != "no-cache":
                    raise ValueError("FHIR read cache directive must be no-cache only")
                continue
            if folded == "accept":
                if value.strip().casefold() != "application/fhir+json":
                    raise ValueError("FHIR read Accept must be application/fhir+json")
                continue
            headers[name] = value
    headers["Cache-Control"] = "no-cache"
    return headers


def next_page_url(bundle: Mapping[str, Any]) -> Optional[str]:
    """Reject malformed/ambiguous next links instead of treating them as EOF."""
    if "link" not in bundle:
        return None
    links = bundle["link"]
    if not isinstance(links, list):
        raise ValueError("a Bundle's link is not a list")
    next_urls: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            raise ValueError("a Bundle link is not an object")
        if link.get("relation") != "next":
            continue
        url = link.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("a Bundle's next link is not a nonempty URL")
        next_urls.append(url)
    if len(next_urls) > 1:
        raise ValueError("a Bundle has ambiguous next links")
    return next_urls[0] if next_urls else None


def search_total(bundle: Mapping[str, Any], previous: Optional[int]) -> Optional[int]:
    """Remember an observed total and reject invalid or changing counts."""
    if "total" not in bundle:
        return previous
    total = bundle["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("a search Bundle's total is not a nonnegative integer")
    if previous is not None and total != previous:
        raise ValueError("a search Bundle's total changed across pages")
    return total


def check_search_coverage(total: Optional[int], matches: int) -> None:
    """Reject observed caps/count conflicts, without certifying native coverage.

    No total or terminal page alone proves an uncapped search. The adapter
    still needs an independently qualified native complete-search profile.
    """
    if total is not None and total != matches:
        raise ValueError(
            f"the Bundle's total is {total}, the read returned {matches} matches"
        )


def check_search_outcome(outcome: Mapping[str, Any], *, has_matches: bool) -> None:
    """Only understood informational notes can accompany a complete search.

    Severity alone and absence of a partial-results code are not completeness
    evidence. A no-match message cannot coexist with actual matching records.
    """
    if outcome.get("resourceType") != "OperationOutcome":
        raise ValueError("the search returned an unreadable outcome")
    issues = outcome.get("issue")
    if not isinstance(issues, list) or not issues:
        raise ValueError("the search returned an unreadable outcome")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in (
            "warning",
            "information",
        ):
            raise ValueError("the search returned an error outcome")
        code = issue.get("code")
        details = issue.get("details")
        if (
            code == "informational"
            and details is None
            and issue.get("diagnostics") is None
        ):
            continue
        if code in ("informational", "not-found", "processing") and isinstance(
            details, dict
        ):
            coding = details.get("coding")
            no_match = isinstance(coding, list) and bool(coding) and all(
                isinstance(item, dict)
                and item.get("system")
                == "http://terminology.hl7.org/CodeSystem/operation-outcome"
                and item.get("code") == "MSG_NO_MATCH"
                for item in coding
            )
            if no_match and not has_matches:
                continue
        raise ValueError("the search returned an unqualified or incomplete outcome")
