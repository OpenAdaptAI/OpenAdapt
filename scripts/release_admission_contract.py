#!/usr/bin/env python3
"""Build and verify the closed launcher release-admission transport contracts."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from verify_release_artifacts import verify_release_artifacts

TARGET = "openadapt"
CLAIM_SCOPE = "production_openadapt"
INVENTORY_SCHEMA = "openadapt.production-release-artifact-inventory/v1"
REFERENCE_SCHEMA = "openadapt.production-evidence-object-reference/v2"
TAG_BINDING_SCHEMA = "openadapt.production-release-tag-binding/v1"
STAGING_SCHEMA = "openadapt.production-release-staging-evidence/v1"
TAG_RULESET_SCHEMA = "openadapt.production-release-tag-ruleset/v1"
RELEASE_APP_ID = 4730708
RELEASE_APP_INSTALLATION_ID = 156835568
RELEASE_APP_BOT_ID = 321543906
SOURCE_REPOSITORY = "OpenAdaptAI/OpenAdapt"
SOURCE_REPOSITORY_ID = 627024850
IMMUTABLE_TAG_RULESET_NAME = "OpenAdapt policy: immutable release tags"
CREATION_TAG_RULESET_NAME = "OpenAdapt policy: release tag creation"
AUTHORITY_REPOSITORY = "OpenAdaptAI/.github"
AUTHORITY_REPOSITORY_ID = "858454062"
AUTHORITY_OWNER_ID = "132681217"
QUALIFICATION_RELEASE_KIND = "qualification-release"
QUALIFICATION_RELEASE_SCHEMA = "openadapt.qualification-release/v1"
QUALIFICATION_RELEASE_MEDIA_TYPE = (
    "application/vnd.openadapt.qualification-release+json;version=1"
)
INVENTORY_DOMAIN = b"OpenAdapt production release artifact inventory v1\0"
REFERENCE_DOMAIN = b"OpenAdapt production release tag admission reference v1\0"
STAGING_DOMAIN = b"OpenAdapt production release staging evidence v1\0"
TAG_RULESETS_DOMAIN = b"OpenAdapt production release tag rulesets v1\0"
IMMUTABLE_RELEASES_DOMAIN = b"OpenAdapt production immutable releases response v1\0"
TAG_REF_STATE_DOMAIN = b"OpenAdapt production release tag ref state v1\0"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

REFERENCE_KEYS = {
    "schema_version",
    "repository",
    "repository_id",
    "repository_owner_id",
    "registry_source_commit",
    "registry_revision",
    "registry_head_sha256",
    "registry_entry_sha256",
    "kind",
    "object_schema_version",
    "object_path",
    "object_sha256",
    "size_bytes",
    "object_media_type",
    "semantic_identity_sha256",
    "subject_sha256",
}
INVENTORY_KEYS = {"schema_version", "target", "claim_scope", "artifacts"}
ARTIFACT_KEYS = {
    "name",
    "kind",
    "sha256",
    "size_bytes",
    "media_type",
    "publish_destinations",
}
TAG_BINDING_KEYS = {
    "schema_version",
    "admission_reference",
    "admission_reference_sha256",
    "artifact_inventory_sha256",
}
STAGING_KEYS = {
    "schema_version",
    "repository",
    "repository_id",
    "draft_release_id",
    "tag",
    "tag_ref_state",
    "tag_ref_state_sha256",
    "target_commitish",
    "draft",
    "prerelease",
    "release_app_id",
    "release_app_installation_id",
    "release_app_bot_user_id",
    "release_author_login",
    "assets",
    "immutable_releases",
    "immutable_releases_sha256",
    "tag_rulesets",
    "tag_rulesets_sha256",
    "observed_at",
}
STAGED_ASSET_KEYS = ARTIFACT_KEYS | {
    "asset_id",
    "uploader_id",
    "uploader_login",
}
NORMALIZED_TAG_RULESET_KEYS = {
    "schema_version",
    "role",
    "repository",
    "repository_id",
    "ruleset_id",
    "name",
    "target",
    "enforcement",
    "bypass_actors",
    "conditions",
    "rules",
}
IMMUTABLE_RELEASES_KEYS = {"enabled", "enforced_by_owner"}
TAG_REF_STATE_KEYS = {"ref", "exists"}


def canonical_json(value: Any) -> bytes:
    """Return UTF-8 canonical JSON bytes for a closed JSON value."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _closed_object(value: Any, keys: set[str], context: str) -> dict[str, Any]:
    document = _object(value, context)
    if set(document) != keys:
        raise ValueError(f"{context} keys are invalid")
    return document


