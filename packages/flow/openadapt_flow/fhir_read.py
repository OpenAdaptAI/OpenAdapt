"""Shared FHIR read directives and fail-closed page metadata checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional
from urllib.parse import urlsplit


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
        relation = link.get("relation")
        if not isinstance(relation, str) or not relation.strip():
            raise ValueError("a Bundle link relation is not nonempty text")
        if relation != "next":
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


def check_collection_search(
    bundle: Mapping[str, Any],
    resource_type: str,
    *,
    search_params: Optional[Mapping[str, Any]] = None,
    request_urls: tuple[str, ...] = (),
) -> None:
    """Validate the unpaged collection exception without granting read authority.

    Only the learner's unrestricted, empty-query search is supported here.
    A collection total can count a capped response instead of all matches;
    equal passes do not make unknown query semantics complete. Searchset
    pagination uses its separate protocol path.

    FHIR omits an empty entry array. An explicit zero total is still required;
    an absent total or a paging link cannot prove that no matches exist.
    """
    if search_params:
        raise ValueError("collection searches require an unrestricted empty query")
    for url in request_urls:
        _check_collection_url(url)
    total = search_total(bundle, None)
    if total is None:
        raise ValueError("a collection search Bundle requires a total")
    entries = bundle.get("entry", [])
    if not isinstance(entries, list):
        raise ValueError("a collection Bundle's entry is not a list")
    if total != len(entries):
        raise ValueError("a collection Bundle's total differs from its entry count")
    links = bundle.get("link", [])
    if not isinstance(links, list):
        raise ValueError("a collection Bundle's link is not a list")
    for link in links:
        if (
            not isinstance(link, dict)
            or link.get("relation") != "self"
            or not isinstance(link.get("url"), str)
            or not link["url"]
            or any(character.isspace() for character in link["url"])
        ):
            raise ValueError("a collection search Bundle permits only valid self links")
        _check_collection_url(link["url"])
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("a collection Bundle entry is not an object")
        resource = entry.get("resource")
        if (
            not isinstance(resource, dict)
            or resource.get("resourceType") != resource_type
            or resource.get("resourceType") == "OperationOutcome"
        ):
            raise ValueError("a collection entry is not the requested resource type")
        if "search" in entry:
            search = entry["search"]
            if not isinstance(search, dict) or search.get("mode", "match") != "match":
                raise ValueError("a collection search entry is not a matching resource")


def check_collection_url(url: str) -> None:
    """The same empty-query rule covers sent, effective and self-link URLs."""
    try:
        query = urlsplit(url).query
    except ValueError as error:
        raise ValueError("a collection search has a malformed URL") from error
    if query:
        raise ValueError("collection searches require an unrestricted empty query")


_check_collection_url = check_collection_url


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
        if code == "informational" and isinstance(details, dict):
            coding = details.get("coding")
            no_match = (
                isinstance(coding, list)
                and bool(coding)
                and all(
                    isinstance(item, dict)
                    and item.get("system")
                    == "http://terminology.hl7.org/CodeSystem/operation-outcome"
                    and item.get("code") == "MSG_NO_MATCH"
                    for item in coding
                )
            )
            if no_match and not has_matches:
                continue
        raise ValueError("the search returned an unqualified or incomplete outcome")
