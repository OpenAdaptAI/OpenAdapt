"""Built archives use the same content rules as the public source tree."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_source_boundary import (
    BoundaryError,
    Policy,
    load_policy,
    scan_archive,
    scan_tree,
)

PAST_OLD_SCAN_LIMIT = 5 * 1024 * 1024 + 1


def _with_digest(document: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    return {
        **document,
        "policy_digest": "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
    }


def _policy() -> Policy:
    return Policy(
        path_tokens=("forbidden_path_token",),
        private_segments=frozenset({"private"}),
        path_prefixes=("forbidden/prefix",),
        content_patterns=re.compile(r"oracle[_-]recipe[_-]id\s*[:=]", re.IGNORECASE),
        archive_content_patterns=re.compile(
            r"deployment[-_]derived\s+threshold\s*=", re.IGNORECASE
        ),
        signatures=(b"UNIT-TEST-PRIVATE-SIGNATURE",),
        repository="openadapt-capture",
        digest="sha256:" + "a" * 64,
    )


def test_policy_loads_the_archive_specific_content_patterns(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    document = _with_digest(
        {
            "schema_version": 1,
            "crown_jewel_categories": ["grown_corpus"],
            "public_repositories": {
                "openadapt-capture": {
                    "classification": "public",
                    "must_not_contain": ["grown_corpus"],
                }
            },
            "enforcement": {
                "path_tokens": ["grown_corpus"],
                "private_path_segments": ["private"],
                "content_signature_parts": [["PRIVATE", "-SIGNATURE"]],
                "repository_tree": {"content_patterns": ["tree_only_pattern"]},
                "built_artifacts": {
                    "path_prefixes": ["forbidden/prefix"],
                    "content_patterns": ["archive_only_pattern"],
                },
            },
        }
    )
    policy_path.write_text(json.dumps(document), encoding="utf-8")

    policy = load_policy(policy_path, "openadapt-capture")

    assert policy.content_patterns.search("tree_only_pattern")
    assert not policy.content_patterns.search("archive_only_pattern")
    assert policy.archive_content_patterns.search("archive_only_pattern")


def test_policy_rejects_a_missing_archive_content_pattern_field(tmp_path: Path) -> None:
    document = {
        "schema_version": 1,
        "crown_jewel_categories": ["grown_corpus"],
        "public_repositories": {
            "openadapt-capture": {
                "classification": "public",
                "must_not_contain": ["grown_corpus"],
            }
        },
        "enforcement": {
            "path_tokens": ["grown_corpus"],
            "private_path_segments": ["private"],
            "content_signature_parts": [["PRIVATE", "-SIGNATURE"]],
            "repository_tree": {"content_patterns": ["tree_only_pattern"]},
            "built_artifacts": {"path_prefixes": ["forbidden/prefix"]},
        },
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(_with_digest(document)), encoding="utf-8")

    with pytest.raises(BoundaryError, match="built_artifacts.content_patterns"):
        load_policy(policy_path, "openadapt-capture")


def test_policy_rejects_a_digest_that_does_not_match_the_document(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy_digest": "sha256:" + "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BoundaryError, match="policy digest is invalid"):
        load_policy(policy_path, "openadapt-capture")


def test_policy_rejects_a_non_regular_policy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "policy.json"
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFLNK, st_size=0),
    )

    with pytest.raises(BoundaryError, match="policy is not a regular file"):
        load_policy(policy_path, "openadapt-capture")


def test_archive_scan_applies_the_canonical_content_pattern(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openadapt_capture/innocent_name.py",
            "deployment-" + "derived threshold = 0.91\n",
        )

    assert scan_archive(wheel, _policy()) == [
        "candidate.whl:openadapt_capture/innocent_name.py:1: content matches a forbidden pattern"
    ]


def test_archive_scan_accepts_unrelated_public_text(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/module.py", "CAPTURE_SCHEMA = 'v1'\n")

    assert scan_archive(wheel, _policy()) == []


def test_archive_scan_checks_content_after_the_tree_scan_limit(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    content = "x" * PAST_OLD_SCAN_LIMIT
    content += "\ndeployment-" + "derived threshold = 0.91\n"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("openadapt_capture/innocent_name.py", content)

    assert scan_archive(wheel, _policy()) == [
        "candidate.whl:openadapt_capture/innocent_name.py:2: content matches a forbidden pattern"
    ]


def test_archive_scan_rejects_an_unsafe_member_path(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../outside.py", "safe = True\n")

    with pytest.raises(BoundaryError, match="unsafe archive member path"):
        scan_archive(wheel, _policy())


def test_archive_scan_rejects_duplicate_members(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/module.py", "first = True\n")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("openadapt_capture/module.py", "second = True\n")

    with pytest.raises(BoundaryError, match="duplicate archive member"):
        scan_archive(wheel, _policy())


@pytest.mark.parametrize(
    "name",
    ["openadapt_capture//module.py", "openadapt_capture/./module.py"],
)
def test_archive_scan_rejects_noncanonical_member_paths(
    tmp_path: Path,
    name: str,
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(name, "safe = True\n")

    with pytest.raises(BoundaryError, match="unsafe archive member path"):
        scan_archive(wheel, _policy())


def test_archive_scan_rejects_casefold_member_collisions(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/Module.py", "first = True\n")
        archive.writestr("openadapt_capture/module.py", "second = True\n")

    with pytest.raises(BoundaryError, match="duplicate archive member"):
        scan_archive(wheel, _policy())


def test_archive_scan_rejects_links(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    link = zipfile.ZipInfo("openadapt_capture/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(link, "module.py")

    with pytest.raises(BoundaryError, match="non-regular archive member"):
        scan_archive(wheel, _policy())


def test_archive_scan_rejects_a_link_disguised_as_a_directory(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    link = zipfile.ZipInfo("openadapt_capture/link/")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(link, b"")

    with pytest.raises(BoundaryError, match="non-directory archive member"):
        scan_archive(wheel, _policy())


def test_archive_scan_rejects_an_encrypted_member(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/module.py", "safe = True\n")
    payload = bytearray(wheel.read_bytes())
    central = payload.index(b"PK\x01\x02")
    for offset in (6, central + 8):
        flags = int.from_bytes(payload[offset : offset + 2], "little") | 0x1
        payload[offset : offset + 2] = flags.to_bytes(2, "little")
    wheel.write_bytes(payload)

    with pytest.raises(BoundaryError, match="encrypted archive member"):
        scan_archive(wheel, _policy())


def test_archive_scan_bounds_expanded_member_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("openadapt_capture/module.py", "safe = True\n")
    monkeypatch.setattr("scripts.check_source_boundary.ARCHIVE_MEMBER_SIZE_LIMIT", 4)

    with pytest.raises(BoundaryError, match="archive member .* is too large"):
        scan_archive(wheel, _policy())


def test_tree_scan_rejects_a_tracked_symbolic_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.check_source_boundary.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"linked.py\0"),
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(st_mode=stat.S_IFLNK, st_size=0),
    )

    assert scan_tree(tmp_path, _policy()) == [
        "linked.py: tracked source path is not a regular file"
    ]


def test_tree_pattern_exemption_still_checks_private_signatures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "scripts" / "check_source_boundary.py"
    script.parent.mkdir()
    script.write_bytes(b"UNIT-TEST-PRIVATE-SIGNATURE")
    monkeypatch.setattr(
        "scripts.check_source_boundary.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"scripts/check_source_boundary.py\0"),
    )

    assert scan_tree(tmp_path, _policy()) == [
        "scripts/check_source_boundary.py: content carries a private-artifact signature"
    ]


def test_tree_scan_fails_closed_on_an_oversized_tracked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = tmp_path / "large.bin"
    tracked.write_bytes(b"safe")
    monkeypatch.setattr(
        "scripts.check_source_boundary.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"large.bin\0"),
    )
    monkeypatch.setattr("scripts.check_source_boundary.TREE_FILE_SIZE_LIMIT", 3)

    assert scan_tree(tmp_path, _policy()) == [
        "large.bin: tracked source file is too large to inspect"
    ]