def _digest(value: Any, domain: bytes) -> str:
    return f"sha256:{_sha256_bytes(domain + canonical_json(value))}"


def build_artifact_inventory(dist_dir: Path) -> dict[str, Any]:
    """Return the exact wheel and sdist inventory accepted by the issuer."""

    wheel, sdist = verify_release_artifacts(dist_dir)
    artifacts = [
        {
            "name": wheel.name,
            "kind": "python-wheel",
            "sha256": _sha256_file(wheel),
            "size_bytes": wheel.stat().st_size,
            "media_type": "application/zip",
            "publish_destinations": ["github-release", "pypi"],
        },
        {
            "name": sdist.name,
            "kind": "python-sdist",
            "sha256": _sha256_file(sdist),
            "size_bytes": sdist.stat().st_size,
            "media_type": "application/gzip",
            "publish_destinations": ["github-release", "pypi"],
        },
    ]
    artifacts.sort(key=lambda item: (item["kind"], item["name"], item["sha256"]))
    inventory = {
        "schema_version": INVENTORY_SCHEMA,
        "target": TARGET,
        "claim_scope": CLAIM_SCOPE,
        "artifacts": artifacts,
    }
    validate_artifact_inventory(inventory)
    return inventory


def validate_artifact_inventory(value: Any) -> dict[str, Any]:
    """Validate and return one closed canonical artifact inventory."""

    inventory = _closed_object(value, INVENTORY_KEYS, "artifact inventory")
    if inventory["schema_version"] != INVENTORY_SCHEMA:
        raise ValueError("artifact inventory schema is invalid")
    if inventory["target"] != TARGET or inventory["claim_scope"] != CLAIM_SCOPE:
        raise ValueError("artifact inventory subject is invalid")

    artifacts = inventory["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValueError("artifact inventory must contain one wheel and one sdist")
    expected_types = {
        ("python-wheel", "application/zip", ".whl"),
        ("python-sdist", "application/gzip", ".tar.gz"),
    }
    actual_types: set[tuple[str, str, str]] = set()
    names: set[str] = set()
    sort_keys: list[tuple[str, str, str]] = []
    for raw_artifact in artifacts:
        artifact = _closed_object(raw_artifact, ARTIFACT_KEYS, "release artifact")
        name = artifact["name"]
        kind = artifact["kind"]
        media_type = artifact["media_type"]
        sha256 = artifact["sha256"]
        size_bytes = artifact["size_bytes"]
        destinations = artifact["publish_destinations"]
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in names
        ):
            raise ValueError("release artifact name is invalid or duplicated")
        if not isinstance(kind, str) or not isinstance(media_type, str):
            raise ValueError("release artifact type is invalid")
        suffix = ".tar.gz" if name.endswith(".tar.gz") else Path(name).suffix
        actual_types.add((kind, media_type, suffix))
        if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
            raise ValueError("release artifact digest is invalid")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
        ):
            raise ValueError("release artifact size is invalid")
        if destinations != ["github-release", "pypi"]:
            raise ValueError("release artifact publish destinations are invalid")
        names.add(name)
        sort_keys.append((kind, name, sha256))
    if actual_types != expected_types:
        raise ValueError("release artifact types are invalid")
    if sort_keys != sorted(sort_keys):
        raise ValueError("release artifacts are not canonically sorted")
    return inventory


def artifact_inventory_sha256(value: Any) -> str:
    """Return the issuer-defined digest for one validated inventory."""

    inventory = validate_artifact_inventory(value)
    subject = {
        "target": inventory["target"],
        "claim_scope": inventory["claim_scope"],
        "artifacts": inventory["artifacts"],
    }
    return _digest(subject, INVENTORY_DOMAIN)


