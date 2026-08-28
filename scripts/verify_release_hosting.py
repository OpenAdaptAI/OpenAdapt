#!/usr/bin/env python3
"""Verify GitHub controls required before an immutable release transaction."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
REQUIRED_TAG_RULES = frozenset({"creation", "update", "deletion"})


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _pattern_matches(pattern: str, ref: str) -> bool:
    if pattern == "~ALL":
        return True
    if "[" in pattern or "]" in pattern:
        raise ValueError(
            "ruleset ref patterns with character sets are unsupported by this gate"
        )
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                while index + 1 < len(pattern) and pattern[index + 1] == "*":
                    index += 1
                pieces.append(".*")
            else:
                pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
        index += 1
    return re.fullmatch("".join(pieces), ref) is not None


def _ruleset_applies(document: dict[str, Any], ref: str) -> bool:
    if document.get("target") != "tag" or document.get("enforcement") != "active":
        return False
    conditions = _object(document.get("conditions"), "ruleset conditions")
    ref_name = _object(conditions.get("ref_name"), "ruleset ref_name condition")
    include = ref_name.get("include")
    exclude = ref_name.get("exclude")
    if not isinstance(include, list) or not all(
        isinstance(pattern, str) for pattern in include
    ):
        raise ValueError("ruleset include patterns must be a list of strings")
    if not isinstance(exclude, list) or not all(
        isinstance(pattern, str) for pattern in exclude
    ):
        raise ValueError("ruleset exclude patterns must be a list of strings")
    return any(_pattern_matches(pattern, ref) for pattern in include) and not any(
        _pattern_matches(pattern, ref) for pattern in exclude
    )


def verify_release_hosting_documents(
    immutable: dict[str, Any], rulesets: list[dict[str, Any]], tag: str
) -> None:
    """Validate immutable releases and active creation/update/deletion tag rules."""
    if RELEASE_TAG.fullmatch(tag) is None:
        raise ValueError("release tag must be an exact stable vX.Y.Z value")
    if immutable.get("enabled") is not True:
        raise ValueError("GitHub immutable releases are not enabled")

    ref = f"refs/tags/{tag}"
    active_rules: set[str] = set()
    for index, ruleset in enumerate(rulesets):
        document = _object(ruleset, f"ruleset {index}")
        if not _ruleset_applies(document, ref):
            continue
        rules = document.get("rules")
        if not isinstance(rules, list):
            raise ValueError("ruleset rules must be a list")
        for rule in rules:
            item = _object(rule, "ruleset rule")
            rule_type = item.get("type")
            if isinstance(rule_type, str):
                active_rules.add(rule_type)

    missing = sorted(REQUIRED_TAG_RULES - active_rules)
    if missing:
        raise ValueError(f"{ref} lacks active tag rules: {', '.join(missing)}")


def _get_json(url: str, token: str) -> tuple[Any, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response), response.status


def load_release_hosting_documents(
    repository: str, *, api_url: str, token: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if REPOSITORY.fullmatch(repository) is None:
        raise ValueError("repository must have the owner/name form")
    base = api_url.rstrip("/")
    repository_path = "/".join(
        urllib.parse.quote(part, safe="") for part in repository.split("/")
    )
    immutable_raw, immutable_status = _get_json(
        f"{base}/repos/{repository_path}/immutable-releases", token
    )
    if immutable_status != 200:
        raise ValueError(
            f"GitHub immutable release query returned HTTP {immutable_status}"
        )
    immutable = _object(immutable_raw, "immutable release response")

    summaries: list[dict[str, Any]] = []
    page = 1
    while True:
        raw, status = _get_json(
            f"{base}/repos/{repository_path}/rulesets"
            f"?includes_parents=true&targets=tag&per_page=100&page={page}",
            token,
        )
        if status != 200:
            raise ValueError(f"GitHub ruleset query returned HTTP {status}")
        if not isinstance(raw, list):
            raise ValueError("GitHub ruleset response must be a list")
        summaries.extend(_object(item, "ruleset summary") for item in raw)
        if len(raw) < 100:
            break
        page += 1

    details: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for summary in summaries:
        if summary.get("enforcement") != "active":
            continue
        links = _object(summary.get("_links"), "ruleset summary links")
        self_link = _object(links.get("self"), "ruleset self link")
        url = self_link.get("href")
        if not isinstance(url, str) or not url.startswith(base + "/"):
            raise ValueError("ruleset self link is missing or outside the GitHub API")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        raw, status = _get_json(url, token)
        if status != 200:
            raise ValueError(f"GitHub ruleset detail query returned HTTP {status}")
        details.append(_object(raw, "ruleset detail"))
    return immutable, details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        parser.exit(1, "GH_TOKEN is required\n")
    try:
        immutable, rulesets = load_release_hosting_documents(
            args.repository, api_url=args.api_url, token=token
        )
        verify_release_hosting_documents(immutable, rulesets, args.tag)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"OK: GitHub immutable releases and tag rules protect {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
