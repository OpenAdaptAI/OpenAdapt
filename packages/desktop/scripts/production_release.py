#!/usr/bin/env python3
"""Derive the Desktop Production channel from the canonical release admission.

The OpenAdaptAI/.github lifecycle ledger is the only Production authority.
This module writes an append-only, attested cache for one active Desktop
admission.  It never creates, signs, or changes an admission.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from scripts.native_release import (
        ENGINE_RELEASE_PROVENANCE,
        VERIFIED_RELEASE_INDEX,
        validate_engine_release_provenance,
        validate_verified_release_index,
    )
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root.
    from native_release import (  # type: ignore[no-redef]
        ENGINE_RELEASE_PROVENANCE,
        VERIFIED_RELEASE_INDEX,
        validate_engine_release_provenance,
        validate_verified_release_index,
    )

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REPOSITORY = "OpenAdaptAI/.github"
DESKTOP_REPOSITORY = "OpenAdaptAI/openadapt-desktop"
DESKTOP_TARGET = "desktop"
STATE_SCHEMA = "openadapt.desktop-production-admission-state/v1"
CHANNEL_SCHEMA = "openadapt.desktop-production-channel-cache/v1"
CHANNEL_TAG = "desktop-production-channel"
CHANNEL_PREFIX = "openadapt-desktop-production-channel-"
PROMOTION_WORKFLOW = ".github/workflows/production-channel.yml"
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _bytes_digest(payload)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _load_canonical_validator(root: Path) -> ModuleType:
    path = root / "scripts" / "validate_production_lifecycle.py"
    if not path.is_file() or path.is_symlink():
        raise ValueError("the canonical Production lifecycle validator is absent")
    module_name = (
        "openadapt_canonical_production_lifecycle_"
        + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("the canonical Production lifecycle validator cannot load")
    module = importlib.util.module_from_spec(spec)
    scripts_path = str(path.parent.resolve())
    added_scripts_path = scripts_path not in sys.path
    sibling_names = {item.stem for item in path.parent.glob("*.py") if item != path}
    saved_siblings = {name: sys.modules.pop(name) for name in sibling_names if name in sys.modules}
    if added_scripts_path:
        sys.path.insert(0, scripts_path)
    try:
        spec.loader.exec_module(module)
    finally:
        if added_scripts_path:
            sys.path.remove(scripts_path)
        for name in sibling_names:
            sys.modules.pop(name, None)
        sys.modules.update(saved_siblings)
    return module


def build_admission_state(
    lifecycle_root: Path,
    *,
    central_source_commit: str,
    validate_files: Callable[[Path], Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Run the canonical validator and select its active Desktop admission."""

    if COMMIT.fullmatch(central_source_commit) is None:
        raise ValueError("the canonical source commit must be a 40-character commit")
    required = {
        "policy": lifecycle_root / "production-lifecycle-policy.json",
        "admissions": lifecycle_root / "production-lifecycle-admissions.json",
        "lifecycle": lifecycle_root / "repository-lifecycle.yml",
        "validator": lifecycle_root / "scripts" / "validate_production_lifecycle.py",
    }
    for label, path in required.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"the canonical {label} file is absent")
    if validate_files is None:
        validator = _load_canonical_validator(lifecycle_root)
        validate_files = validator.validate_files
    active = validate_files(lifecycle_root)
    if not isinstance(active, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in active.items()
    ):
        raise ValueError("the canonical validator returned an invalid active map")

    admissions_document = _load_json(required["admissions"], "canonical admissions")
    admissions = admissions_document.get("admissions")
    if not isinstance(admissions, list):
        raise ValueError("the canonical admissions document has no admission list")
    admission_id = active.get(DESKTOP_TARGET)
    selected: dict[str, Any] | None = None
    if admission_id is not None:
        matches = [
            item
            for item in admissions
            if isinstance(item, dict) and item.get("admission_id") == admission_id
        ]
        if len(matches) != 1 or matches[0].get("target") != DESKTOP_TARGET:
            raise ValueError("the active Desktop admission is not unique and exact")
        selected = matches[0]

    return {
        "schema": STATE_SCHEMA,
        "canonical_repository": CANONICAL_REPOSITORY,
        "canonical_source_commit": central_source_commit,
        "policy_sha256": _bytes_digest(required["policy"].read_bytes()),
        "admissions_sha256": _bytes_digest(required["admissions"].read_bytes()),
        "lifecycle_sha256": _bytes_digest(required["lifecycle"].read_bytes()),
        "validator_sha256": _bytes_digest(required["validator"].read_bytes()),
        "active_admission": selected,
        "active_admission_sha256": (_canonical_digest(selected) if selected is not None else None),
    }