def validate_admission_reference(value: Any) -> dict[str, Any]:
    """Validate the closed shape needed for a content-addressed v2 reference."""

    reference = _closed_object(value, REFERENCE_KEYS, "admission reference")
    commit = reference["registry_source_commit"]
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        raise ValueError("admission reference registry_source_commit is invalid")
    for key in (
        "registry_head_sha256",
        "registry_entry_sha256",
        "object_sha256",
        "semantic_identity_sha256",
    ):
        item = reference[key]
        if not isinstance(item, str) or SHA256.fullmatch(item) is None:
            raise ValueError(f"admission reference {key} is invalid")
    if reference["subject_sha256"] is not None:
        raise ValueError("regular admission reference subject_sha256 must be null")
    if (
        not isinstance(reference["size_bytes"], int)
        or isinstance(reference["size_bytes"], bool)
        or reference["size_bytes"] < 1
    ):
        raise ValueError("admission reference size is invalid")
    for key in REFERENCE_KEYS - {
        "repository_id",
        "repository_owner_id",
        "registry_revision",
        "size_bytes",
        "subject_sha256",
    }:
        if not isinstance(reference[key], str) or not reference[key]:
            raise ValueError(f"admission reference {key} is invalid")
    for key in ("repository_id", "repository_owner_id"):
        if (
            not isinstance(reference[key], str)
            or not reference[key].isdigit()
            or reference[key].startswith("0")
        ):
            raise ValueError(f"admission reference {key} is invalid")
    revision = reference["registry_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("admission reference registry_revision is invalid")
    if (
        reference["schema_version"] != REFERENCE_SCHEMA
        or reference["repository"] != AUTHORITY_REPOSITORY
        or reference["repository_id"] != AUTHORITY_REPOSITORY_ID
        or reference["repository_owner_id"] != AUTHORITY_OWNER_ID
        or reference["kind"] != QUALIFICATION_RELEASE_KIND
        or reference["object_schema_version"] != QUALIFICATION_RELEASE_SCHEMA
        or reference["object_media_type"] != QUALIFICATION_RELEASE_MEDIA_TYPE
    ):
        raise ValueError("admission reference authority or object kind is invalid")
    object_digest = reference["object_sha256"].removeprefix("sha256:")
    expected_path = (
        "production-evidence/objects/sha256/"
        f"{object_digest[:2]}/{object_digest}.qualification-release.json"
    )
    if reference["object_path"] != expected_path:
        raise ValueError("admission reference object path is not content-addressed")
    return reference


def admission_reference_sha256(value: Any) -> str:
    """Return the tag-binding digest for one closed admission reference."""

    return _digest(validate_admission_reference(value), REFERENCE_DOMAIN)


def build_tag_binding(
    admission_reference: Any,
    artifact_inventory: Any,
    verified_artifact_inventory_sha256: str,
) -> dict[str, Any]:
    """Bind the exact admission reference and admitted artifact inventory."""

    reference = validate_admission_reference(admission_reference)
    local_inventory_sha256 = artifact_inventory_sha256(artifact_inventory)
    if (
        SHA256.fullmatch(verified_artifact_inventory_sha256) is None
        or verified_artifact_inventory_sha256 != local_inventory_sha256
    ):
        raise ValueError("verified artifact inventory digest does not match")
    return {
        "schema_version": TAG_BINDING_SCHEMA,
        "admission_reference": reference,
        "admission_reference_sha256": admission_reference_sha256(reference),
        "artifact_inventory_sha256": local_inventory_sha256,
    }


def validate_tag_binding(value: Any) -> dict[str, Any]:
    """Validate one exact canonical tag annotation."""

    binding = _closed_object(value, TAG_BINDING_KEYS, "release tag binding")
    if binding["schema_version"] != TAG_BINDING_SCHEMA:
        raise ValueError("release tag binding schema is invalid")
    reference = validate_admission_reference(binding["admission_reference"])
    if binding["admission_reference_sha256"] != admission_reference_sha256(reference):
        raise ValueError("release tag admission reference digest does not match")
    digest = binding["artifact_inventory_sha256"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValueError("release tag artifact inventory digest is invalid")
    return binding


def tag_binding_bytes(value: Any) -> bytes:
    """Return one validated tag binding as canonical JSON plus exactly one LF."""

    return canonical_json(validate_tag_binding(value)) + b"\n"


def validate_tag_object(
    value: Any,
    *,
    expected_tag: str,
    expected_commit: str,
) -> tuple[str, dict[str, Any]]:
    """Validate an annotated GitHub tag object and return its SHA and binding."""

    document = _object(value, "GitHub tag object")
    tag_object_sha = document.get("sha")
    if not isinstance(tag_object_sha, str) or COMMIT.fullmatch(tag_object_sha) is None:
        raise ValueError("GitHub tag object SHA is invalid")
    if document.get("tag") != expected_tag:
        raise ValueError("GitHub tag object name does not match")
    target = _object(document.get("object"), "GitHub tag target")
    if target.get("type") != "commit" or target.get("sha") != expected_commit:
        raise ValueError("GitHub tag target does not match the admitted commit")

    message = document.get("message")
    if not isinstance(message, str) or not message:
        raise ValueError("GitHub tag annotation is missing")
    if not message.endswith("\n") or message.endswith("\n\n"):
        raise ValueError("GitHub tag annotation must end in exactly one LF")
    annotation = message[:-1]
    binding = validate_tag_binding(_load_json(annotation, "GitHub tag annotation"))
    if tag_binding_bytes(binding).decode("utf-8") != message:
        raise ValueError("GitHub tag annotation is not canonical compact JSON plus LF")
    return tag_object_sha, binding


def validate_tag_ref(
    value: Any,
    *,
    expected_tag: str,
    expected_tag_object_sha: str,
) -> None:
    """Require the exact immutable annotated tag object at its public ref."""

    document = _object(value, "GitHub tag ref")
    if COMMIT.fullmatch(expected_tag_object_sha) is None:
        raise ValueError("expected GitHub tag object SHA is invalid")
    if document.get("ref") != f"refs/tags/{expected_tag}":
        raise ValueError("GitHub tag ref does not match")
    target = _object(document.get("object"), "GitHub tag ref target")
    actual_sha = target.get("sha")
    if (
        target.get("type") != "tag"
        or not isinstance(actual_sha, str)
        or COMMIT.fullmatch(actual_sha) is None
        or actual_sha != expected_tag_object_sha
    ):
        raise ValueError("GitHub tag ref is not the exact annotated tag object")


def _ref_pattern_matches(pattern: str, ref: str) -> bool:
    return pattern == "~ALL" or fnmatch.fnmatchcase(ref, pattern)


def _tag_ruleset_subject(
    value: Any, *, expected_tag: str, expected_name: str
) -> tuple[int, list[Any], list[Any]]:
    """Return the ID, bypasses, and rules for one applicable active ruleset."""

    document = _object(value, "GitHub tag ruleset")
    if document.get("name") != expected_name:
        raise ValueError("GitHub tag ruleset name is invalid")
    ruleset_id = document.get("id")
    if (
        not isinstance(ruleset_id, int)
        or isinstance(ruleset_id, bool)
        or ruleset_id < 1
    ):
        raise ValueError("GitHub tag ruleset ID is invalid")
    if document.get("target") != "tag" or document.get("enforcement") != "active":
        raise ValueError("GitHub tag ruleset is not active for tags")

    conditions = _closed_object(
        document.get("conditions"), {"ref_name"}, "GitHub tag ruleset conditions"
    )
    ref_name = _closed_object(
        conditions.get("ref_name"), {"include", "exclude"}, "tag ref condition"
    )
    includes = ref_name["include"]
    excludes = ref_name["exclude"]
    if includes != ["refs/tags/v*"]:
        raise ValueError("GitHub tag ruleset includes are invalid")
    if excludes != []:
        raise ValueError("GitHub tag ruleset excludes are invalid")
    expected_ref = f"refs/tags/{expected_tag}"
    if not any(_ref_pattern_matches(item, expected_ref) for item in includes) or any(
        _ref_pattern_matches(item, expected_ref) for item in excludes
    ):
        raise ValueError("GitHub tag ruleset does not protect the release tag")

    bypass_actors = document.get("bypass_actors")
    if not isinstance(bypass_actors, list):
        raise ValueError("GitHub tag ruleset bypass actors are invalid")
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ValueError("GitHub tag ruleset rules are invalid")
    if not all(isinstance(rule, dict) for rule in rules):
        raise ValueError("GitHub tag ruleset contains an invalid rule")
    return ruleset_id, bypass_actors, rules


def validate_tag_rulesets(
    immutable_value: Any,
    creation_value: Any,
    *,
    expected_tag: str,
) -> tuple[int, int]:
    """Require separate immutable and release-App-only creation rulesets."""

    immutable_id, immutable_bypasses, immutable_rules = _tag_ruleset_subject(
        immutable_value,
        expected_tag=expected_tag,
        expected_name=IMMUTABLE_TAG_RULESET_NAME,
    )
    creation_id, creation_bypasses, creation_rules = _tag_ruleset_subject(
        creation_value,
        expected_tag=expected_tag,
        expected_name=CREATION_TAG_RULESET_NAME,
    )
    if immutable_id == creation_id:
        raise ValueError("GitHub tag protection requires two distinct rulesets")
    if immutable_bypasses:
        raise ValueError("GitHub immutable tag ruleset must not have a bypass")
    if immutable_rules != [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "update",
            "parameters": {"update_allows_fetch_and_merge": False},
        },
    ]:
        raise ValueError("GitHub immutable tag ruleset rules are invalid")
    if creation_bypasses != [
        {
            "actor_id": RELEASE_APP_ID,
            "actor_type": "Integration",
            "bypass_mode": "always",
        }
    ]:
        raise ValueError("GitHub tag creation bypass is not limited to the release App")
    if creation_rules != [{"type": "creation"}]:
        raise ValueError("GitHub tag creation ruleset rules are invalid")
    return immutable_id, creation_id


def _decimal_id(value: Any, context: str) -> str:
    if not isinstance(value, str) or DECIMAL_ID.fullmatch(value) is None:
        raise ValueError(f"{context} is invalid")
    return value


def validate_normalized_tag_rulesets(
    value: Any, *, repository: str, repository_id: str
) -> list[dict[str, Any]]:
    """Validate the two normalized rulesets bound by central admission."""

    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("publication staging tag rulesets are invalid")
    expected = (
        {
            "role": "creation_authority",
            "name": CREATION_TAG_RULESET_NAME,
            "bypass_actors": [
                {
                    "actor_id": str(RELEASE_APP_ID),
                    "actor_type": "Integration",
                    "bypass_mode": "always",
                }
            ],
            "rules": [{"type": "creation"}],
        },
        {
            "role": "immutability",
            "name": IMMUTABLE_TAG_RULESET_NAME,
            "bypass_actors": [],
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "update",
                    "parameters": {"update_allows_fetch_and_merge": False},
                },
            ],
        },
    )
    ids: set[str] = set()
    for index, raw_ruleset in enumerate(value):
        ruleset = _closed_object(
            raw_ruleset,
            NORMALIZED_TAG_RULESET_KEYS,
            f"publication staging tag ruleset {index}",
        )
        if (
            ruleset["schema_version"] != TAG_RULESET_SCHEMA
            or ruleset["repository"] != repository
            or ruleset["repository_id"] != repository_id
            or ruleset["target"] != "tag"
            or ruleset["enforcement"] != "active"
            or ruleset["conditions"]
            != {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}}
        ):
            raise ValueError("publication staging tag ruleset identity is invalid")
        ruleset_id = _decimal_id(
            ruleset["ruleset_id"], f"publication staging tag ruleset {index} ID"
        )
        if ruleset_id in ids:
            raise ValueError("publication staging tag ruleset IDs are duplicated")
        ids.add(ruleset_id)
        for key, expected_value in expected[index].items():
            if ruleset[key] != expected_value:
                raise ValueError("publication staging tag ruleset policy is invalid")
    return value


