#!/usr/bin/env python3
"""Fail CI when deployment-specific or private-boundary content enters a
public core repository.

Motivating case: the PowerChart incident. Application-specific workflow
content tied to a real customer deployment of an EHR (Cerner PowerChart)
reached a public core surface and had to be excised by hand after review
caught it. Application-specific recipes, customer fixtures, and proprietary
system identifiers derived from real deployments are exactly the class of
content the open-core boundary keeps private (see the workspace
source-policy.yaml: grown corpora, tuned adversary parameters,
deployment-derived thresholds, oracle/connector recipes, and real-EMR-tied
datasets are crown jewels). This check makes that class of leak a CI failure
instead of a code-review coin flip.

It complements, and deliberately overlaps with, the packaging-time guard in
openadapt-flow's scripts/check_release_consistency.py (which inspects built
wheels/sdists). This script inspects the REPOSITORY TREE, so the leak is
caught at PR time in any public core repo, not only when an artifact is
built.

Two kinds of matching, so a rename cannot dodge the check:

* Path tokens: any tracked file whose path contains a denylisted token fails.
* Content signatures: any tracked text file whose contents match a
  denylisted pattern fails.

Files that legitimately DISCUSS the boundary (this script, policy docs,
contributor docs) are covered by the allowlist below.

Usage:
    python scripts/check_source_boundary.py [--root PATH]

--root defaults to this repository. Point it at another checkout to run the
same gate in that repo's CI without vendoring a divergent copy.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

# Path fragments that indicate private-boundary or deployment-specific
# content. Lowercase; matched case-insensitively against the full repo-
# relative path.
DENYLISTED_PATH_TOKENS = (
    # Proprietary systems of record: per-system recipes/fixtures stay private.
    "powerchart",
    "cerner",
    "millennium_recipe",
    "epic_hyperspace",
    "meditech",
    # Customer/deployment-derived material.
    "customer_fixture",
    "customer_recipe",
    "client_fixture",
    "deployment_corpus",
    "deployment_thresholds",
    "real_emr",
    # Crown-jewel categories from source-policy.yaml.
    "grown_corpus",
    "tuned_adversary",
    "adversary_corpus",
    "identity_roc",
    "held_out_corpus",
    "oracle_recipe",
    "effect_oracle_recipe",
    "pixel_verify_cert",
    "enterprise_productionized",
    "paid_agent_evidence",
    # The private repos themselves must never be vendored into a public one.
    "openadapt-corpus",
    "openadapt-cloud",
    "openadapt-internal",
)

# Content signatures for text files. Case-insensitive regular expressions.
DENYLISTED_CONTENT_PATTERNS = (
    # Proprietary EHR surfaces showing up inside code, fixtures, or recipes.
    r"powerchart",
    r"cerner\s+millennium",
    r"epic\s+hyperspace",
    # Markers our private artifacts carry.
    r"openadapt[_-]corpus[/:]",
    r"deployment[_-]derived\s+threshold\s*=",
    r"oracle[_-]recipe[_-]id\s*[:=]",
)

# Repo-relative paths (exact file or directory prefix) that may mention the
# denylisted terms because they document or enforce the boundary itself.
ALLOWLISTED_PATHS = (
    "scripts/check_source_boundary.py",
    "docs/platform-manifest.md",
    "CONTRIBUTING.md",
    "TRADEMARKS.md",
)

# Binary-ish extensions we skip for content scanning (path scan still applies).
SKIP_CONTENT_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".icns",
    ".pdf",
    ".zip",
    ".gz",
    ".whl",
    ".mp4",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".db",
    ".sqlite",
}

MAX_CONTENT_BYTES = 5 * 1024 * 1024


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [p for p in result.stdout.decode().split("\0") if p]


def _is_allowlisted(path: str) -> bool:
    return any(
        path == allowed or path.startswith(allowed.rstrip("/") + "/")
        for allowed in ALLOWLISTED_PATHS
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Repository checkout to scan (default: this repository).",
    )
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        tracked = _tracked_files(root)
    except subprocess.CalledProcessError as exc:
        print(f"FATAL: git ls-files failed in {root}: {exc}", file=sys.stderr)
        return 2

    content_regex = re.compile(
        "|".join(f"(?:{p})" for p in DENYLISTED_CONTENT_PATTERNS), re.IGNORECASE
    )

    violations: list[str] = []
    for rel_path in tracked:
        if _is_allowlisted(rel_path):
            continue
        lower = rel_path.lower()
        for token in DENYLISTED_PATH_TOKENS:
            if token in lower:
                violations.append(
                    f"{rel_path}: path contains denylisted token {token!r}"
                )
                break

        full_path = root / rel_path
        if full_path.suffix.lower() in SKIP_CONTENT_SUFFIXES or not full_path.is_file():
            continue
        try:
            if full_path.stat().st_size > MAX_CONTENT_BYTES:
                continue
            text = full_path.read_text(errors="ignore")
        except OSError:
            continue
        match = content_regex.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"{rel_path}:{line}: content matches denylisted pattern "
                f"{match.group(0)!r}"
            )

    if violations:
        print(
            "Source-availability boundary violations found. Deployment-"
            "specific recipes, customer fixtures, and proprietary system "
            "identifiers must not enter public core repos (see docstring; "
            "motivating case: the PowerChart incident).",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"ERROR: {violation}", file=sys.stderr)
        return 1

    print(f"OK: no boundary violations across {len(tracked)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
