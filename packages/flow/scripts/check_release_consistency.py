#!/usr/bin/env python3
"""Check that release version sources and built distributions agree."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "openadapt-flow"
DIST_NAME = "openadapt_flow"
FORBIDDEN_DISTRIBUTION_PATH_TOKENS = (
    "openimis",
    "agpl",
    "third_party_notices",
)
AGPL_CONTENT_SIGNATURES = (
    b"SPDX-License-Identifier: " + b"AG" + b"PL-",
    b"GNU " + b"AFFERO GENERAL PUBLIC LICENSE",
)
# Private, deployment-derived hardening artifacts -- the GROWN failure corpus
# from real deployments, the TUNED metamorphic-adversary parameters/weights,
# deployment-derived THRESHOLDS, effect-verification oracle RECIPES,
# customer/deployment-derived real-EMR datasets, and raw paid-agent evidence or
# per-system driver/oracle recipes -- live ONLY in the private
# OpenAdaptAI/openadapt-corpus repo and must never ride inside an MIT wheel or
# sdist. Reproducible synthetic fixtures, generic harnesses, fake-patient
# public-demo evidence, and bounded aggregates remain public. This mirrors the
# AGPL boundary above: both a path-token check and a content-signature check, so
# a rename cannot smuggle a private artifact in.
PRIVATE_DISTRIBUTION_PATH_TOKENS = (
    "openadapt-corpus",
    "adversary_corpus",
    "identity_roc",
    "grown_corpus",
    "tuned_adversary",
    "deployment_corpus",
    "deployment_thresholds",
    "effect_oracle_recipe",
    "held_out_corpus",
    "oracle_recipe",
    "pixel_verify_cert",
    "real_emr",
    "enterprise_productionized",
    "control_plane",
    "paid_agent_evidence",
    "agent-arm/",
    "rows.jsonl",
    "cost_ledger",
    "frappe_agent_arm.py",
    "openemr_agent_arm.py",
    "openimis_agent_arm.py",
)
PRIVATE_DISTRIBUTION_PATH_SEGMENTS = frozenset({"private", ".private"})
PRIVATE_DISTRIBUTION_EXACT_PATHS = frozenset(
    {
        "tests/test_identity_corpus_rates.py",
        "tests/test_identity_out_of_corpus.py",
    }
)

# This public-web study is not customer-derived, but the complete target list,
# per-target workflows, raw rows, and generated report are high-leverage
# evaluation DATA rather than the engine mechanism. Keep the generic harness
# and bounded aggregate public while refusing the detailed recipes/results.
REPOSITORY_ONLY_EVALUATION_PATH_TOKENS = ("reliability_corpus",)
REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES = (
    "benchmark/reliability/",
    "scripts/reliability/",
)
REPOSITORY_ONLY_EVALUATION_EXACT_PATHS = frozenset(
    {
        "tests/test_reliability.py",
    }
)

# Current-tree guard. Public source retains mechanisms, interfaces, conservative
# defaults, and bounded aggregate evidence. Raw/grown data, tuning sweeps,
# target recipes, and per-target rows must be absent from the public checkout,
# not merely excluded from package archives.
PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES = (
    "benchmark/reliability/",
    "scripts/reliability/",
)
PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS = frozenset(
    {
        "benchmark/reliability/RELIABILITY.md".lower(),
        "benchmark/reliability/summary.json",
    }
)
PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS = frozenset(
    {
        "benchmark/reliability/corpus.json",
        "benchmark/reliability/results.json",
        "tests/test_reliability.py",
    }
)
PUBLIC_SOURCE_ROOT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
PUBLIC_SOURCE_ANYWHERE_IGNORED_DIRECTORIES = frozenset({"__pycache__"})
# Assembled from parts so this guard (which ships in the sdist) does not itself
# trip the content scan; every private-corpus artifact carries the full banner.
PRIVATE_CORPUS_CONTENT_SIGNATURES = (b"OPENADAPT-CORPUS" + b"-PRIVATE-DO-NOT-PACKAGE",)

# Positive inventory for files that can carry data, evidence, static payloads,
# models, or deployment-shaped configuration.  Ordinary Python/Markdown/TeX
# source remains reviewable source code and is not exhaustively enumerated.
# Every file with one of these suffixes must instead appear, with its exact
# SHA-256, in PUBLIC_ARTIFACT_INVENTORY_PATH.  Updating that reviewed manifest is
# an explicit command; validation never rewrites it.
PUBLIC_ARTIFACT_INVENTORY_PATH = "public-artifacts.json"
WHEEL_ARTIFACT_INVENTORY_PATH = f"{DIST_NAME}/{PUBLIC_ARTIFACT_INVENTORY_PATH}"
PUBLIC_ARTIFACT_SUFFIXES = frozenset(
    {
        ".7z",
        ".arrow",
        ".bin",
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".db",
        ".gif",
        ".gz",
        ".html",
        ".ini",
        ".joblib",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".jsonl",
        ".mjs",
        ".npy",
        ".npz",
        ".onnx",
        ".parquet",
        ".pickle",
        ".pkl",
        ".png",
        ".pt",
        ".pth",
        ".safetensors",
        ".sqlite",
        ".svg",
        ".tar",
        ".toml",
        ".tsv",
        ".wasm",
        ".webp",
        ".yaml",
        ".yml",
        ".zip",
    }
)
# Semantic Release intentionally stamps pyproject.toml immediately before the
# build.  It is the one artifact-like project file whose hash cannot be frozen
# in the reviewed inventory.  It is still constrained by version/metadata and
# exact-head release checks elsewhere in this module.
PUBLIC_ARTIFACT_INVENTORY_EXEMPT_PATHS = frozenset(
    {
        PUBLIC_ARTIFACT_INVENTORY_PATH,
        "pyproject.toml",
    }
)
PUBLIC_SOURCE_FORBIDDEN_CATEGORIES = frozenset(
    {
        "control_plane",
        "deployment_thresholds",
        "enterprise_productionized",
        "grown_corpus",
        "oracle_recipes",
        "real_emr_datasets",
        "tuned_adversary_params",
    }
)

LENDING_PUBLIC_EVIDENCE_PATH = "benchmark/lending_fault_model/swer_results.json"
LENDING_PUBLIC_EVIDENCE_ARMS = (
    "screen_only",
    "effect_verify_single",
    "effect_verify_full",
)
LENDING_PUBLIC_EVIDENCE_META = {
    "schema_version": 1,
    "evidence_scope": "bounded_aggregate",
    "synthetic": True,
    "domain": "lending (MockLoan) - loan disbursement authorization",
    "oracle": (
        "benchmark-local read-only SQLite ground truth with independent "
        "row and open-world canonical table-content classification"
    ),
    "judge_read_path": (
        "direct read-only SQLite capture over sqlite_master-discovered business tables"
    ),
    "single_surface_read_path": "/api/disbursements",
    "full_read_path": "/api/db",
    "ground_truth": "mockloan.fault_server isolated temporary SQLite ledger",
    "arms": list(LENDING_PUBLIC_EVIDENCE_ARMS),
    "tasks": 12,
    "trials_per_task_per_arm": 3,
    "deterministic": True,
    "model_calls": 0,
}
LENDING_PUBLIC_EVIDENCE_ARM_KEYS = frozenset(
    {
        "arm",
        "arms",
        "n_episodes",
        "n_tasks",
        "swer",
        "swer_wrong_write",
        "swer_phantom",
        "over_halt",
        "task_success",
        "screen_success",
        "success_effect_gap",
        "total_cost_usd",
        "mean_cost_usd",
        "cells",
        "outcome_counts",
    }
)
LENDING_PUBLIC_EVIDENCE_CELL_KEYS = frozenset(
    {
        "category",
        "substrate",
        "n",
        "swer",
        "swer_wrong_write",
        "swer_phantom",
        "over_halt",
        "task_success",
        "screen_success",
        "success_effect_gap",
    }
)
LENDING_PUBLIC_EVIDENCE_RATE_KEYS = frozenset({"numerator", "denominator", "rate"})
LENDING_PUBLIC_EVIDENCE_RATE_FIELDS = (
    "swer",
    "swer_wrong_write",
    "swer_phantom",
    "over_halt",
    "task_success",
    "screen_success",
)
LENDING_PUBLIC_EVIDENCE_CATEGORIES = frozenset(
    {
        "C1_partial_save",
        "C2_duplicate_submission",
        "C3_optimistic_then_reject",
        "C4_stale_overwrite",
        "C5_double_delivered_input",
        "C6_wrong_record_homonym",
        "C7_silent_noop_wrong_target",
        "control",
    }
)
LENDING_PUBLIC_EVIDENCE_OUTCOMES = frozenset(
    {
        "false_abort",
        "over_halt",
        "safe_halt",
        "silent_wrong_effect",
        "success",
        "wrong_action",
    }
)


def _canonical_source_path(path: str, *, source: str) -> str:
    """Return a canonical relative POSIX path or fail closed."""
    if not path or "\\" in path:
        raise ValueError(f"{source} contains a non-canonical path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"{source} contains a non-canonical path: {path!r}")
    return pure.as_posix()


def _has_private_path_segment(path: str) -> bool:
    return any(
        part.lower() in PRIVATE_DISTRIBUTION_PATH_SEGMENTS
        for part in PurePosixPath(path).parts
    )


def _private_distribution_hits(members: set[str], signature_hits: set[str]) -> set[str]:
    """Members that cross the private source-availability boundary."""
    hits = {
        member
        for member in members
        if member.lower() in PRIVATE_DISTRIBUTION_EXACT_PATHS
        or _has_private_path_segment(member)
        or any(token in member.lower() for token in PRIVATE_DISTRIBUTION_PATH_TOKENS)
    }
    hits.update(signature_hits)
    return hits


def _repository_only_evaluation_hits(members: set[str]) -> set[str]:
    """Members that are public-source evaluation data, not package runtime."""
    return {
        member
        for member in members
        if member.lower() in REPOSITORY_ONLY_EVALUATION_EXACT_PATHS
        or any(
            member.lower().startswith(prefix)
            for prefix in REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES
        )
        or any(
            token in member.lower() for token in REPOSITORY_ONLY_EVALUATION_PATH_TOKENS
        )
    }


def _artifact_inventory_candidate(path: str) -> bool:
    if path in PUBLIC_ARTIFACT_INVENTORY_EXEMPT_PATHS:
        return False
    return PurePosixPath(path).suffix.lower() in PUBLIC_ARTIFACT_SUFFIXES


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_public_source_files(root: Path) -> dict[str, Path]:
    """Return regular source-tree files while rejecting symlinks/special files.

    Build/cache directories are ignored only at repository root.  A nested
    ``openadapt_flow/dist`` or ``openadapt_flow/build`` is product source and
    must be inspected rather than disappearing behind a basename filter.
    """
    files: dict[str, Path] = {}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        retained: list[str] = []
        for name in directories:
            candidate = directory_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise ValueError(
                    f"public source tree contains a symlink directory: {relative}"
                )
            if (
                relative_directory == Path(".")
                and name in PUBLIC_SOURCE_ROOT_IGNORED_DIRECTORIES
            ) or name in PUBLIC_SOURCE_ANYWHERE_IGNORED_DIRECTORIES:
                continue
            retained.append(name)
        directories[:] = retained
        for filename in filenames:
            candidate = directory_path / filename
            relative = candidate.relative_to(root).as_posix()
            mode = candidate.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(
                    f"public source tree contains a symlink/special file: {relative}"
                )
            files[relative] = candidate
    return files


def build_public_artifact_inventory(root: Path = ROOT) -> dict[str, object]:
    """Build the deterministic inventory document for explicit human review."""
    files = _walk_public_source_files(root)
    artifacts = [
        {"path": path, "sha256": _sha256_file(files[path])}
        for path in sorted(files)
        if _artifact_inventory_candidate(path)
    ]
    return {
        "schema_version": 1,
        "policy": _public_artifact_policy(),
        "artifacts": artifacts,
    }


def write_public_artifact_inventory(root: Path = ROOT) -> Path:
    """Explicitly regenerate the reviewed inventory; never called by validation."""
    path = root / PUBLIC_ARTIFACT_INVENTORY_PATH
    path.write_text(
        json.dumps(build_public_artifact_inventory(root), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _public_artifact_policy() -> dict[str, object]:
    return {
        "artifact_suffixes": sorted(PUBLIC_ARTIFACT_SUFFIXES),
        "forbidden_categories": sorted(PUBLIC_SOURCE_FORBIDDEN_CATEGORIES),
        "note": (
            "Reviewed positive inventory of public data, evidence, static, "
            "model, and configuration assets. Validation never regenerates it."
        ),
    }


def _parse_public_artifact_inventory(payload: bytes, *, source: str) -> dict[str, str]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not parse reviewed public artifact inventory in {source}: {error}"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "policy",
        "artifacts",
    }:
        raise ValueError("public artifact inventory has an unexpected top-level schema")
    if document["schema_version"] != 1:
        raise ValueError("public artifact inventory schema_version must be 1")
    if document["policy"] != _public_artifact_policy():
        raise ValueError(
            "public artifact inventory policy does not match the validator's "
            "reviewed suffix/category contract"
        )
    rows = document["artifacts"]
    if not isinstance(rows, list):
        raise ValueError("public artifact inventory artifacts must be a list")
    inventory: dict[str, str] = {}
    previous = ""
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError("public artifact inventory row has an unexpected schema")
        if not isinstance(row["path"], str) or not isinstance(row["sha256"], str):
            raise ValueError(
                "public artifact inventory path and SHA-256 must be strings"
            )
        relative = _canonical_source_path(row["path"], source="inventory")
        digest = row["sha256"]
        if relative <= previous:
            raise ValueError(
                "public artifact inventory paths must be unique and sorted"
            )
        if not _artifact_inventory_candidate(relative):
            raise ValueError(
                f"public artifact inventory contains a non-artifact path: {relative}"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(
                f"public artifact inventory has an invalid SHA-256 for {relative}"
            )
        inventory[relative] = digest
        previous = relative
    return inventory


def _load_public_artifact_inventory(root: Path = ROOT) -> dict[str, str]:
    path = root / PUBLIC_ARTIFACT_INVENTORY_PATH
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"could not read reviewed public artifact inventory at {path}: {error}"
        ) from error
    return _parse_public_artifact_inventory(payload, source=str(path))


def _validate_public_artifact_inventory(
    files: dict[str, Path], *, root: Path = ROOT
) -> dict[str, str]:
    inventory = _load_public_artifact_inventory(root)
    observed = {path for path in files if _artifact_inventory_candidate(path)}
    expected = set(inventory)
    if observed != expected:
        raise ValueError(
            "public artifact inventory does not match source tree; explicitly "
            "regenerate and review it: "
            f"unregistered={sorted(observed - expected)}, "
            f"missing={sorted(expected - observed)}"
        )
    changed = [
        path
        for path in sorted(expected)
        if _sha256_file(files[path]) != inventory[path]
    ]
    if changed:
        raise ValueError(
            "public artifact inventory hash mismatch; explicitly regenerate and "
            f"review it: {changed}"
        )
    return inventory


def _bounded_lending_error(message: str) -> ValueError:
    return ValueError(f"bounded public lending evidence {message}")


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_lending_rate(
    value: object, *, path: str, denominator: int
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != LENDING_PUBLIC_EVIDENCE_RATE_KEYS:
        raise _bounded_lending_error(f"has an unexpected rate schema at {path}")
    numerator = value["numerator"]
    observed_denominator = value["denominator"]
    rate = value["rate"]
    if (
        not isinstance(numerator, int)
        or isinstance(numerator, bool)
        or not isinstance(observed_denominator, int)
        or isinstance(observed_denominator, bool)
        or observed_denominator != denominator
        or not 0 <= numerator <= denominator
        or not _finite_number(rate)
    ):
        raise _bounded_lending_error(f"has invalid counts or rate at {path}")
    expected_rate = numerator / denominator
    if not math.isclose(float(rate), expected_rate, rel_tol=0.0, abs_tol=1e-12):
        raise _bounded_lending_error(f"has a rate/count mismatch at {path}")
    return value


def _validate_bounded_lending_evidence(files: dict[str, Path]) -> None:
    """Recursively enforce the deterministic, aggregate-only lending schema."""
    path = files.get(LENDING_PUBLIC_EVIDENCE_PATH)
    if path is None:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _bounded_lending_error(
            f"could not be parsed at {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise _bounded_lending_error("must be a JSON object")
    if set(payload) != {"meta", *LENDING_PUBLIC_EVIDENCE_ARMS}:
        raise _bounded_lending_error(
            "has unexpected top-level keys; raw rows and scenario recipes "
            "must remain private/in-memory"
        )
    if payload.get("meta") != LENDING_PUBLIC_EVIDENCE_META:
        raise _bounded_lending_error("has invalid or non-canonical metadata")

    task_count = cast(int, LENDING_PUBLIC_EVIDENCE_META["tasks"])
    trial_count = cast(int, LENDING_PUBLIC_EVIDENCE_META["trials_per_task_per_arm"])
    expected_episodes = task_count * trial_count
    for arm in LENDING_PUBLIC_EVIDENCE_ARMS:
        summary = payload.get(arm)
        arm_path = arm
        if (
            not isinstance(summary, dict)
            or set(summary) != LENDING_PUBLIC_EVIDENCE_ARM_KEYS
        ):
            raise _bounded_lending_error(f"has an unexpected {arm} schema")
        if (
            summary["arm"] != arm
            or summary["arms"] != [arm]
            or summary["n_tasks"] != task_count
            or summary["n_episodes"] != expected_episodes
        ):
            raise _bounded_lending_error(f"has inconsistent arm counts at {arm_path}")

        rates = {
            field: _validate_lending_rate(
                summary[field],
                path=f"{arm_path}.{field}",
                denominator=expected_episodes,
            )
            for field in LENDING_PUBLIC_EVIDENCE_RATE_FIELDS
        }
        if (
            cast(int, rates["swer_wrong_write"]["numerator"])
            + cast(int, rates["swer_phantom"]["numerator"])
            != rates["swer"]["numerator"]
        ):
            raise _bounded_lending_error(
                f"has inconsistent SWER variants at {arm_path}"
            )

        gap = summary["success_effect_gap"]
        if not _finite_number(gap) or not -1.0 <= float(gap) <= 1.0:
            raise _bounded_lending_error(
                f"has invalid success/effect gap at {arm_path}"
            )
        expected_gap = float(cast(float, rates["screen_success"]["rate"])) - float(
            cast(float, rates["task_success"]["rate"])
        )
        if not math.isclose(float(gap), expected_gap, rel_tol=0.0, abs_tol=1e-12):
            raise _bounded_lending_error(
                f"has inconsistent success/effect gap at {arm_path}"
            )
        for cost_field in ("total_cost_usd", "mean_cost_usd"):
            if summary[cost_field] != 0.0:
                raise _bounded_lending_error(
                    f"must report zero model cost at {arm_path}.{cost_field}"
                )
        outcome_counts = summary["outcome_counts"]
        if (
            not isinstance(outcome_counts, dict)
            or not outcome_counts
            or not set(outcome_counts) <= LENDING_PUBLIC_EVIDENCE_OUTCOMES
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in outcome_counts.values()
            )
            or sum(outcome_counts.values()) != expected_episodes
        ):
            raise _bounded_lending_error(f"has invalid outcome counts at {arm_path}")
        if rates["swer"]["numerator"] != outcome_counts.get("silent_wrong_effect", 0):
            raise _bounded_lending_error(
                f"has inconsistent SWER outcomes at {arm_path}"
            )
        if rates["over_halt"]["numerator"] != outcome_counts.get("over_halt", 0):
            raise _bounded_lending_error(
                f"has inconsistent over-halt outcomes at {arm_path}"
            )
        if rates["task_success"]["numerator"] != outcome_counts.get("success", 0):
            raise _bounded_lending_error(
                f"has inconsistent task-success outcomes at {arm_path}"
            )
        if rates["screen_success"]["numerator"] != (
            outcome_counts.get("success", 0)
            + outcome_counts.get("silent_wrong_effect", 0)
        ):
            raise _bounded_lending_error(
                f"has inconsistent screen-success outcomes at {arm_path}"
            )

        cells = summary["cells"]
        if not isinstance(cells, list) or len(cells) != len(
            LENDING_PUBLIC_EVIDENCE_CATEGORIES
        ):
            raise _bounded_lending_error(
                f"must retain one bounded category cell at {arm_path}"
            )
        categories: set[str] = set()
        cell_episodes = 0
        for index, cell in enumerate(cells):
            cell_path = f"{arm_path}.cells[{index}]"
            if (
                not isinstance(cell, dict)
                or set(cell) != LENDING_PUBLIC_EVIDENCE_CELL_KEYS
            ):
                raise _bounded_lending_error(
                    f"has an unexpected cell schema at {cell_path}"
                )
            category = cell["category"]
            n = cell["n"]
            if (
                category not in LENDING_PUBLIC_EVIDENCE_CATEGORIES
                or category in categories
                or cell["substrate"] != "web"
                or not isinstance(n, int)
                or isinstance(n, bool)
                or n <= 0
            ):
                raise _bounded_lending_error(
                    f"has invalid cell identity/count at {cell_path}"
                )
            categories.add(category)
            cell_episodes += n
            cell_rates = {
                field: _validate_lending_rate(
                    cell[field], path=f"{cell_path}.{field}", denominator=n
                )
                for field in LENDING_PUBLIC_EVIDENCE_RATE_FIELDS
            }
            cell_gap = cell["success_effect_gap"]
            expected_cell_gap = float(
                cast(float, cell_rates["screen_success"]["rate"])
            ) - float(cast(float, cell_rates["task_success"]["rate"]))
            if not _finite_number(cell_gap) or not math.isclose(
                float(cell_gap), expected_cell_gap, rel_tol=0.0, abs_tol=1e-12
            ):
                raise _bounded_lending_error(f"has an invalid cell gap at {cell_path}")
        if (
            categories != LENDING_PUBLIC_EVIDENCE_CATEGORIES
            or cell_episodes != expected_episodes
        ):
            raise _bounded_lending_error(
                f"has incomplete category coverage at {arm_path}"
            )


def _wheel_member_source_path(member: str) -> str:
    """Map a wheel member back to the reviewed source-tree artifact path."""
    schema_prefix = f"{DIST_NAME}/schemas/"
    if member.startswith(schema_prefix):
        return f"schemas/{member.removeprefix(schema_prefix)}"
    if member == WHEEL_ARTIFACT_INVENTORY_PATH:
        return PUBLIC_ARTIFACT_INVENTORY_PATH
    return member


def _validate_archive_artifact_inventory(
    payloads: dict[str, bytes],
    *,
    source: str,
    wheel: bool,
) -> None:
    """Bind artifact-like archive members to exact reviewed source bytes."""
    manifest_member = (
        WHEEL_ARTIFACT_INVENTORY_PATH if wheel else PUBLIC_ARTIFACT_INVENTORY_PATH
    )
    manifest_payload = payloads.get(manifest_member)
    if manifest_payload is None:
        raise ValueError(f"{source} is missing its embedded public artifact inventory")
    inventory = _parse_public_artifact_inventory(
        manifest_payload,
        source=f"{source}:{manifest_member}",
    )
    unregistered: list[str] = []
    changed: list[str] = []
    for member, payload in sorted(payloads.items()):
        relative = _wheel_member_source_path(member) if wheel else member
        if relative == PUBLIC_ARTIFACT_INVENTORY_PATH:
            continue
        if relative in PUBLIC_ARTIFACT_INVENTORY_EXEMPT_PATHS:
            continue
        if not _artifact_inventory_candidate(relative):
            continue
        expected_hash = inventory.get(relative)
        if expected_hash is None:
            unregistered.append(member)
        elif _sha256_bytes(payload) != expected_hash:
            changed.append(member)
    if unregistered or changed:
        raise ValueError(
            f"{source} artifact inventory/provenance mismatch: "
            f"unregistered={unregistered}, changed={changed}"
        )


def validate_public_source_tree(root: Path = ROOT) -> None:
    """Fail if private data/recipes/tuning re-enter the public checkout."""
    files = _walk_public_source_files(root)
    members = set(files)
    _validate_public_artifact_inventory(files, root=root)
    _validate_bounded_lending_evidence(files)

    private_signature_hits = {
        member
        for member, path in files.items()
        if any(
            signature in path.read_bytes()
            for signature in PRIVATE_CORPUS_CONTENT_SIGNATURES
        )
    }

    private = _private_distribution_hits(members, private_signature_hits)
    repository_only = {
        member
        for member in members
        if member.lower() in PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS
        or any(
            member.lower().startswith(prefix)
            and member.lower() not in PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS
            for prefix in PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES
        )
        or any(
            token in member.lower() for token in REPOSITORY_ONLY_EVALUATION_PATH_TOKENS
        )
    }
    forbidden = private | repository_only
    if forbidden:
        raise ValueError(
            "public source tree contains private data, recipes, tuning, or raw "
            f"evaluation artifacts: {sorted(forbidden)}"
        )


LOCK_PACKAGE_PATTERN = re.compile(
    r'(?P<prefix>\[\[package\]\]\nname = "openadapt-flow"\nversion = ")'
    r'[^\"]+(?P<suffix>"\nsource = \{ editable = "\." \})',
    flags=re.MULTILINE,
)
REQUIRED_SDIST_PATHS = frozenset(
    {
        "LICENSE",
        PUBLIC_ARTIFACT_INVENTORY_PATH,
    }
)
FORBIDDEN_SDIST_PATHS = frozenset(
    {
        "THIRD_PARTY_NOTICES.md",
        "scripts/openimis_claims_demo.py",
        "tests/test_openimis_claims_fixture.py",
    }
)
FORBIDDEN_SDIST_PREFIXES = (
    "benchmark/openimis_claims/",
    "benchmark/frappe_lending/agent-arm/",
    "benchmark/openemr_local/agent-arm/",
)

FORBIDDEN_PUBLIC_SOURCE_PATHS = frozenset(
    {
        "scripts/frappe_agent_arm.py",
        "scripts/openemr_agent_arm.py",
        "scripts/openimis_agent_arm.py",
        "benchmark/agent_arm_verticals/COST_LEDGER.md",
    }
)


def validate_public_source_policy(root: Path = ROOT) -> None:
    """Refuse private paid-agent evidence and per-system recipes in source.

    Archive inspection remains mandatory, but this earlier guard prevents a
    sensitive path from being committed to the public repository even when a
    packaging exclusion would otherwise hide it from the wheel or sdist.
    """
    hits = {
        relative
        for relative in FORBIDDEN_PUBLIC_SOURCE_PATHS
        if (root / relative).is_file()
    }
    hits.update(
        path.relative_to(root).as_posix()
        for path in root.rglob("rows.jsonl")
        if ".git" not in path.parts
    )
    hits.update(
        path.relative_to(root).as_posix()
        for path in root.glob("benchmark/**/agent-arm/**/*")
        if path.is_file()
    )
    if hits:
        raise ValueError(
            "public source contains private paid-agent per-run evidence, "
            "environment-linked result rows, detailed cost ledgers, or "
            "per-system driver recipes that belong only in the private "
            f"OpenAdaptAI/openadapt-corpus repo: {sorted(hits)}"
        )


def _expected_license_bytes(license_file: Path | None = None) -> bytes:
    path = license_file or (ROOT / "LICENSE")
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"could not read expected MIT license at {path}: {error}"
        ) from error
    if b"MIT License" not in value or b"Permission is hereby granted" not in value:
        raise ValueError(f"expected license is not the reviewed MIT license: {path}")
    return value


def _validate_package_metadata(
    raw: bytes,
    *,
    source: str,
    expected_version: str | None = None,
) -> str:
    metadata = BytesParser(policy=default).parsebytes(raw)
    if metadata.get("Name") != PACKAGE_NAME:
        raise ValueError(f"{source} package name is not {PACKAGE_NAME!r}")
    version = metadata.get("Version")
    if not version:
        raise ValueError(f"{source} is missing Version metadata")
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"{source} version {version!r} does not match {expected_version!r}"
        )
    if metadata.get("License") != "MIT":
        raise ValueError(f"{source} does not declare License: MIT")
    license_files = metadata.get_all("License-File", [])
    if license_files != ["LICENSE"]:
        raise ValueError(
            f"{source} must declare exactly License-File: LICENSE; "
            f"found {license_files}"
        )
    return version


def _archive_parts(name: str, *, source: str) -> tuple[str, ...]:
    """Return canonical POSIX archive parts or reject an ambiguous path."""
    if not name or "\\" in name:
        raise ValueError(f"{source} contains a non-canonical member path: {name!r}")
    normalized = name[:-1] if name.endswith("/") else name
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{source} contains a non-canonical member path: {name!r}")
    return parts


def _wheel_members(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, zipfile.ZipInfo], set[str]]:
    by_name: dict[str, zipfile.ZipInfo] = {}
    for member in archive.infolist():
        parts = _archive_parts(member.filename, source="wheel")
        normalized = "/".join(parts)
        if normalized in by_name:
            raise ValueError(f"wheel contains duplicate member: {normalized!r}")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise ValueError(f"wheel contains a symlink member: {normalized!r}")
        by_name[normalized] = member
    return by_name, set(by_name)


def _match(pattern: str, text: str, source: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"could not read version from {source}")
    return match.group(1)


def release_versions(root: Path = ROOT) -> dict[str, str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (root / "openadapt_flow/__init__.py").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")

    return {
        "pyproject.toml": _match(
            r'\[project\]\s+name = "openadapt-flow"\s+version = "([^"]+)"',
            pyproject,
            "pyproject.toml",
        ),
        "openadapt_flow/__init__.py": _match(
            r'^__version__ = "([^"]+)"$',
            package_init,
            "openadapt_flow/__init__.py",
        ),
        "uv.lock": _match(
            r'\[\[package\]\]\s+name = "openadapt-flow"\s+version = "([^"]+)"'
            r'\s+source = \{ editable = "\." \}',
            lock,
            "uv.lock",
        ),
    }


def sync_lock_version(root: Path = ROOT) -> str:
    """Stamp only the editable root version, preserving reviewed resolution."""
    versions = release_versions(root)
    source_versions = {
        versions["pyproject.toml"],
        versions["openadapt_flow/__init__.py"],
    }
    if len(source_versions) != 1:
        raise ValueError(f"release source versions differ: {versions}")
    version = source_versions.pop()

    lock_path = root / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    updated, replacements = LOCK_PACKAGE_PATTERN.subn(
        rf"\g<prefix>{version}\g<suffix>", lock
    )
    if replacements != 1:
        raise ValueError(
            f"expected exactly one editable {PACKAGE_NAME} package in uv.lock, "
            f"found {replacements}"
        )
    lock_path.write_text(updated, encoding="utf-8")

    synchronized = release_versions(root)
    if len(set(synchronized.values())) != 1:
        raise ValueError(f"release versions differ after lock sync: {synchronized}")
    return version


def validate_sdist_license_boundary(
    sdist: Path,
    *,
    expected_license: bytes | None = None,
    expected_version: str | None = None,
) -> str:
    """Require an MIT-only sdist with no openIMIS benchmark material."""
    expected_root = sdist.name.removesuffix(".tar.gz")
    members: set[str] = set()
    archived_license: bytes | None = None
    archived_metadata: bytes | None = None
    signature_hits: set[str] = set()
    private_signature_hits: set[str] = set()
    payloads: dict[str, bytes] = {}
    with tarfile.open(sdist, mode="r:gz") as archive:
        for member in archive:
            parts = _archive_parts(member.name, source="source distribution")
            if parts[0] != expected_root:
                raise ValueError(
                    "source distribution contains a member outside its single "
                    f"{expected_root!r} root: {member.name!r}"
                )
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError(
                        "source distribution root entry must be a directory: "
                        f"{member.name!r}"
                    )
                continue
            if not (member.isfile() or member.isdir()):
                raise ValueError(
                    "source distribution contains a link/device/special member: "
                    f"{member.name!r}"
                )
            relative = "/".join(parts[1:])
            if relative in members:
                raise ValueError(
                    f"source distribution contains duplicate member: {relative!r}"
                )
            members.add(relative)
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(
                    f"source distribution member could not be read: {relative!r}"
                )
            payload = extracted.read()
            payloads[relative] = payload
            if relative == "LICENSE":
                archived_license = payload
            elif relative == "PKG-INFO":
                archived_metadata = payload
            if any(signature in payload for signature in AGPL_CONTENT_SIGNATURES):
                signature_hits.add(relative)
            if any(
                signature in payload for signature in PRIVATE_CORPUS_CONTENT_SIGNATURES
            ):
                private_signature_hits.add(relative)
    private = _private_distribution_hits(members, private_signature_hits)
    if private:
        raise ValueError(
            "source distribution contains private source-policy "
            "material (grown corpus / tuned adversary / thresholds / oracle "
            "recipes / real-EMR datasets / paid-agent raw evidence) that "
            "belongs only in the private "
            f"OpenAdaptAI/openadapt-corpus repo: {sorted(private)}"
        )
    repository_only = _repository_only_evaluation_hits(members)
    if repository_only:
        raise ValueError(
            "source distribution contains repository-only evaluation data or "
            "recipes that are not part of the distributable engine: "
            f"{sorted(repository_only)}"
        )
    missing = REQUIRED_SDIST_PATHS - members
    if missing:
        raise ValueError(
            f"source distribution is missing required files: {sorted(missing)}"
        )
    reviewed_license = expected_license or _expected_license_bytes()
    if archived_license != reviewed_license:
        raise ValueError(
            "source distribution LICENSE does not match the reviewed root MIT LICENSE"
        )
    if archived_metadata is None:
        raise ValueError("source distribution is missing root PKG-INFO")
    version = _validate_package_metadata(
        archived_metadata,
        source="source distribution PKG-INFO",
        expected_version=expected_version,
    )
    forbidden = set(FORBIDDEN_SDIST_PATHS & members)
    forbidden.update(
        member
        for member in members
        if any(member.startswith(prefix) for prefix in FORBIDDEN_SDIST_PREFIXES)
        or any(token in member.lower() for token in FORBIDDEN_DISTRIBUTION_PATH_TOKENS)
    )
    forbidden.update(signature_hits)
    if forbidden:
        raise ValueError(
            "source distribution contains repository-only openIMIS benchmark "
            f"material outside the MIT package boundary: {sorted(forbidden)}"
        )
    _validate_archive_artifact_inventory(
        payloads,
        source="source distribution",
        wheel=False,
    )
    return version


def validate_wheel_license_boundary(
    wheel: Path,
    *,
    expected_license: bytes | None = None,
    expected_version: str | None = None,
) -> str:
    """Require the MIT license and no openIMIS material in the wheel."""
    with zipfile.ZipFile(wheel) as archive:
        member_info, members = _wheel_members(archive)
        license_members = sorted(
            member
            for member in members
            if member.endswith(".dist-info/licenses/LICENSE")
        )
        metadata_members = sorted(
            member for member in members if member.endswith(".dist-info/METADATA")
        )
        archived_license: bytes | None = None
        archived_metadata: bytes | None = None
        signature_hits: set[str] = set()
        private_signature_hits: set[str] = set()
        payloads: dict[str, bytes] = {}
        for name, info in member_info.items():
            if info.is_dir():
                continue
            payload = archive.read(info)
            payloads[name] = payload
            if name in license_members:
                archived_license = payload
            elif name in metadata_members:
                archived_metadata = payload
            if any(signature in payload for signature in AGPL_CONTENT_SIGNATURES):
                signature_hits.add(name)
            if any(
                signature in payload for signature in PRIVATE_CORPUS_CONTENT_SIGNATURES
            ):
                private_signature_hits.add(name)
    if not license_members:
        raise ValueError("wheel is missing the MIT LICENSE")
    if len(license_members) != 1:
        raise ValueError(f"wheel contains multiple LICENSE files: {license_members}")
    reviewed_license = expected_license or _expected_license_bytes()
    if archived_license != reviewed_license:
        raise ValueError("wheel LICENSE does not match the reviewed root MIT LICENSE")
    if len(metadata_members) != 1 or archived_metadata is None:
        raise ValueError(
            f"wheel must contain exactly one METADATA file: {metadata_members}"
        )
    version = _validate_package_metadata(
        archived_metadata,
        source="wheel METADATA",
        expected_version=expected_version,
    )
    private = _private_distribution_hits(members, private_signature_hits)
    if private:
        raise ValueError(
            "wheel contains private source-policy material "
            "(grown corpus / tuned adversary / thresholds / oracle recipes / "
            "real-EMR datasets / paid-agent raw evidence) that belongs only "
            "in the private "
            f"OpenAdaptAI/openadapt-corpus repo: {sorted(private)}"
        )
    repository_only = _repository_only_evaluation_hits(members)
    if repository_only:
        raise ValueError(
            "wheel contains repository-only evaluation data or recipes that "
            "are not part of the distributable engine: "
            f"{sorted(repository_only)}"
        )
    forbidden = {
        member
        for member in members
        if any(token in member.lower() for token in FORBIDDEN_DISTRIBUTION_PATH_TOKENS)
    }
    forbidden.update(signature_hits)
    if forbidden:
        raise ValueError(
            "wheel contains openIMIS/AGPL material outside the MIT package "
            f"boundary: {sorted(forbidden)}"
        )
    _validate_archive_artifact_inventory(payloads, source="wheel", wheel=True)
    return version


def validate_distribution_directory(
    dist_dir: Path,
    *,
    version: str | None = None,
    license_file: Path | None = None,
) -> tuple[Path, Path]:
    """Validate exactly the two files the publisher is allowed to upload."""
    if not dist_dir.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist_dir}")
    files = sorted(path for path in dist_dir.iterdir() if path.is_file())
    # `uv build` creates this sentinel in custom output directories and the
    # repository keeps the same sentinel in `dist/`; PyPI publishers ignore it.
    artifacts = [path for path in files if path.name != ".gitignore"]
    wheels = [path for path in artifacts if path.name.endswith(".whl")]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            "distribution directory must contain exactly one wheel and one sdist; "
            f"found {[path.name for path in artifacts]}"
        )
    wheel, sdist = wheels[0], sdists[0]
    reviewed_license = _expected_license_bytes(license_file)
    wheel_version = validate_wheel_license_boundary(
        wheel,
        expected_license=reviewed_license,
        expected_version=version,
    )
    sdist_version = validate_sdist_license_boundary(
        sdist,
        expected_license=reviewed_license,
        expected_version=version,
    )
    if wheel_version != sdist_version:
        raise ValueError(
            f"wheel/sdist metadata versions differ: {wheel_version}, {sdist_version}"
        )
    with zipfile.ZipFile(wheel) as wheel_archive:
        wheel_inventory = wheel_archive.read(WHEEL_ARTIFACT_INVENTORY_PATH)
    expected_sdist_root = sdist.name.removesuffix(".tar.gz")
    with tarfile.open(sdist, mode="r:gz") as sdist_archive:
        sdist_member = f"{expected_sdist_root}/{PUBLIC_ARTIFACT_INVENTORY_PATH}"
        extracted = sdist_archive.extractfile(sdist_member)
        if extracted is None:  # already rejected by the individual validator
            raise ValueError("source distribution inventory could not be read")
        sdist_inventory = extracted.read()
    if wheel_inventory != sdist_inventory:
        raise ValueError(
            "wheel and source distribution embed different public artifact inventories"
        )
    effective_version = version or wheel_version
    expected_prefix = f"{DIST_NAME}-{effective_version}"
    if not wheel.name.startswith(f"{expected_prefix}-") or sdist.name != (
        f"{expected_prefix}.tar.gz"
    ):
        raise ValueError(
            f"distribution filenames do not match version {effective_version}: "
            f"{wheel.name}, {sdist.name}"
        )
    return wheel, sdist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--require-dist", action="store_true")
    parser.add_argument(
        "--write-public-artifact-inventory",
        action="store_true",
        help=(
            "explicitly regenerate the reviewed public artifact inventory; "
            "inspect and commit the resulting diff before release"
        ),
    )
    parser.add_argument(
        "--validate-dist-dir",
        type=Path,
        help="validate an external build directory without trusting its source scripts",
    )
    parser.add_argument(
        "--license-file",
        type=Path,
        help="reviewed root MIT LICENSE for --validate-dist-dir",
    )
    args = parser.parse_args()

    if args.write_public_artifact_inventory:
        if (
            args.sync
            or args.require_dist
            or args.validate_dist_dir
            or args.license_file
        ):
            parser.error(
                "--write-public-artifact-inventory cannot be combined with "
                "release synchronization or distribution validation"
            )
        path = write_public_artifact_inventory()
        print(f"Wrote reviewed public artifact inventory candidate: {path}")
        return 0

    if args.validate_dist_dir is not None:
        if args.sync or args.require_dist:
            parser.error(
                "--validate-dist-dir cannot be combined with --sync/--require-dist"
            )
        try:
            wheel, sdist = validate_distribution_directory(
                args.validate_dist_dir,
                license_file=args.license_file,
            )
        except ValueError as error:
            parser.error(str(error))
        print(f"Distribution license boundary passed: {wheel.name}, {sdist.name}")
        return 0
    if args.license_file is not None:
        parser.error("--license-file requires --validate-dist-dir")

    try:
        validate_public_source_policy()
    except ValueError as error:
        parser.error(str(error))

    if args.sync:
        sync_lock_version()

    try:
        validate_public_source_tree()
    except ValueError as error:
        parser.error(str(error))

    versions = release_versions()
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        parser.error(f"release versions differ: {versions}")
    version = unique_versions.pop()

    if args.require_dist:
        try:
            validate_distribution_directory(ROOT / "dist", version=version)
        except ValueError as error:
            parser.error(str(error))

    print(
        f"Release version {version} is synchronized across project, module, and lock."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