def validate_publication_staging(
    value: Any,
    *,
    artifact_inventory: Any,
    verified_staging_sha256: str,
    expected_tag: str,
    expected_commit: str,
) -> dict[str, Any]:
    """Validate the exact App-authored draft bound by central admission."""

    staging = _closed_object(value, STAGING_KEYS, "publication staging")
    inventory = validate_artifact_inventory(artifact_inventory)
    if staging["schema_version"] != STAGING_SCHEMA:
        raise ValueError("publication staging schema is invalid")
    for key in (
        "repository_id",
        "draft_release_id",
        "release_app_id",
        "release_app_installation_id",
        "release_app_bot_user_id",
    ):
        _decimal_id(staging[key], f"publication staging {key}")
    if (
        staging["repository"] != SOURCE_REPOSITORY
        or staging["repository_id"] != str(SOURCE_REPOSITORY_ID)
        or staging["tag"] != expected_tag
        or not isinstance(expected_commit, str)
        or COMMIT.fullmatch(expected_commit) is None
        or staging["target_commitish"] != expected_commit
        or staging["draft"] is not True
        or staging["prerelease"] is not False
        or staging["release_app_id"] != str(RELEASE_APP_ID)
        or staging["release_app_installation_id"] != str(RELEASE_APP_INSTALLATION_ID)
        or staging["release_app_bot_user_id"] != str(RELEASE_APP_BOT_ID)
        or staging["release_author_login"] != "openadapt-release[bot]"
    ):
        raise ValueError("publication staging identity or state is invalid")

    immutable_releases = validate_immutable_releases(staging["immutable_releases"])
    if staging["immutable_releases_sha256"] != _digest(
        immutable_releases, IMMUTABLE_RELEASES_DOMAIN
    ):
        raise ValueError("publication staging immutable releases digest is invalid")
    tag_ref_state = _closed_object(
        staging["tag_ref_state"], TAG_REF_STATE_KEYS, "publication staging tag ref"
    )
    if tag_ref_state != {"ref": f"refs/tags/{expected_tag}", "exists": False}:
        raise ValueError("publication staging tag ref state is invalid")
    if staging["tag_ref_state_sha256"] != _digest(tag_ref_state, TAG_REF_STATE_DOMAIN):
        raise ValueError("publication staging tag ref state digest is invalid")

    timestamp = staging["observed_at"]
    if not isinstance(timestamp, str) or TIMESTAMP.fullmatch(timestamp) is None:
        raise ValueError("publication staging timestamp is invalid")
    try:
        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("publication staging timestamp is invalid") from exc

    rulesets = validate_normalized_tag_rulesets(
        staging["tag_rulesets"],
        repository=staging["repository"],
        repository_id=staging["repository_id"],
    )
    if staging["tag_rulesets_sha256"] != _digest(rulesets, TAG_RULESETS_DOMAIN):
        raise ValueError("publication staging tag ruleset digest is invalid")

    assets = staging["assets"]
    if not isinstance(assets, list) or len(assets) != 2:
        raise ValueError("publication staging assets are invalid")
    asset_ids: set[str] = set()
    asset_names: set[str] = set()
    projections: list[dict[str, Any]] = []
    for index, raw_asset in enumerate(assets):
        asset = _closed_object(
            raw_asset, STAGED_ASSET_KEYS, f"publication staging asset {index}"
        )
        asset_id = _decimal_id(
            asset["asset_id"], f"publication staging asset {index} ID"
        )
        if (
            asset_id in asset_ids
            or asset["name"] in asset_names
            or asset["uploader_id"] != str(RELEASE_APP_BOT_ID)
            or asset["uploader_login"] != "openadapt-release[bot]"
        ):
            raise ValueError("publication staging asset authority is invalid")
        asset_ids.add(asset_id)
        asset_names.add(asset["name"])
        projections.append({key: asset[key] for key in ARTIFACT_KEYS})
    if assets != sorted(assets, key=lambda item: (item["name"], item["asset_id"])):
        raise ValueError("publication staging assets are not canonically sorted")
    if (
        sorted(
            projections, key=lambda item: (item["kind"], item["name"], item["sha256"])
        )
        != inventory["artifacts"]
    ):
        raise ValueError(
            "publication staging assets differ from the artifact inventory"
        )

    actual_digest = _digest(staging, STAGING_DOMAIN)
    if (
        not isinstance(verified_staging_sha256, str)
        or SHA256.fullmatch(verified_staging_sha256) is None
        or verified_staging_sha256 != actual_digest
    ):
        raise ValueError("verified publication staging digest does not match")
    return staging


