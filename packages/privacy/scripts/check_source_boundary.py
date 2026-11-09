#!/usr/bin/env python3
"""Reject private crown-jewel material from a public tree and its archives.

The rules come from ``source-policy.public.json``. The private canonical
manifest renders that file. This guard fails closed when the rendered policy is
missing, malformed, incomplete, or does not classify this repository as public.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "source-policy.public.json"
SCHEMA_VERSION = 1
CONTENT_SCAN_LIMIT = 5 * 1024 * 1024
EXEMPT_TREE_PATHS = {
    "scripts/check_source_boundary.py",
    "source-policy.public.json",
}


class BoundaryError(RuntimeError):
    """The policy or the inspected public artifact is invalid."""


def _strings(container: dict, key: str, *, where: str) -> tuple[str, ...]:
    value = container.get(key)
    if not isinstance(value, list) or not value:
        raise BoundaryError(f"{where}.{key} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise BoundaryError(f"{where}.{key} must contain non-empty strings")
    return tuple(item.lower() for item in value)


@dataclass(frozen=True)
class Policy:
    path_tokens: tuple[str, ...]
    private_segments: frozenset[str]
    path_prefixes: tuple[str, ...]
    content_patterns: re.Pattern[str]
    signatures: tuple[bytes, ...]
    repository: str
    digest: str


def _repository_name(root: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def load_policy(path: Path, repository: str) -> Policy:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"cannot load rendered source policy {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise BoundaryError("the rendered source policy has an unknown schema")

    enforcement = document.get("enforcement")
    repositories = document.get("public_repositories")
    categories = document.get("crown_jewel_categories")
    if not isinstance(enforcement, dict) or not isinstance(repositories, dict):
        raise BoundaryError("the rendered source policy is incomplete")
    if not isinstance(categories, list) or not categories:
        raise BoundaryError("the rendered source policy has no crown-jewel categories")
    entry = repositories.get(repository)
    if not isinstance(entry, dict) or entry.get("classification") != "public":
        raise BoundaryError(f"{repository!r} is not classified as public")
    forbidden = entry.get("must_not_contain")
    if not isinstance(forbidden, list) or not set(categories).issubset(forbidden):
        raise BoundaryError(f"{repository!r} does not forbid every crown-jewel category")

    tree = enforcement.get("repository_tree")
    archives = enforcement.get("built_artifacts")
    if not isinstance(tree, dict) or not isinstance(archives, dict):
        raise BoundaryError("the rendered source policy has no tree or archive rules")
    patterns = _strings(tree, "content_patterns", where="repository_tree")
    try:
        content_patterns = re.compile(
            "|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE
        )
    except re.error as exc:
        raise BoundaryError(f"the source-policy content patterns are invalid: {exc}") from exc

    parts = enforcement.get("content_signature_parts")
    if not isinstance(parts, list) or not parts:
        raise BoundaryError("the rendered source policy has no content signatures")
    signatures: list[bytes] = []
    for value in parts:
        if not isinstance(value, list) or not value or any(not str(part) for part in value):
            raise BoundaryError("a source-policy content signature is invalid")
        signatures.append("".join(str(part) for part in value).encode())

    policy = Policy(
        path_tokens=_strings(enforcement, "path_tokens", where="enforcement"),
        private_segments=frozenset(
            _strings(enforcement, "private_path_segments", where="enforcement")
        ),
        path_prefixes=_strings(archives, "path_prefixes", where="built_artifacts"),
        content_patterns=content_patterns,
        signatures=tuple(signatures),
        repository=repository,
        digest=str(document.get("policy_digest", "")),
    )
    if not policy.digest.startswith("sha256:"):
        raise BoundaryError("the rendered source policy has no digest")
    _self_test(policy)
    return policy


def _path_violation(name: str, policy: Policy) -> str | None:
    normalized = PurePosixPath(name.replace("\\", "/")).as_posix().lower().lstrip("./")
    token = next((value for value in policy.path_tokens if value in normalized), None)
    if token:
        return f"path contains forbidden token {token!r}"
    segment = next(
        (part for part in PurePosixPath(normalized).parts if part in policy.private_segments),
        None,
    )
    if segment:
        return f"path contains private segment {segment!r}"
    candidates = (normalized, "/".join(PurePosixPath(normalized).parts[1:]))
    prefix = next(
        (
            value
            for value in policy.path_prefixes
            if any(item == value or item.startswith(value.rstrip("/") + "/") for item in candidates)
        ),
        None,
    )
    return f"path uses forbidden archive prefix {prefix!r}" if prefix else None


def _contains_signature(stream: BinaryIO, signatures: tuple[bytes, ...]) -> bool:
    overlap = max(len(value) for value in signatures) - 1
    tail = b""
    while chunk := stream.read(1024 * 1024):
        payload = tail + chunk
        if any(value in payload for value in signatures):
            return True
        tail = payload[-overlap:] if overlap else b""
    return False


def _self_test(policy: Policy) -> None:
    if _path_violation(f"fixture/{policy.path_tokens[0]}/item.json", policy) is None:
        raise BoundaryError("the path-token guard failed its self-test")
    if _path_violation(f"fixture/{next(iter(policy.private_segments))}/item.json", policy) is None:
        raise BoundaryError("the private-segment guard failed its self-test")
    if not _contains_signature(io.BytesIO(policy.signatures[0]), policy.signatures):
        raise BoundaryError("the content-signature guard failed its self-test")


def scan_tree(root: Path, policy: Policy) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    violations: list[str] = []
    for name in (value for value in result.stdout.decode().split("\0") if value):
        if name in EXEMPT_TREE_PATHS:
            continue
        if problem := _path_violation(name, policy):
            violations.append(f"{name}: {problem}")
        full_path = root / name
        if not full_path.is_file():
            continue
        with full_path.open("rb") as stream:
            if _contains_signature(stream, policy.signatures):
                violations.append(f"{name}: content carries a private-artifact signature")
                continue
        if full_path.stat().st_size <= CONTENT_SCAN_LIMIT:
            text = full_path.read_text(encoding="utf-8", errors="ignore")
            if match := policy.content_patterns.search(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{name}:{line}: content matches a forbidden pattern")
    return violations


def _archive_members(path: Path) -> Iterator[tuple[str, BinaryIO]]:
    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    with archive.open(info) as stream:
                        yield info.filename, stream
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.isfile() and (stream := archive.extractfile(member)) is not None:
                    with stream:
                        yield member.name, stream
        return
    raise BoundaryError(f"unsupported distribution archive: {path}")


def scan_archive(path: Path, policy: Policy) -> list[str]:
    violations: list[str] = []
    for name, stream in _archive_members(path):
        if problem := _path_violation(name, policy):
            violations.append(f"{path.name}:{name}: {problem}")
        if _contains_signature(stream, policy.signatures):
            violations.append(f"{path.name}:{name}: content carries a private-artifact signature")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repo")
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--require-dist", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        repository = args.repo or _repository_name(root)
        policy = load_policy(args.policy, repository)
        violations = scan_tree(root, policy)
        if args.require_dist:
            archives = sorted((root / "dist").glob("*.whl")) + sorted(
                (root / "dist").glob("*.tar.gz")
            )
            if not any(path.suffix == ".whl" for path in archives) or not any(
                path.name.endswith(".tar.gz") for path in archives
            ):
                raise BoundaryError("dist must contain a wheel and a source distribution")
            for archive in archives:
                violations.extend(scan_archive(archive, policy))
    except (BoundaryError, OSError, subprocess.CalledProcessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    if violations:
        for violation in violations:
            print(f"ERROR: {violation}", file=sys.stderr)
        return 1
    scope = "source tree and distributions" if args.require_dist else "source tree"
    print(f"OK: {policy.repository} {scope} satisfy {policy.digest}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