def validate_admission_state(path: Path) -> dict[str, Any]:
    state = _load_json(path, "Desktop admission state")
    expected = {
        "schema",
        "canonical_repository",
        "canonical_source_commit",
        "policy_sha256",
        "admissions_sha256",
        "lifecycle_sha256",
        "validator_sha256",
        "active_admission",
        "active_admission_sha256",
    }
    if set(state) != expected or state.get("schema") != STATE_SCHEMA:
        raise ValueError("the Desktop admission state does not use the closed v1 schema")
    if state.get("canonical_repository") != CANONICAL_REPOSITORY:
        raise ValueError("the Desktop admission state has the wrong authority")
    if COMMIT.fullmatch(str(state.get("canonical_source_commit") or "")) is None:
        raise ValueError("the Desktop admission state source commit is invalid")
    for field in (
        "policy_sha256",
        "admissions_sha256",
        "lifecycle_sha256",
        "validator_sha256",
    ):
        if DIGEST.fullmatch(str(state.get(field) or "")) is None:
            raise ValueError(f"the Desktop admission state {field} is invalid")
    admission = state.get("active_admission")
    if admission is not None:
        if (
            not isinstance(admission, dict)
            or admission.get("target") != DESKTOP_TARGET
            or not isinstance(admission.get("admission_id"), str)
            or not admission["admission_id"]
        ):
            raise ValueError("the Desktop admission state has an invalid admission")
        if state.get("active_admission_sha256") != _canonical_digest(admission):
            raise ValueError("the Desktop admission state digest differs")
    elif state.get("active_admission_sha256") is not None:
        raise ValueError("the Desktop admission state has a digest without an admission")
    return state