def select_tag_ruleset_ids(value: Any) -> tuple[int, int]:
    """Select the two exact active tag-ruleset summaries by stable name."""

    if not isinstance(value, list):
        raise ValueError("GitHub ruleset index must be an array")
    selected: list[int] = []
    for expected_name in (
        IMMUTABLE_TAG_RULESET_NAME,
        CREATION_TAG_RULESET_NAME,
    ):
        matches = [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("name") == expected_name
            and item.get("target") == "tag"
            and item.get("enforcement") == "active"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"GitHub needs one active tag ruleset named {expected_name}"
            )
        ruleset_id = matches[0].get("id")
        if (
            not isinstance(ruleset_id, int)
            or isinstance(ruleset_id, bool)
            or ruleset_id < 1
        ):
            raise ValueError("GitHub tag ruleset ID is invalid")
        selected.append(ruleset_id)
    if selected[0] == selected[1]:
        raise ValueError("GitHub tag protection requires two distinct rulesets")
    return selected[0], selected[1]


def merge_tag_ruleset_page(index_path: Path, page_path: Path) -> int:
    """Append one validated GitHub API page to a complete local ruleset index."""

    if page_path.is_symlink() or not page_path.is_file():
        raise ValueError("GitHub ruleset page path is invalid")
    if index_path.is_symlink():
        raise ValueError("GitHub ruleset index path is invalid")
    if index_path.exists():
        index = _load_json(
            index_path.read_text(encoding="utf-8"), "GitHub ruleset index"
        )
    else:
        index = []
    page = _load_json(page_path.read_text(encoding="utf-8"), "GitHub ruleset page")
    if not isinstance(index, list) or not isinstance(page, list) or len(page) > 100:
        raise ValueError("GitHub ruleset pagination response is invalid")
    known_ids = {
        item.get("id")
        for item in index
        if isinstance(item, dict)
        and isinstance(item.get("id"), int)
        and not isinstance(item.get("id"), bool)
    }
    if len(known_ids) != len(index):
        raise ValueError("GitHub ruleset index contains invalid or duplicate IDs")
    for item in page:
        ruleset = _object(item, "GitHub ruleset summary")
        ruleset_id = ruleset.get("id")
        if (
            not isinstance(ruleset_id, int)
            or isinstance(ruleset_id, bool)
            or ruleset_id < 1
            or ruleset_id in known_ids
        ):
            raise ValueError("GitHub ruleset page contains an invalid or duplicate ID")
        known_ids.add(ruleset_id)
    index_path.write_text(
        canonical_json([*index, *page]).decode("utf-8"), encoding="utf-8"
    )
    return len(page)


