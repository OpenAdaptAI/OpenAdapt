#!/usr/bin/env python3
"""Render the README maturity block from the verified public lifecycle record.

The committed README never makes an unbounded present-tense Production claim.
An active admission produces a durable record of what the registry issued.  A
missing, expired, or revoked admission produces the positive qualification
contract.  Current state always comes from the live machine record.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
SOURCE_PATH = ROOT / "production-lifecycle-source.json"
LIVE_RECORD_URL = "https://docs.openadapt.ai/production-lifecycle.json"
SOURCE_SCHEMA = "openadapt.production-readme-source/v1"
PROJECTION_SCHEMA = "openadapt.public-production-lifecycle/v1"
PROJECTION_JSON_SCHEMA = "schemas/production-lifecycle-public.schema.json"
BEGIN = "<!-- BEGIN PRODUCTION LIFECYCLE -->"
END = "<!-- END PRODUCTION LIFECYCLE -->"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SOURCE_BYTES = 4 * 1024 * 1024

EXPECTED_SOURCE_FILES = {
    "projection": "docs/production-lifecycle.json",
    "schema": "docs/schemas/production-lifecycle-public.schema.json",
}
EXPECTED_CANONICAL_FILES = {
    "admissions": "production-lifecycle-admissions.json",
    "admissions_schema": "schemas/production-lifecycle-admissions.schema.json",
    "evidence_manifest_schema": (
        "schemas/production-lifecycle-evidence-manifest.schema.json"
    ),
    "evidence_summary_schema": (
        "schemas/production-lifecycle-evidence-summary.schema.json"
    ),
    "lifecycle": "repository-lifecycle.yml",
    "policy": "production-lifecycle-policy.json",
    "policy_schema": "schemas/production-lifecycle-policy.schema.json",
    "validator": "scripts/validate_production_lifecycle.py",
}
EXPECTED_TARGETS = {
    "agent",
    "capture",
    "cloud",
    "desktop",
    "docs",
    "flow",
    "openadapt",
}
TARGET_KEYS = {
    "id",
    "display_name",
    "lifecycle_scope",
    "lifecycle_subject",
    "source_repository",
    "release_kind",
    "required_claim_scope",
    "required_artifact_kinds",
    "package_index_project",
    "artifact_authority_by_kind",
    "latest_admission",
    "admission_history",
}
ADMISSION_KEYS = {
    "admission_id",
    "target",
    "claim_scope",
    "release_identity",
    "policy_revision",
    "release",
    "acceptance_evidence",
    "issued_at",
    "expires_at",
    "revoked_at",
}


class MaturityError(ValueError):
    """The README maturity source or derived state is not trustworthy."""


def _closed(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise MaturityError(
            f"{label} must contain exactly {sorted(keys)}; got {actual}"
        )
    return value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load_json_bytes(value: bytes, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise MaturityError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise MaturityError(f"{label} must be a JSON object")
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise MaturityError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise MaturityError(f"{label} is not a valid UTC timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise MaturityError(f"{label} must use UTC")
    return parsed.astimezone(timezone.utc)


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "openadapt-readme-maturity-renderer/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_SOURCE_BYTES:
            raise MaturityError("Production lifecycle input exceeds the size limit")
        body = response.read(MAX_SOURCE_BYTES + 1)
    if len(body) > MAX_SOURCE_BYTES:
        raise MaturityError("Production lifecycle input exceeds the size limit")
    return body


def load_source(path: Path = SOURCE_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaturityError(
            f"Production lifecycle source is missing or invalid: {exc}"
        ) from exc
    source = _closed(
        value,
        {"schema_version", "repository", "source_commit", "files"},
        "Production lifecycle source",
    )
    if source["schema_version"] != SOURCE_SCHEMA:
        raise MaturityError("Production lifecycle source schema is not supported")
    if source["repository"] != "OpenAdaptAI/openadapt-ops":
        raise MaturityError("Production lifecycle source repository is not canonical")
    commit = source["source_commit"]
    if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
        raise MaturityError("Production lifecycle source commit is not exact")
    files = source["files"]
    if not isinstance(files, dict) or set(files) != set(EXPECTED_SOURCE_FILES):
        raise MaturityError("Production lifecycle source file inventory is not exact")
    for key, expected_path in EXPECTED_SOURCE_FILES.items():
        item = _closed(files[key], {"path", "url", "sha256"}, f"source file {key}")
        if item["path"] != expected_path:
            raise MaturityError(f"source file {key} path is not canonical")
        if (
            not isinstance(item["sha256"], str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise MaturityError(f"source file {key} digest is invalid")
        parsed = urlsplit(item["url"])
        expected_url_path = f"/OpenAdaptAI/openadapt-ops/{commit}/{expected_path}"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "raw.githubusercontent.com"
            or parsed.path != expected_url_path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise MaturityError(
                f"source file {key} URL is not bound to the exact commit"
            )
    return source


def fetch_source_files(
    source: Mapping[str, Any],
    *,
    fetch: Callable[[str], bytes] = _fetch,
) -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    for key in sorted(EXPECTED_SOURCE_FILES):
        item = source["files"][key]
        try:
            body = fetch(item["url"])
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise MaturityError(
                f"source file {key} could not be fetched: {exc}"
            ) from exc
        if _digest_bytes(body) != item["sha256"]:
            raise MaturityError(f"source file {key} digest changed")
        values[key] = body
    return values


def require_current_source(
    source: Mapping[str, Any],
    *,
    fetch: Callable[[str], bytes] = _fetch,
) -> None:
    """Reject a safe but stale pin when either relevant file changed on main."""

    for key, path in EXPECTED_SOURCE_FILES.items():
        url = f"https://raw.githubusercontent.com/OpenAdaptAI/openadapt-ops/main/{path}"
        try:
            current = fetch(url)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise MaturityError(
                f"current source file {key} could not be fetched: {exc}"
            ) from exc
        if _digest_bytes(current) != source["files"][key]["sha256"]:
            raise MaturityError(
                f"source file {key} changed on openadapt-ops main; repin and rerender"
            )


def _validate_canonical_source(source: object) -> dict[str, Any]:
    canonical = _closed(
        source,
        {"schema_version", "repository", "source_commit", "files"},
        "canonical lifecycle source",
    )
    if canonical["schema_version"] != "openadapt.production-lifecycle-source/v1":
        raise MaturityError("canonical lifecycle source schema is not supported")
    if canonical["repository"] != "OpenAdaptAI/.github":
        raise MaturityError("canonical lifecycle repository differs")
    commit = canonical["source_commit"]
    if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
        raise MaturityError("canonical lifecycle source commit is not exact")
    files = canonical["files"]
    if not isinstance(files, dict) or set(files) != set(EXPECTED_CANONICAL_FILES):
        raise MaturityError("canonical lifecycle file inventory is not exact")
    for key, expected_path in EXPECTED_CANONICAL_FILES.items():
        item = _closed(files[key], {"path", "url", "sha256"}, f"canonical file {key}")
        if item["path"] != expected_path:
            raise MaturityError(f"canonical file {key} path differs")
        if (
            not isinstance(item["sha256"], str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise MaturityError(f"canonical file {key} digest is invalid")
        parsed = urlsplit(item["url"])
        expected_url_path = f"/OpenAdaptAI/.github/{commit}/{expected_path}"
        if (
            parsed.scheme != "https"
            or parsed.netloc != "raw.githubusercontent.com"
            or parsed.path != expected_url_path
            or parsed.query
            or parsed.fragment
        ):
            raise MaturityError(f"canonical file {key} URL is not exact")
    return canonical


def fetch_canonical_files(
    canonical: Mapping[str, Any],
    *,
    fetch: Callable[[str], bytes] = _fetch,
) -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    for key in sorted(EXPECTED_CANONICAL_FILES):
        item = canonical["files"][key]
        try:
            body = fetch(item["url"])
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise MaturityError(
                f"canonical file {key} could not be fetched: {exc}"
            ) from exc
        if _digest_bytes(body) != item["sha256"]:
            raise MaturityError(f"canonical file {key} digest changed")
        values[key] = body
    return values


def _run_canonical_validator(inputs: Mapping[str, bytes]) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="openadapt-readme-lifecycle-") as directory:
        root = Path(directory)
        for key, relative in EXPECTED_CANONICAL_FILES.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(inputs[key])
        validator_path = root / EXPECTED_CANONICAL_FILES["validator"]
        spec = importlib.util.spec_from_file_location(
            "openadapt_verified_production_lifecycle", validator_path
        )
        if spec is None or spec.loader is None:
            raise MaturityError("canonical lifecycle validator cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            active = module.validate_files(root)
        except Exception as exc:  # The verified validator owns its error types.
            raise MaturityError(
                f"canonical lifecycle validator refused: {exc}"
            ) from exc
    if not isinstance(active, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in active.items()
    ):
        raise MaturityError("canonical lifecycle validator returned invalid state")
    return active


def validate_projection(
    value: object,
    canonical_inputs: Mapping[str, bytes],
) -> dict[str, Any]:
    projection = _closed(
        value,
        {
            "$schema",
            "schema_version",
            "source",
            "policy_revision",
            "maximum_admission_days",
            "derivation",
            "targets",
        },
        "public Production lifecycle projection",
    )
    if projection["$schema"] != PROJECTION_JSON_SCHEMA:
        raise MaturityError("public projection JSON schema binding differs")
    if projection["schema_version"] != PROJECTION_SCHEMA:
        raise MaturityError("public projection schema is not supported")
    if projection["derivation"] != {
        "mode": "latest_signed_admission_at_read_time",
        "static_production_state": False,
        "expired_or_revoked_latest_behavior": "no_production",
        "fallback_to_older_release": False,
    }:
        raise MaturityError("public projection derivation contract differs")
    canonical = _validate_canonical_source(projection["source"])
    for key in EXPECTED_CANONICAL_FILES:
        if _digest_bytes(canonical_inputs[key]) != canonical["files"][key]["sha256"]:
            raise MaturityError(f"public projection canonical binding {key} differs")

    policy = _load_json_bytes(canonical_inputs["policy"], "canonical policy")
    admissions = _load_json_bytes(
        canonical_inputs["admissions"], "canonical admissions"
    )
    policy_targets = policy.get("targets")
    admission_records = admissions.get("admissions")
    if not isinstance(policy_targets, list) or not isinstance(admission_records, list):
        raise MaturityError("canonical policy or admission inventory is invalid")
    projected_targets = projection["targets"]
    if not isinstance(projected_targets, list):
        raise MaturityError("public projection targets must be a list")
    by_policy = {
        target.get("id"): target
        for target in policy_targets
        if isinstance(target, dict)
    }
    if set(by_policy) != EXPECTED_TARGETS or len(by_policy) != len(policy_targets):
        raise MaturityError("canonical policy target inventory differs")
    by_projection: dict[str, dict[str, Any]] = {}
    for target in projected_targets:
        item = _closed(target, TARGET_KEYS, "public projection target")
        target_id = item["id"]
        if target_id in by_projection or target_id not in EXPECTED_TARGETS:
            raise MaturityError("public projection target inventory differs")
        by_projection[target_id] = item
    if set(by_projection) != EXPECTED_TARGETS:
        raise MaturityError("public projection target inventory is incomplete")

    for target_id, projected in by_projection.items():
        expected_target = by_policy[target_id]
        for key in TARGET_KEYS - {"latest_admission", "admission_history"}:
            if projected[key] != expected_target[key]:
                raise MaturityError(
                    f"public projection target {target_id} {key} differs"
                )
        history = [
            record
            for record in admission_records
            if isinstance(record, dict) and record.get("target") == target_id
        ]
        history.sort(key=lambda item: item["release_identity"]["sequence"])
        if projected["admission_history"] != history:
            raise MaturityError(f"public projection target {target_id} history differs")
        latest = history[-1] if history else None
        if projected["latest_admission"] != latest:
            raise MaturityError(
                f"public projection target {target_id} latest record differs"
            )
    return projection


def active_admission(
    projection: Mapping[str, Any],
    target_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return only the latest active target admission. Never fall back."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target = next(
        (item for item in projection["targets"] if item["id"] == target_id), None
    )
    if target is None:
        raise MaturityError(f"public projection has no target {target_id!r}")
    latest = target["latest_admission"]
    if latest is None:
        return None
    admission = _closed(latest, ADMISSION_KEYS, f"latest {target_id} admission")
    if admission["target"] != target_id:
        raise MaturityError(f"latest {target_id} admission target differs")
    issued_at = _timestamp(admission["issued_at"], f"{target_id} issued_at")
    expires_at = _timestamp(admission["expires_at"], f"{target_id} expires_at")
    if admission["revoked_at"] is not None:
        _timestamp(admission["revoked_at"], f"{target_id} revoked_at")
        return None
    if not issued_at <= now < expires_at:
        return None
    return admission


def _qualification_block() -> str:
    return "\n".join(
        [
            BEGIN,
            "> **Built for qualified production workflows.** A Production run requires both",
            "> active signed product admissions for the exact OpenAdapt component and",
            "> deployment releases, and an active signed, expiring, revocable workflow",
            "> admission for the exact compiled workflow version. The workflow admission binds",
            "> the organization and workflow identity; bundle version and digest; admitted",
            "> runtime release; application and environment; input, action, identity, effect,",
            "> and policy contracts; evidence authority; and its issue, expiry, and revocation",
            "> state. Qualification requires at least three trials for each task and condition.",
            "> A closed result schema must report silent incorrect success and over-halt. Any",
            "> bound change requires a new qualification.",
            f"> [Check the live signed Production record]({LIVE_RECORD_URL}).",
            END,
        ]
    )


def render_block(
    projection: Mapping[str, Any],
    active: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> str:
    admission = active_admission(projection, "openadapt", now=now)
    active_id = active.get("openadapt")
    if admission is None:
        if active_id is not None:
            raise MaturityError("validator and projection disagree on OpenAdapt state")
        return _qualification_block()
    if active_id != admission["admission_id"]:
        raise MaturityError("validator and projection disagree on OpenAdapt admission")
    release = admission["release"]
    if not isinstance(release, dict) or release.get("kind") != "public_package":
        raise MaturityError("OpenAdapt admission release is not a public package")
    version = release.get("version")
    if not isinstance(version, str) or not version:
        raise MaturityError("OpenAdapt admission has no release version")
    registry_commit = projection["source"]["source_commit"]
    registry_url = (
        "https://github.com/OpenAdaptAI/.github/blob/"
        f"{registry_commit}/production-lifecycle-admissions.json"
    )
    evidence_url = admission["acceptance_evidence"]["summary_url"]
    return "\n".join(
        [
            BEGIN,
            "> **Production admission record.** The verified registry issued admission",
            f"> `{admission['admission_id']}` for OpenAdapt `{version}` at",
            f"> `{admission['issued_at']}`, with declared expiry `{admission['expires_at']}`",
            "> and no revocation in this exact registry snapshot. This is an immutable",
            "> historical record, not a present-tense maturity assertion. Verify current",
            f"> state in the [live record]({LIVE_RECORD_URL}).",
            f"> [Registry snapshot]({registry_url}) · [Acceptance evidence]({evidence_url})",
            END,
        ]
    )


def replace_block(readme: str, block: str) -> str:
    if readme.count(BEGIN) != 1 or readme.count(END) != 1:
        raise MaturityError(
            "README must contain one complete Production lifecycle block"
        )
    before, remainder = readme.split(BEGIN, 1)
    _old, after = remainder.split(END, 1)
    return before + block + after


def validated_block(
    source: Mapping[str, Any],
    *,
    fetch: Callable[[str], bytes] = _fetch,
    now: datetime | None = None,
    check_current: bool = False,
) -> str:
    if check_current:
        require_current_source(source, fetch=fetch)
    source_files = fetch_source_files(source, fetch=fetch)
    schema = _load_json_bytes(source_files["schema"], "public projection schema")
    if schema.get("$id") != (
        "https://docs.openadapt.ai/schemas/production-lifecycle-public.schema.json"
    ):
        raise MaturityError("public projection schema identity differs")
    projection_value = _load_json_bytes(source_files["projection"], "public projection")
    canonical = _validate_canonical_source(projection_value.get("source"))
    canonical_files = fetch_canonical_files(canonical, fetch=fetch)
    projection = validate_projection(projection_value, canonical_files)
    active = _run_canonical_validator(canonical_files)
    return render_block(projection, active, now=now)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Refuse README drift")
    parser.add_argument(
        "--require-current-source",
        action="store_true",
        help="Refuse when the relevant openadapt-ops main bytes changed",
    )
    args = parser.parse_args()
    try:
        source = load_source()
        block = validated_block(
            source,
            check_current=args.require_current_source,
        )
        current = README_PATH.read_text(encoding="utf-8")
        rendered = replace_block(current, block)
        if args.check:
            if rendered != current:
                raise MaturityError("README Production lifecycle block has drifted")
        else:
            temporary = README_PATH.with_suffix(".md.tmp")
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(README_PATH)
    except (OSError, MaturityError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print("Validated the README against the public Production lifecycle record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
