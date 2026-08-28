#!/usr/bin/env python3
"""Fail CI when deployment-specific or private-boundary content enters a
public core repository.

Motivating case: the PowerChart incident. Application-specific workflow
content tied to a real customer deployment of an EHR (Cerner PowerChart)
reached a public core surface and had to be excised by hand after review
caught it. Application-specific recipes, customer fixtures, and proprietary
system identifiers derived from real deployments are exactly the class of
content the open-core boundary keeps private (grown corpora, tuned adversary
parameters, deployment-derived thresholds, oracle/connector recipes, and
real-EMR-tied datasets are crown jewels). This check makes that class of leak
a CI failure instead of a code-review coin flip.

Where the rules come from
-------------------------
This script owns no denylist. Every token, pattern, and signature it enforces
is read from `source-policy.public.json` in this repository, which is RENDERED
from the canonical source-availability manifest
`OpenAdaptAI/openadapt-internal:source-policy.yaml`. That manifest is private
and a public CI job must never be able to read it, so the publishable subset of
it travels outward as a generated file instead. To change a rule, change the
canonical manifest and re-render; a job in the private repository compares this
copy against the canonical one daily and fails while they disagree.

The script therefore FAILS CLOSED. A missing, unparseable, or incomplete
`source-policy.public.json` exits 2 and stops the build. A guard that passes
because it found no rules is worse than the hardcoded list it replaced, because
everyone believes it ran.

Two kinds of matching, so a rename cannot dodge the check:

* Path tokens and private path segments: any tracked file whose path contains a
  denylisted token, or lies under a private segment, fails.
* Content signatures and patterns: any tracked text file whose contents match a
  denylisted regular expression, or carry a private-artifact banner, fails.

It complements, and deliberately overlaps with, the packaging-time guard in
openadapt-flow's scripts/check_release_consistency.py. This script inspects the
repository tree and, when ``--dist`` is set, the actual wheel and source archive
members. This catches a leak at PR time and again after the build tool generates
the release artifacts.

Files that legitimately DISCUSS the boundary (this script, the rendered policy
itself, policy docs, contributor docs) are covered by the allowlist below. That
allowlist is repository-local knowledge -- which files here are about the
boundary -- and is deliberately not part of the shared policy.

Usage:
    python scripts/check_source_boundary.py [--root PATH] [--repo NAME]
        [--dist PATH]

--root defaults to this repository. Point it at another checkout to run the
same gate in that repo's CI without vendoring a divergent copy; the rules are
always read from THIS checkout, never from the tree being scanned.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = DEFAULT_ROOT / "source-policy.public.json"
POLICY_SCHEMA_VERSION = 1

# Repo-relative paths (exact file or directory prefix) that may mention the
# denylisted terms because they document or enforce the boundary itself.
ALLOWLISTED_PATHS = (
    "scripts/check_source_boundary.py",
    "source-policy.public.json",
    "tests/test_source_boundary.py",
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
MAX_ARTIFACT_MEMBER_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARTIFACT_MEMBERS = 10_000


class PolicyError(RuntimeError):
    """The rendered policy is missing, unparseable, or incomplete."""


def _require_strings(
    container: dict, key: str, *, where: str, lower: bool = True
) -> list[str]:
    value = container.get(key)
    if not isinstance(value, list) or not value:
        raise PolicyError(f"{where}.{key} must be a non-empty list")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{where}.{key} must contain non-empty strings")
        items.append(item.lower() if lower else item)
    return items


class SourcePolicy:
    """The subset of the rendered manifest this guard enforces."""

    def __init__(self, document: dict) -> None:
        if document.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise PolicyError(
                f"schema_version is {document.get('schema_version')!r}, expected "
                f"{POLICY_SCHEMA_VERSION}; this guard refuses to enforce an "
                "unknown policy schema"
            )
        enforcement = document.get("enforcement")
        if not isinstance(enforcement, dict):
            raise PolicyError("enforcement: block is missing")

        self.path_tokens = tuple(
            _require_strings(enforcement, "path_tokens", where="enforcement")
        )
        self.private_path_segments = frozenset(
            _require_strings(enforcement, "private_path_segments", where="enforcement")
        )

        built = enforcement.get("built_artifacts")
        if not isinstance(built, dict):
            raise PolicyError("enforcement.built_artifacts: block is missing")
        self.built_artifact_path_prefixes = tuple(
            prefix.rstrip("/")
            for prefix in _require_strings(
                built, "path_prefixes", where="enforcement.built_artifacts"
            )
        )

        tree = enforcement.get("repository_tree")
        if not isinstance(tree, dict):
            raise PolicyError("enforcement.repository_tree: block is missing")
        patterns = _require_strings(
            tree, "content_patterns", where="enforcement.repository_tree", lower=False
        )
        try:
            self.content_regex = re.compile(
                "|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE
            )
        except re.error as exc:
            raise PolicyError(
                f"enforcement.repository_tree.content_patterns is not valid: {exc}"
            ) from exc

        # Signatures arrive as PARTS and are joined here on purpose: a whole
        # banner written literally into the policy file would make this guard
        # fail on its own rule file.
        signature_parts = enforcement.get("content_signature_parts")
        if not isinstance(signature_parts, list) or not signature_parts:
            raise PolicyError(
                "enforcement.content_signature_parts must be a non-empty list"
            )
        signatures = []
        for entry in signature_parts:
            if not isinstance(entry, list) or not entry:
                raise PolicyError(
                    "enforcement.content_signature_parts entries must be "
                    "non-empty lists of parts"
                )
            joined = "".join(str(part) for part in entry)
            if not joined:
                raise PolicyError("a content signature is empty")
            signatures.append(joined)
        self.content_signatures = tuple(signatures)

        repositories = document.get("public_repositories")
        if not isinstance(repositories, dict) or not repositories:
            raise PolicyError("public_repositories: must be a non-empty mapping")
        self.public_repositories = repositories
        self.policy_digest = str(document.get("policy_digest", "unknown"))
        self.policy_last_updated = str(document.get("policy_last_updated", "unknown"))


def load_policy(path: Path = POLICY_PATH) -> SourcePolicy:
    """Load the rendered policy, or raise so the caller can fail closed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(
            f"cannot read the rendered source policy {path}: {exc}. It is rendered "
            "from OpenAdaptAI/openadapt-internal:source-policy.yaml and must be "
            "committed in this repository"
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PolicyError(f"{path} did not parse to an object")
    return SourcePolicy(document)


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [p for p in result.stdout.decode().split("\0") if p]


def _repository_name(root: Path) -> str:
    """Name of the scanned repository, used to look up its classification."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return root.name
    url = result.stdout.decode().strip()
    if not url:
        return root.name
    # Match on the repository NAME rather than owner/name so a fork running the
    # same CI is checked rather than rejected.
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or root.name


def _is_allowlisted(path: str) -> bool:
    return any(
        path == allowed or path.startswith(allowed.rstrip("/") + "/")
        for allowed in ALLOWLISTED_PATHS
    )


def scan(root: Path, policy: SourcePolicy) -> list[str]:
    """Return every boundary violation among the tracked files under `root`."""
    violations: list[str] = []
    for rel_path in _tracked_files(root):
        if _is_allowlisted(rel_path):
            continue
        lower = rel_path.lower()
        matched_token = next(
            (token for token in policy.path_tokens if token in lower), None
        )
        if matched_token is not None:
            violations.append(
                f"{rel_path}: path contains denylisted token {matched_token!r}"
            )
        else:
            segment = next(
                (
                    part
                    for part in lower.split("/")
                    if part in policy.private_path_segments
                ),
                None,
            )
            if segment is not None:
                violations.append(
                    f"{rel_path}: path lies under private segment {segment!r}"
                )

        full_path = root / rel_path
        if full_path.suffix.lower() in SKIP_CONTENT_SUFFIXES or not full_path.is_file():
            continue
        try:
            if full_path.stat().st_size > MAX_CONTENT_BYTES:
                continue
            text = full_path.read_text(errors="ignore")
        except OSError:
            continue
        signature = next(
            (sig for sig in policy.content_signatures if sig in text), None
        )
        if signature is not None:
            violations.append(
                f"{rel_path}: content carries the private-artifact banner"
            )
            continue
        match = policy.content_regex.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"{rel_path}:{line}: content matches denylisted pattern "
                f"{match.group(0)!r}"
            )
    return violations


def _safe_archive_member_name(name: str, *, directory: bool) -> str | None:
    candidate = name[:-1] if directory and name.endswith("/") else name
    if (
        not candidate
        or candidate.startswith("/")
        or "\\" in candidate
        or (len(candidate) >= 2 and candidate[0].isalpha() and candidate[1] == ":")
    ):
        return None
    if any(part in {"", ".", ".."} for part in candidate.split("/")):
        return None
    return candidate


def _artifact_path_violations(
    artifact: str, member: str, policy: SourcePolicy
) -> list[str]:
    violations: list[str] = []
    lower = member.lower()
    token = next((item for item in policy.path_tokens if item in lower), None)
    if token is not None:
        violations.append(
            f"{artifact}:{member}: path contains denylisted token {token!r}"
        )
    segment = next(
        (part for part in lower.split("/") if part in policy.private_path_segments),
        None,
    )
    if segment is not None:
        violations.append(
            f"{artifact}:{member}: path lies under private segment {segment!r}"
        )
    prefix = next(
        (
            item
            for item in policy.built_artifact_path_prefixes
            if lower == item
            or lower.startswith(item + "/")
            or ("/" + item + "/") in ("/" + lower + "/")
        ),
        None,
    )
    if prefix is not None:
        violations.append(
            f"{artifact}:{member}: path matches private artifact prefix {prefix!r}"
        )
    return violations


def _artifact_content_violations(
    artifact: str, member: str, raw: bytes, policy: SourcePolicy
) -> list[str]:
    for signature in policy.content_signatures:
        if signature.encode("utf-8") in raw:
            return [f"{artifact}:{member}: content carries the private-artifact banner"]
    text = raw.decode("utf-8", errors="ignore")
    match = policy.content_regex.search(text)
    if match:
        line = text.count("\n", 0, match.start()) + 1
        return [
            f"{artifact}:{member}:{line}: content matches denylisted pattern "
            f"{match.group(0)!r}"
        ]
    return []


def _scan_zip_artifact(path: Path, policy: SourcePolicy) -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARTIFACT_MEMBERS:
            return [f"{path.name}: archive contains too many members"]
        for info in members:
            member = _safe_archive_member_name(info.filename, directory=info.is_dir())
            if member is None:
                violations.append(f"{path.name}:{info.filename}: unsafe archive path")
                continue
            if member in seen:
                violations.append(f"{path.name}:{member}: duplicate archive path")
                continue
            seen.add(member)
            violations.extend(_artifact_path_violations(path.name, member, policy))

            mode = (info.external_attr >> 16) & 0xFFFF
            entry_type = stat.S_IFMT(mode)
            if info.is_dir():
                if entry_type not in {0, stat.S_IFDIR}:
                    violations.append(
                        f"{path.name}:{member}: directory has an invalid entry type"
                    )
                continue
            if entry_type not in {0, stat.S_IFREG}:
                violations.append(
                    f"{path.name}:{member}: symlink or special entry is not permitted"
                )
                continue
            if info.flag_bits & 0x1:
                violations.append(
                    f"{path.name}:{member}: encrypted archive entry is not permitted"
                )
                continue
            if info.file_size > MAX_ARTIFACT_MEMBER_BYTES:
                violations.append(f"{path.name}:{member}: archive member is too large")
                continue
            total_bytes += info.file_size
            if total_bytes > MAX_ARTIFACT_TOTAL_BYTES:
                violations.append(f"{path.name}: expanded archive is too large")
                break
            violations.extend(
                _artifact_content_violations(
                    path.name, member, archive.read(info), policy
                )
            )
    return violations


def _scan_tar_artifact(path: Path, policy: SourcePolicy) -> list[str]:
    violations: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARTIFACT_MEMBERS:
            return [f"{path.name}: archive contains too many members"]
        for info in members:
            member = _safe_archive_member_name(info.name, directory=info.isdir())
            if member is None:
                violations.append(f"{path.name}:{info.name}: unsafe archive path")
                continue
            if member in seen:
                violations.append(f"{path.name}:{member}: duplicate archive path")
                continue
            seen.add(member)
            violations.extend(_artifact_path_violations(path.name, member, policy))
            if info.isdir():
                continue
            if not info.isfile():
                violations.append(
                    f"{path.name}:{member}: symlink or special entry is not permitted"
                )
                continue
            if info.size > MAX_ARTIFACT_MEMBER_BYTES:
                violations.append(f"{path.name}:{member}: archive member is too large")
                continue
            total_bytes += info.size
            if total_bytes > MAX_ARTIFACT_TOTAL_BYTES:
                violations.append(f"{path.name}: expanded archive is too large")
                break
            stream = archive.extractfile(info)
            if stream is None:
                violations.append(f"{path.name}:{member}: archive member is unreadable")
                continue
            violations.extend(
                _artifact_content_violations(path.name, member, stream.read(), policy)
            )
    return violations


def scan_built_artifacts(dist: Path, policy: SourcePolicy) -> list[str]:
    """Return policy and archive-structure violations in built distributions."""
    if dist.is_symlink() or not dist.is_dir():
        raise PolicyError(f"built artifact directory is missing or invalid: {dist}")
    artifacts = sorted(
        path
        for path in dist.iterdir()
        if path.name.endswith(".whl") or path.name.endswith(".tar.gz")
    )
    if not artifacts:
        raise PolicyError(f"built artifact directory has no wheel or sdist: {dist}")
    violations: list[str] = []
    for path in artifacts:
        if path.is_symlink() or not path.is_file():
            violations.append(f"{path.name}: artifact is not a regular file")
        elif path.name.endswith(".whl"):
            violations.extend(_scan_zip_artifact(path, policy))
        else:
            violations.extend(_scan_tar_artifact(path, policy))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Repository checkout to scan (default: this repository).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "Repository name to look up in the policy (default: derived from the "
            "scanned checkout's origin remote)."
        ),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=POLICY_PATH,
        help="Rendered policy to enforce (default: the copy in this repository).",
    )
    parser.add_argument(
        "--dist",
        type=Path,
        default=None,
        help="Built wheel and source archive directory to scan after the tree.",
    )
    args = parser.parse_args()
    root = args.root.resolve()

    # Fail closed: no rules, no run.
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    name = args.repo or _repository_name(root)
    if name not in policy.public_repositories:
        print(
            f"FATAL: {name!r} is not classified as a public repository in the "
            "source-availability manifest. Either it has no entry (add one to "
            "OpenAdaptAI/openadapt-internal:source-policy.yaml in the same change "
            "that creates the repository) or it is private, in which case this "
            "public-tree guard does not apply.",
            file=sys.stderr,
        )
        return 2

    try:
        violations = scan(root, policy)
        if args.dist is not None:
            violations.extend(scan_built_artifacts(args.dist.resolve(), policy))
    except subprocess.CalledProcessError as exc:
        print(f"FATAL: git ls-files failed in {root}: {exc}", file=sys.stderr)
        return 2
    except (OSError, PolicyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"FATAL: built artifact scan failed: {exc}", file=sys.stderr)
        return 2

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

    print(
        f"OK: no boundary violations in {name}"
        f"{' and its built artifacts' if args.dist is not None else ''} "
        f"(policy {policy.policy_digest}, updated {policy.policy_last_updated})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