def write_admission_state(path: Path, state: Mapping[str, Any]) -> Path:
    if path.exists():
        raise ValueError("the Desktop admission state output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _installer_kind(name: str) -> str | None:
    if name.endswith(".dmg"):
        return "macos-installer"
    if name.endswith(".msi") or name.endswith("-nsis-setup.exe"):
        return "windows-installer"
    if name.endswith(".deb") or name.endswith(".AppImage"):
        return "linux-installer"
    return None


def _candidate_identity(
    state: Mapping[str, Any],
    *,
    index_path: Path,
    engine_provenance_path: Path,
    engine_release_path: Path,
    engine_directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    admission = state.get("active_admission")
    if not isinstance(admission, dict):
        raise ValueError("there is no active Desktop Production admission")
    release = admission.get("release")
    if not isinstance(release, dict) or release.get("kind") != "public_package":
        raise ValueError("the Desktop admission does not name a public package")

    index = validate_verified_release_index(index_path)
    if index.get("repository") != DESKTOP_REPOSITORY:
        raise ValueError("the verified Desktop index belongs to another repository")
    version = str(release.get("version") or "")
    expected_release = {
        "version": index["native_version"],
        "tag": index["engine_tag"],
        "source_commit": index["native_source_commit"],
    }
    for field, expected in expected_release.items():
        if release.get(field) != expected:
            raise ValueError(
                f"the active Desktop admission {field} differs from the verified candidate"
            )
    if VERSION.fullmatch(version) is None:
        raise ValueError("the active Desktop admission version is invalid")

    engine_provenance = validate_engine_release_provenance(
        engine_provenance_path,
        repository=DESKTOP_REPOSITORY,
        engine_tag=index["engine_tag"],
        engine_commit=index["engine_commit"],
        release_path=engine_release_path,
        directory=engine_directory,
    )
    if engine_provenance.get("engine_release_id") != index["engine_release_id"]:
        raise ValueError("the engine receipt and verified candidate release differ")

    expected_artifacts: dict[str, tuple[str, str, str]] = {}
    for item in engine_provenance["assets"]:
        name = item["name"]
        kind = "wheel" if name.endswith(".whl") else "sdist"
        expected_artifacts[name] = (kind, "pypi", "sha256:" + item["sha256"])
    for item in index["assets"]:
        name = item["name"]
        kind = _installer_kind(name)
        if kind is not None:
            expected_artifacts[name] = (
                kind,
                "github_release",
                "sha256:" + item["sha256"],
            )
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("the active Desktop admission has no artifact inventory")
    observed: dict[str, tuple[str, str, str]] = {}
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("the active Desktop admission has an invalid artifact")
        name = item["name"]
        if name in observed:
            raise ValueError("the active Desktop admission repeats an artifact")
        observed[name] = (
            str(item.get("kind") or ""),
            str(item.get("authority") or ""),
            str(item.get("sha256") or ""),
        )
    if observed != expected_artifacts:
        raise ValueError(
            "the active Desktop admission artifact inventory differs from the exact "
            "verified Python and installer artifacts"
        )
    return admission, index, engine_provenance


def production_channel_asset_name(admission: Mapping[str, Any], central_source_commit: str) -> str:
    if COMMIT.fullmatch(central_source_commit) is None:
        raise ValueError("the canonical source commit is invalid")
    digest = _canonical_digest(admission).removeprefix("sha256:")
    return f"{CHANNEL_PREFIX}{central_source_commit}-{digest}.json"


def write_production_channel(
    output_directory: Path,
    *,
    state_path: Path,
    index_path: Path,
    engine_provenance_path: Path,
    engine_release_path: Path,
    engine_directory: Path,
    repository: str,
    workflow_ref: str,
    workflow_commit: str,
    run_id: int,
    run_attempt: int,
) -> Path:
    state = validate_admission_state(state_path)
    admission, index, _engine = _candidate_identity(
        state,
        index_path=index_path,
        engine_provenance_path=engine_provenance_path,
        engine_release_path=engine_release_path,
        engine_directory=engine_directory,
    )
    if repository != DESKTOP_REPOSITORY:
        raise ValueError("the Production channel repository is not Desktop")
    expected_ref = f"{repository}/{PROMOTION_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_ref:
        raise ValueError("the Production channel workflow ref is not protected main")
    if COMMIT.fullmatch(workflow_commit) is None:
        raise ValueError("the Production channel workflow commit is invalid")
    if run_id <= 0 or run_attempt <= 0:
        raise ValueError("the Production channel run identity is invalid")

    admission_digest = _canonical_digest(admission)
    engine_base = f"https://github.com/{repository}/releases/download/{index['engine_tag']}"
    payload = {
        "schema": CHANNEL_SCHEMA,
        "cache_role": "derived-only",
        "repository": repository,
        "channel": "production",
        "canonical_authority": {
            "repository": state["canonical_repository"],
            "source_commit": state["canonical_source_commit"],
            "policy_sha256": state["policy_sha256"],
            "admissions_sha256": state["admissions_sha256"],
            "lifecycle_sha256": state["lifecycle_sha256"],
            "validator_sha256": state["validator_sha256"],
        },
        "admission": {
            "admission_id": admission["admission_id"],
            "sha256": admission_digest,
            "expires_at": admission["expires_at"],
        },
        "release": admission["release"],
        "verified_candidate": {
            "native_tag": index["native_tag"],
            "native_source_commit": index["native_source_commit"],
            "engine_tag": index["engine_tag"],
            "engine_commit": index["engine_commit"],
            "engine_release_id": index["engine_release_id"],
            "verified_index": {
                "name": VERIFIED_RELEASE_INDEX,
                "sha256": _bytes_digest(index_path.read_bytes()),
                "url": f"{engine_base}/{VERIFIED_RELEASE_INDEX}",
            },
            "engine_provenance": {
                "name": ENGINE_RELEASE_PROVENANCE,
                "sha256": _bytes_digest(engine_provenance_path.read_bytes()),
                "url": f"{engine_base}/{ENGINE_RELEASE_PROVENANCE}",
            },
        },
        "derivation": {
            "workflow_path": PROMOTION_WORKFLOW,
            "workflow_ref": workflow_ref,
            "workflow_commit": workflow_commit,
            "event": "workflow_dispatch",
            "run_id": run_id,
            "run_attempt": run_attempt,
            "runner_environment": "github-hosted",
        },
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / production_channel_asset_name(
        admission, state["canonical_source_commit"]
    )
    if output.exists():
        raise ValueError("the derived Production channel cache already exists")
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def validate_production_channel(path: Path) -> dict[str, Any]:
    channel = _load_json(path, "Desktop Production channel cache")
    expected = {
        "schema",
        "cache_role",
        "repository",
        "channel",
        "canonical_authority",
        "admission",
        "release",
        "verified_candidate",
        "derivation",
    }
    if set(channel) != expected or channel.get("schema") != CHANNEL_SCHEMA:
        raise ValueError("the Desktop Production channel cache is not closed v1")
    if (
        channel.get("cache_role") != "derived-only"
        or channel.get("repository") != DESKTOP_REPOSITORY
        or channel.get("channel") != "production"
    ):
        raise ValueError("the Desktop Production channel cache identity is invalid")
    authority = channel.get("canonical_authority")
    if not isinstance(authority, dict) or set(authority) != {
        "repository",
        "source_commit",
        "policy_sha256",
        "admissions_sha256",
        "lifecycle_sha256",
        "validator_sha256",
    }:
        raise ValueError("the Desktop Production channel authority is invalid")
    if (
        authority.get("repository") != CANONICAL_REPOSITORY
        or COMMIT.fullmatch(str(authority.get("source_commit") or "")) is None
    ):
        raise ValueError("the Desktop Production channel authority differs")
    for field in (
        "policy_sha256",
        "admissions_sha256",
        "lifecycle_sha256",
        "validator_sha256",
    ):
        if DIGEST.fullmatch(str(authority.get(field) or "")) is None:
            raise ValueError("the Desktop Production channel authority digest is invalid")
    admission = channel.get("admission")
    if (
        not isinstance(admission, dict)
        or set(admission)
        != {
            "admission_id",
            "sha256",
            "expires_at",
        }
        or DIGEST.fullmatch(str(admission.get("sha256") or "")) is None
    ):
        raise ValueError("the Desktop Production channel admission binding is invalid")
    expected_name = (
        CHANNEL_PREFIX
        + authority["source_commit"]
        + "-"
        + admission["sha256"].removeprefix("sha256:")
        + ".json"
    )
    if path.name != expected_name:
        raise ValueError("the Desktop Production channel cache filename differs")
    candidate = channel.get("verified_candidate")
    if not isinstance(candidate, dict) or set(candidate) != {
        "native_tag",
        "native_source_commit",
        "engine_tag",
        "engine_commit",
        "engine_release_id",
        "verified_index",
        "engine_provenance",
    }:
        raise ValueError("the Desktop Production channel candidate binding is invalid")
    for field in ("native_source_commit", "engine_commit"):
        if COMMIT.fullmatch(str(candidate.get(field) or "")) is None:
            raise ValueError("the Desktop Production channel candidate commit is invalid")
    if (
        not isinstance(candidate.get("engine_release_id"), int)
        or candidate["engine_release_id"] <= 0
    ):
        raise ValueError("the Desktop Production channel release id is invalid")
    for field, name in (
        ("verified_index", VERIFIED_RELEASE_INDEX),
        ("engine_provenance", ENGINE_RELEASE_PROVENANCE),
    ):
        binding = candidate.get(field)
        if (
            not isinstance(binding, dict)
            or set(binding) != {"name", "sha256", "url"}
            or binding.get("name") != name
            or DIGEST.fullmatch(str(binding.get("sha256") or "")) is None
            or not str(binding.get("url") or "").startswith("https://github.com/")
        ):
            raise ValueError("the Desktop Production channel artifact binding is invalid")
    derivation = channel.get("derivation")
    expected_derivation = {
        "workflow_path": PROMOTION_WORKFLOW,
        "workflow_ref": f"{DESKTOP_REPOSITORY}/{PROMOTION_WORKFLOW}@refs/heads/main",
        "event": "workflow_dispatch",
        "runner_environment": "github-hosted",
    }
    if not isinstance(derivation, dict) or set(derivation) != {
        *expected_derivation,
        "workflow_commit",
        "run_id",
        "run_attempt",
    }:
        raise ValueError("the Desktop Production channel derivation is invalid")
    for field, expected_value in expected_derivation.items():
        if derivation.get(field) != expected_value:
            raise ValueError("the Desktop Production channel derivation differs")
    if COMMIT.fullmatch(str(derivation.get("workflow_commit") or "")) is None:
        raise ValueError("the Desktop Production channel workflow commit is invalid")
    for field in ("run_id", "run_attempt"):
        if not isinstance(derivation.get(field), int) or derivation[field] <= 0:
            raise ValueError("the Desktop Production channel run identity is invalid")
    return channel


def verify_production_channel(
    channel_path: Path,
    *,
    state_path: Path,
    index_path: Path,
    engine_provenance_path: Path,
    engine_release_path: Path,
    engine_directory: Path,
) -> dict[str, Any]:
    state = validate_admission_state(state_path)
    admission, index, _engine = _candidate_identity(
        state,
        index_path=index_path,
        engine_provenance_path=engine_provenance_path,
        engine_release_path=engine_release_path,
        engine_directory=engine_directory,
    )
    channel = validate_production_channel(channel_path)
    expected_authority = {
        "repository": state["canonical_repository"],
        "source_commit": state["canonical_source_commit"],
        "policy_sha256": state["policy_sha256"],
        "admissions_sha256": state["admissions_sha256"],
        "lifecycle_sha256": state["lifecycle_sha256"],
        "validator_sha256": state["validator_sha256"],
    }
    if channel["canonical_authority"] != expected_authority:
        raise ValueError("the Desktop Production channel differs from canonical main")
    expected_admission = {
        "admission_id": admission["admission_id"],
        "sha256": _canonical_digest(admission),
        "expires_at": admission["expires_at"],
    }
    if channel["admission"] != expected_admission:
        raise ValueError("the Desktop Production channel differs from the active admission")
    if channel["release"] != admission["release"]:
        raise ValueError("the Desktop Production channel release differs from the admission")
    expected_candidate = {
        "native_tag": index["native_tag"],
        "native_source_commit": index["native_source_commit"],
        "engine_tag": index["engine_tag"],
        "engine_commit": index["engine_commit"],
        "engine_release_id": index["engine_release_id"],
        "verified_index": {
            "name": VERIFIED_RELEASE_INDEX,
            "sha256": _bytes_digest(index_path.read_bytes()),
            "url": (
                f"https://github.com/{DESKTOP_REPOSITORY}/releases/download/"
                f"{index['engine_tag']}/{VERIFIED_RELEASE_INDEX}"
            ),
        },
        "engine_provenance": {
            "name": ENGINE_RELEASE_PROVENANCE,
            "sha256": _bytes_digest(engine_provenance_path.read_bytes()),
            "url": (
                f"https://github.com/{DESKTOP_REPOSITORY}/releases/download/"
                f"{index['engine_tag']}/{ENGINE_RELEASE_PROVENANCE}"
            ),
        },
    }
    if channel["verified_candidate"] != expected_candidate:
        raise ValueError("the Desktop Production channel candidate differs")
    return channel


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    state = commands.add_parser("state")
    state.add_argument("--lifecycle-root", type=Path, required=True)
    state.add_argument("--central-source-commit", required=True)
    state.add_argument("--output", type=Path, required=True)
    for command in (commands.add_parser("write-channel"), commands.add_parser("verify-channel")):
        command.add_argument("--state", type=Path, required=True)
        command.add_argument("--index", type=Path, required=True)
        command.add_argument("--engine-provenance", type=Path, required=True)
        command.add_argument("--engine-release", type=Path, required=True)
        command.add_argument("--engine-directory", type=Path, required=True)
    write = commands.choices["write-channel"]
    write.add_argument("--output-directory", type=Path, required=True)
    write.add_argument("--repository", required=True)
    write.add_argument("--workflow-ref", required=True)
    write.add_argument("--workflow-commit", required=True)
    write.add_argument("--run-id", type=int, required=True)
    write.add_argument("--run-attempt", type=int, required=True)
    commands.choices["verify-channel"].add_argument("--file", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "state":
            state = build_admission_state(
                args.lifecycle_root,
                central_source_commit=args.central_source_commit,
            )
            write_admission_state(args.output, state)
            admission = state["active_admission"]
            if admission is None:
                print("Validated canonical lifecycle state: no active Desktop admission.")
            else:
                print(f"Validated canonical lifecycle state for {admission['admission_id']}.")
        elif args.command == "write-channel":
            output = write_production_channel(
                args.output_directory,
                state_path=args.state,
                index_path=args.index,
                engine_provenance_path=args.engine_provenance,
                engine_release_path=args.engine_release,
                engine_directory=args.engine_directory,
                repository=args.repository,
                workflow_ref=args.workflow_ref,
                workflow_commit=args.workflow_commit,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
            print(output)
        else:
            channel = verify_production_channel(
                args.file,
                state_path=args.state,
                index_path=args.index,
                engine_provenance_path=args.engine_provenance,
                engine_release_path=args.engine_release,
                engine_directory=args.engine_directory,
            )
            print(
                "Validated the derived Desktop Production channel cache for "
                f"{channel['admission']['admission_id']}."
            )
    except (OSError, ValueError, AttributeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