def validate_immutable_releases(value: Any) -> dict[str, bool]:
    """Require the exact current GitHub immutable-releases API response."""

    document = _closed_object(
        value, IMMUTABLE_RELEASES_KEYS, "GitHub immutable releases"
    )
    if document["enabled"] is not True or not isinstance(
        document["enforced_by_owner"], bool
    ):
        raise ValueError("GitHub immutable releases are not enabled")
    return document


def _load_json(value: str, context: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{context} is not valid JSON") from exc


def _compact(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--directory", type=Path, required=True)

    digest_parser = subparsers.add_parser("inventory-sha256")
    digest_parser.add_argument("--inventory-json", required=True)

    binding_parser = subparsers.add_parser("tag-binding")
    binding_parser.add_argument("--admission-reference-json", required=True)
    binding_parser.add_argument("--artifact-inventory-json", required=True)
    binding_parser.add_argument("--verified-artifact-inventory-sha256", required=True)

    verify_parser = subparsers.add_parser("verify-tag-binding")
    verify_parser.add_argument("--binding-json", required=True)

    tag_object_parser = subparsers.add_parser("verify-tag-object")
    tag_object_parser.add_argument("--metadata", type=Path, required=True)
    tag_object_parser.add_argument("--tag", required=True)
    tag_object_parser.add_argument("--source-commit", required=True)

    tag_ref_parser = subparsers.add_parser("verify-tag-ref")
    tag_ref_parser.add_argument("--metadata", type=Path, required=True)
    tag_ref_parser.add_argument("--tag", required=True)
    tag_ref_parser.add_argument("--tag-object-sha", required=True)

    ruleset_parser = subparsers.add_parser("verify-tag-rulesets")
    ruleset_parser.add_argument("--immutable-metadata", type=Path, required=True)
    ruleset_parser.add_argument("--creation-metadata", type=Path, required=True)
    ruleset_parser.add_argument("--tag", required=True)

    ruleset_index_parser = subparsers.add_parser("select-tag-rulesets")
    ruleset_index_parser.add_argument("--metadata", type=Path, required=True)

    staging_parser = subparsers.add_parser("verify-publication-staging")
    staging_parser.add_argument("--staging-json", required=True)
    staging_parser.add_argument("--verified-staging-sha256", required=True)
    staging_parser.add_argument("--artifact-inventory-json", required=True)
    staging_parser.add_argument("--tag", required=True)
    staging_parser.add_argument("--source-commit", required=True)

    immutable_releases_parser = subparsers.add_parser("verify-immutable-releases")
    immutable_releases_parser.add_argument("--metadata", type=Path, required=True)

    merge_rulesets_parser = subparsers.add_parser("merge-tag-ruleset-page")
    merge_rulesets_parser.add_argument("--index", type=Path, required=True)
    merge_rulesets_parser.add_argument("--page", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "inventory":
            result: Any = build_artifact_inventory(args.directory)
        elif args.command == "inventory-sha256":
            result = artifact_inventory_sha256(
                _load_json(args.inventory_json, "artifact inventory")
            )
        elif args.command == "tag-binding":
            result = build_tag_binding(
                _load_json(args.admission_reference_json, "admission reference"),
                _load_json(args.artifact_inventory_json, "artifact inventory"),
                args.verified_artifact_inventory_sha256,
            )
        elif args.command == "verify-tag-binding":
            result = validate_tag_binding(
                _load_json(args.binding_json, "release tag binding")
            )
        elif args.command == "verify-tag-object":
            document = _load_json(
                args.metadata.read_text(encoding="utf-8"), "GitHub tag object"
            )
            tag_object_sha, binding = validate_tag_object(
                document,
                expected_tag=args.tag,
                expected_commit=args.source_commit,
            )
            result = {"tag_object_sha": tag_object_sha, "binding": binding}
        elif args.command == "verify-tag-ref":
            document = _load_json(
                args.metadata.read_text(encoding="utf-8"), "GitHub tag ref"
            )
            validate_tag_ref(
                document,
                expected_tag=args.tag,
                expected_tag_object_sha=args.tag_object_sha,
            )
            result = "verified"
        elif args.command == "verify-tag-rulesets":
            immutable_document = _load_json(
                args.immutable_metadata.read_text(encoding="utf-8"),
                "GitHub immutable tag ruleset",
            )
            creation_document = _load_json(
                args.creation_metadata.read_text(encoding="utf-8"),
                "GitHub tag creation ruleset",
            )
            result = ",".join(
                str(value)
                for value in validate_tag_rulesets(
                    immutable_document,
                    creation_document,
                    expected_tag=args.tag,
                )
            )
        elif args.command == "select-tag-rulesets":
            index_document = _load_json(
                args.metadata.read_text(encoding="utf-8"), "GitHub ruleset index"
            )
            result = ",".join(
                str(value) for value in select_tag_ruleset_ids(index_document)
            )
        elif args.command == "verify-publication-staging":
            result = validate_publication_staging(
                _load_json(args.staging_json, "publication staging"),
                artifact_inventory=_load_json(
                    args.artifact_inventory_json, "artifact inventory"
                ),
                verified_staging_sha256=args.verified_staging_sha256,
                expected_tag=args.tag,
                expected_commit=args.source_commit,
            )
        elif args.command == "verify-immutable-releases":
            result = validate_immutable_releases(
                _load_json(
                    args.metadata.read_text(encoding="utf-8"),
                    "GitHub immutable releases",
                )
            )
        else:
            result = str(merge_tag_ruleset_page(args.index, args.page))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")

    if args.command == "tag-binding":
        sys.stdout.buffer.write(tag_binding_bytes(result))
    else:
        print(result if isinstance(result, str) else _compact(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
