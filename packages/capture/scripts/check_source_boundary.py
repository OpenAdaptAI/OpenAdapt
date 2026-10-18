#!/usr/bin/env python3
"""Reject private crown-jewel material from a public tree and its archives.

The rules come from ``source-policy.public.json``. The private canonical
manifest renders that file. This guard fails closed when the rendered policy is
missing, malformed, incomplete, or does not classify this repository as public.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
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
TREE_FILE_SIZE_LIMIT = 64 * 1024 * 1024
ARCHIVE_MEMBER_LIMIT = 10_000
ARCHIVE_MEMBER_SIZE_LIMIT = 64 * 1024 * 1024
ARCHIVE_EXPANDED_SIZE_LIMIT = 256 * 1024 * 1024
EXEMPT_TREE_PATTERN_PATHS = {
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
    archive_content_patterns: re.Pattern[str]
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
        if not stat.S_ISREG(path.lstat().st_mode):
            raise BoundaryError(f"rendered source policy is not a regular file: {path}")
    except OSError as exc:
        raise BoundaryError(f"cannot inspect rendered source policy {path}: {exc}") from exc
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoundaryError(f"cannot load rendered source policy {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise BoundaryError("the rendered source policy has an unknown schema")
    claimed_digest = document.get("policy_digest")
    unsigned_document = dict(document)
    unsigned_document.pop("policy_digest", None)
    canonical_payload = (
        json.dumps(unsigned_document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    )
    expected_digest = "sha256:" + hashlib.sha256(canonical_payload.encode()).hexdigest()
    if claimed_digest != expected_digest:
        raise BoundaryError(
            "the rendered source policy digest is invalid: "
            f"expected {expected_digest}, got {claimed_digest!r}"
        )

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
    archive_pattern_values = archives.get("content_patterns")
    if not isinstance(archive_pattern_values, list) or not archive_pattern_values:
        raise BoundaryError("built_artifacts.content_patterns must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in archive_pattern_values):
        raise BoundaryError("built_artifacts.content_patterns must contain non-empty strings")
    try:
        content_patterns = re.compile(
            "|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE
        )
        archive_content_patterns = re.compile(
            "|".join(f"(?:{pattern})" for pattern in archive_pattern_values),
            re.IGNORECASE,
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
        archive_content_patterns=archive_content_patterns,
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


def _inspect_archive_content(
    stream: BinaryIO,
    policy: Policy,
) -> tuple[bool, re.Match[str] | None, str]:
    """Apply the canonical signature and text-pattern rules to one member."""

    overlap = max(len(value) for value in policy.signatures) - 1
    tail = b""
    payload = bytearray()
    total = 0
    carries_signature = False
    while chunk := stream.read(1024 * 1024):
        total += len(chunk)
        candidate = tail + chunk
        if any(value in candidate for value in policy.signatures):
            carries_signature = True
        tail = candidate[-overlap:] if overlap else b""
        payload.extend(chunk)
    text = payload.decode("utf-8", errors="ignore")
    return carries_signature, policy.archive_content_patterns.search(text), text


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
        if problem := _path_violation(name, policy):
            violations.append(f"{name}: {problem}")
        full_path = root / name
        try:
            file_stat = full_path.lstat()
        except OSError as exc:
            violations.append(f"{name}: cannot inspect tracked source path: {exc}")
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            violations.append(f"{name}: tracked source path is not a regular file")
            continue
        with full_path.open("rb") as stream:
            if _contains_signature(stream, policy.signatures):
                violations.append(f"{name}: content carries a private-artifact signature")
                continue
        if file_stat.st_size > TREE_FILE_SIZE_LIMIT:
            violations.append(f"{name}: tracked source file is too large to inspect")
            continue
        if name in EXEMPT_TREE_PATTERN_PATHS:
            continue
        text = full_path.read_text(encoding="utf-8", errors="ignore")
        if match := policy.content_patterns.search(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{name}:{line}: content matches a forbidden pattern")
    return violations


def _safe_archive_name(name: str) -> bool:
    if "\\" in name or re.match(r"^[A-Za-z]:", name):
        return False
    normalized = name.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    canonical = candidate.as_posix()
    if normalized.endswith("/"):
        canonical += "/"
    return (
        bool(normalized)
        and normalized == canonical
        and not candidate.is_absolute()
        and not any(part in {"", ".", ".."} for part in candidate.parts)
    )


def _archive_members(path: Path) -> Iterator[tuple[str, BinaryIO]]:
    seen: set[str] = set()
    expanded_size = 0
    if path.suffix in {".whl", ".zip"}:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > ARCHIVE_MEMBER_LIMIT:
                raise BoundaryError(f"{path}: archive has too many members")
            for info in members:
                name = info.filename
                normalized_name = PurePosixPath(name).as_posix().casefold()
                if normalized_name in seen:
                    raise BoundaryError(f"{path}: duplicate archive member {name!r}")
                seen.add(normalized_name)
                if not _safe_archive_name(name):
                    raise BoundaryError(f"{path}: unsafe archive member path {name!r}")
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if info.flag_bits & 0x1:
                    raise BoundaryError(f"{path}: encrypted archive member {name!r}")
                if info.is_dir():
                    if file_type not in {0, stat.S_IFDIR}:
                        raise BoundaryError(f"{path}: non-directory archive member {name!r}")
                    continue
                if file_type and file_type != stat.S_IFREG:
                    raise BoundaryError(f"{path}: non-regular archive member {name!r}")
                if info.file_size > ARCHIVE_MEMBER_SIZE_LIMIT:
                    raise BoundaryError(f"{path}: archive member {name!r} is too large")
                expanded_size += info.file_size
                if expanded_size > ARCHIVE_EXPANDED_SIZE_LIMIT:
                    raise BoundaryError(f"{path}: expanded archive is too large")
                with archive.open(info) as stream:
                    yield name, stream
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > ARCHIVE_MEMBER_LIMIT:
                raise BoundaryError(f"{path}: archive has too many members")
            for member in members:
                name = member.name
                normalized_name = PurePosixPath(name).as_posix().casefold()
                if normalized_name in seen:
                    raise BoundaryError(f"{path}: duplicate archive member {name!r}")
                seen.add(normalized_name)
                if not _safe_archive_name(name):
                    raise BoundaryError(f"{path}: unsafe archive member path {name!r}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise BoundaryError(f"{path}: non-regular archive member {name!r}")
                if member.size > ARCHIVE_MEMBER_SIZE_LIMIT:
                    raise BoundaryError(f"{path}: archive member {name!r} is too large")
                expanded_size += member.size
                if expanded_size > ARCHIVE_EXPANDED_SIZE_LIMIT:
                    raise BoundaryError(f"{path}: expanded archive is too large")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BoundaryError(f"{path}: cannot read archive member {name!r}")
                with stream:
                    yield name, stream
        return
    raise BoundaryError(f"unsupported distribution archive: {path}")


def scan_archive(path: Path, policy: Policy) -> list[str]:
    violations: list[str] = []
    for name, stream in _archive_members(path):
        if problem := _path_violation(name, policy):
            violations.append(f"{path.name}:{name}: {problem}")
        carries_signature, match, text = _inspect_archive_content(stream, policy)
        if carries_signature:
            violations.append(f"{path.name}:{name}: content carries a private-artifact signature")
        elif match is not None:
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path.name}:{name}:{line}: content matches a forbidden pattern")
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
