#!/usr/bin/env python3
"""Fail loudly when published evidence is pinned to a superseded engine release.

Public credibility rests on measured evidence, and every evidence set here is
bound to one exact ``openadapt-flow`` wheel.  When Flow ships a new release the
published numbers silently become claims about an engine nobody is running any
more -- the failure this check exists to catch.  It went unnoticed once already:
the comparison in ``docs/eval_results/current_flow_v1_16_1_local_20260718`` was
still the published result eight minor releases after it was measured.

What is checked, against ``docs/eval_results/PUBLISHED_EVIDENCE.json``:

1. Exactly one evidence set is marked ``current``.
2. Its directory exists and carries a ``results.json`` whose recorded Flow
   version and wheel digest match the manifest (the manifest cannot drift from
   the artifact it describes).
3. Its pinned version equals the newest non-yanked release on PyPI.
4. Its pinned wheel digest equals that release's published wheel digest.
5. Every ``superseded`` set names the set that replaced it.
6. Its verifier, result, replication, public-report, task/oracle, campaign,
   browser, and installed-dependency inventories are complete and exact.
7. A campaign cannot declare production acceptance through a class, boolean,
   or summary count.  The checker accepts that state only from the independent
   certificate/admission/campaign importer.
8. A production import is digest-bound, GitHub-attestation verified, signed by
   an externally trusted qualification authority, contains at least three
   retained trials per qualification condition, verifies every normalized
   evidence receipt, and derives the failure taxonomy from those trial rows.

A release that ships before the comparison can be re-run may be recorded on the
current entry as ``accepted_drift`` (``version``, ``reason``, ``expires``).  It
waives check 3 for that one release only, until its expiry date, and it may be
set at most 31 days ahead.  A later release, an expired waiver, or a malformed
one is drift again.  A waived run says so; it never reports the evidence as
bound to the current release.

Checks 1, 2 and 5-8 are offline and always run.  Checks 3 and 4 need PyPI: pass
``--current-version``/``--current-wheel-sha256`` to supply the release
out-of-band, or ``--offline`` to skip them.  A network failure is reported and
skipped rather than failing the build -- it is not evidence of drift.  Actual
drift always exits non-zero.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import stat
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "docs" / "eval_results" / "PUBLISHED_EVIDENCE.json"
PYPI_URL = "https://pypi.org/pypi/{package}/json"
PRODUCTION_IMPORTER = Path(__file__).resolve().with_name("import_production_acceptance.py")
PRODUCTION_SPEC = importlib.util.spec_from_file_location(
    "_production_acceptance_importer",
    PRODUCTION_IMPORTER,
)
assert PRODUCTION_SPEC is not None and PRODUCTION_SPEC.loader is not None
PRODUCTION = importlib.util.module_from_spec(PRODUCTION_SPEC)
PRODUCTION_SPEC.loader.exec_module(PRODUCTION)


class DriftError(Exception):
    """Published evidence no longer matches the current published release."""


# A waiver longer than this stops being a stopgap and becomes a false green.
MAX_ACCEPTED_DRIFT_DAYS = 31
ACCEPTED_DRIFT_KEYS = frozenset({"version", "reason", "expires"})


def _today() -> date:
    return datetime.now(timezone.utc).date()


def check_accepted_drift(entry: dict[str, Any], today: date | None = None) -> list[str]:
    """An accepted-drift waiver must be short, named, and complete."""

    waiver = entry.get("accepted_drift")
    if waiver is None:
        return []
    prefix = f"{entry.get('path')}: accepted_drift "
    if not isinstance(waiver, dict):
        return [prefix + "must be an object with version, reason and expires"]
    problems: list[str] = []
    extra = sorted(set(waiver) - ACCEPTED_DRIFT_KEYS)
    if extra:
        problems.append(prefix + f"has unexpected keys {extra}")
    version = waiver.get("version")
    if not isinstance(version, str) or not version:
        problems.append(prefix + "must name the exact release version it accepts")
    elif version == entry.get("flow_version"):
        problems.append(prefix + f"names {version}, which the evidence already pins")
    reason = waiver.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        problems.append(prefix + "must give a reason")
    try:
        expires = date.fromisoformat(str(waiver.get("expires")))
    except ValueError:
        problems.append(prefix + "expires must be an ISO date (YYYY-MM-DD)")
    else:
        today = today or _today()
        if (expires - today).days > MAX_ACCEPTED_DRIFT_DAYS:
            problems.append(
                prefix
                + f"expires {expires.isoformat()}; a waiver may run at most "
                + f"{MAX_ACCEPTED_DRIFT_DAYS} days ahead"
            )
    return problems


def accepted_drift_in_force(
    entry: dict[str, Any], release_version: str, today: date | None = None
) -> dict[str, Any] | None:
    """Return the waiver if it validly covers this exact release today."""

    today = today or _today()
    waiver = entry.get("accepted_drift")
    if waiver is None or check_accepted_drift(entry, today):
        return None
    if waiver["version"] != release_version:
        return None
    if today > date.fromisoformat(waiver["expires"]):
        return None
    return waiver


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the single evidence set that must track the current release."""

    entries = manifest.get("evidence", [])
    current = [entry for entry in entries if entry.get("status") == "current"]
    if len(current) != 1:
        raise DriftError(
            f"expected exactly one evidence set with status 'current', found "
            f"{len(current)}: {[entry.get('path') for entry in current]}"
        )
    return current[0]


def check_superseded_links(manifest: dict[str, Any]) -> list[str]:
    """Every superseded set must name its replacement, which must exist."""

    paths = {entry.get("path") for entry in manifest.get("evidence", [])}
    problems: list[str] = []
    for entry in manifest.get("evidence", []):
        if entry.get("status") != "superseded":
            continue
        replacement = entry.get("superseded_by")
        if not replacement:
            problems.append(f"{entry.get('path')}: superseded but no superseded_by")
        elif replacement not in paths:
            problems.append(
                f"{entry.get('path')}: superseded_by {replacement!r} is not in the manifest"
            )
    return problems


def check_entry_matches_artifact(entry: dict[str, Any], repo_root: Path) -> list[str]:
    """The manifest must agree with the results.json it describes."""

    problems: list[str] = []
    directory = repo_root / entry["path"]
    if not directory.is_dir():
        return [f"{entry['path']}: evidence directory is missing"]
    results = directory / "results.json"
    if not results.is_file():
        return [f"{entry['path']}: results.json is missing"]
    document = json.loads(results.read_text(encoding="utf-8"))
    flow = document.get("source", {}).get("flow", {})
    recorded_version = flow.get("version")
    recorded_sha = flow.get("artifact", {}).get("sha256")
    if recorded_version != entry.get("flow_version"):
        problems.append(
            f"{entry['path']}: manifest pins flow_version "
            f"{entry.get('flow_version')!r} but results.json recorded "
            f"{recorded_version!r}"
        )
    if recorded_sha != entry.get("wheel_sha256"):
        problems.append(
            f"{entry['path']}: manifest pins wheel_sha256 "
            f"{entry.get('wheel_sha256')!r} but results.json recorded "
            f"{recorded_sha!r}"
        )
    return problems


def _safe_file(repo_root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str):
        return None
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    try:
        resolved_root = repo_root.resolve(strict=True)
    except OSError:
        return None
    component = repo_root
    for part in path.parts:
        component = component / part
        if component.is_symlink():
            return None
    candidate = repo_root / path
    try:
        candidate_stat = candidate.lstat()
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(candidate_stat.st_mode):
        return None
    return candidate


def _relative_file(repo_root: Path, relative: object) -> tuple[Path | None, str | None]:
    """Return a safe file and its normalized repository-relative name."""

    path = _safe_file(repo_root, relative)
    if path is None:
        return None, None
    return path, path.relative_to(repo_root).as_posix()


def _production_import_file(
    value: object,
    label: str,
    repo_root: Path,
) -> tuple[Path | None, str | None, list[str]]:
    """Validate one digest-bound file link in a production import."""

    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        return None, None, [f"production acceptance {label} link must contain path and sha256"]
    path, normalized = _relative_file(repo_root, value.get("path"))
    if path is None or normalized is None:
        return None, None, [f"production acceptance {label} path is missing or unsafe"]
    if value.get("sha256") != _sha256(path):
        return None, None, [f"production acceptance {label} digest changed"]
    return path, normalized, []


def _check_production_acceptance_import(
    entry: dict[str, Any],
    binding: dict[str, Any],
    repo_root: Path,
    bound_artifacts: set[str],
    approved_cloud_source_commit: str | None,
    trusted_admission_signers: dict[str, Any],
    revoked_admission_ids: set[str],
    revoked_admission_signer_key_ids: set[str],
) -> list[str]:
    """Derive production acceptance; never accept a registry or campaign flag."""

    prefix = f"{entry['path']}: "
    declared = entry.get("production_acceptance")
    if not isinstance(declared, bool):
        return [prefix + "PUBLISHED_EVIDENCE production_acceptance must be boolean"]
    imported = binding.get("production_acceptance_import")
    if not declared:
        if imported is not None:
            return [prefix + "production acceptance import exists but the registry is false"]
        return []
    if not isinstance(imported, dict) or set(imported) != {
        "certificate",
        "campaign",
        "qualification_admission",
        "attestation_bundle",
        "derived_result",
    }:
        return [
            prefix
            + "production acceptance requires a verified certificate/campaign import; "
            "a declared boolean is not evidence"
        ]

    paths: dict[str, Path] = {}
    normalized_paths: set[str] = set()
    problems: list[str] = []
    if (
        not isinstance(approved_cloud_source_commit, str)
        or len(approved_cloud_source_commit) != 40
        or any(
            character not in "0123456789abcdef"
            for character in approved_cloud_source_commit
        )
    ):
        problems.append(
            prefix
            + "an exact externally approved Cloud source commit is required"
        )
    for label in (
        "certificate",
        "campaign",
        "qualification_admission",
        "attestation_bundle",
        "derived_result",
    ):
        value = imported[label]
        path, normalized, link_problems = _production_import_file(value, label, repo_root)
        problems.extend(prefix + problem for problem in link_problems)
        if path is not None and normalized is not None:
            paths[label] = path
            normalized_paths.add(normalized)
    if problems:
        return problems
    if not normalized_paths.issubset(bound_artifacts):
        return [prefix + "production acceptance import files are outside the artifact inventory"]
    if len(normalized_paths) != 5:
        return [prefix + "production acceptance import files must be distinct"]
    try:
        derived = PRODUCTION.import_files(
            paths["certificate"],
            paths["campaign"],
            paths["qualification_admission"],
            paths["attestation_bundle"],
            approved_cloud_source_commit,
            trusted_admission_signers=trusted_admission_signers,
            revoked_admission_ids=revoked_admission_ids,
            revoked_admission_signer_key_ids=revoked_admission_signer_key_ids,
        )
        retained = load_manifest(paths["derived_result"])
    except (PRODUCTION.AcceptanceError, OSError, json.JSONDecodeError) as exc:
        return [prefix + f"production acceptance import was refused: {exc}"]
    if retained != derived:
        problems.append(prefix + "retained production acceptance result is not independently derived")
    bindings = derived.get("bindings", {})
    if bindings.get("flow_version") != entry.get("flow_version"):
        problems.append(prefix + "production acceptance Flow version differs from the registry")
    if bindings.get("flow_release_commit") != entry.get("flow_source_commit"):
        problems.append(prefix + "production acceptance Flow commit differs from the registry")
    if bindings.get("flow_wheel_sha256") != f"sha256:{entry.get('wheel_sha256')}":
        problems.append(prefix + "production acceptance Flow wheel differs from the registry")
    if derived.get("claim_scope") != PRODUCTION.CLAIM_SCOPE:
        problems.append(prefix + "production acceptance claim scope is not supported")
    return problems


def _validated_bindings(
    binding: dict[str, Any],
    key: str,
    repo_root: Path,
    *,
    require_sha256: bool = True,
) -> tuple[list[tuple[dict[str, Any], Path, str]], list[str]]:
    """Validate one non-empty, unique file-binding inventory."""

    raw = binding.get(key)
    if not isinstance(raw, list) or not raw:
        return [], [f"{key} must be a non-empty list"]
    values: list[tuple[dict[str, Any], Path, str]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            problems.append(f"{key} contains a malformed binding")
            continue
        path, normalized = _relative_file(repo_root, item.get("path"))
        if path is None or normalized is None:
            problems.append(f"{key} contains a missing or unsafe path: {item.get('path')!r}")
            continue
        if normalized in seen:
            problems.append(f"{key} contains a duplicate path: {normalized!r}")
            continue
        seen.add(normalized)
        if require_sha256 and item.get("sha256") != _sha256(path):
            problems.append(f"{key} digest changed: {normalized!r}")
            continue
        values.append((item, path, normalized))
    return values, problems


def _inventory_files(repo_root: Path, root: Path, suffix: str | None) -> set[str]:
    return {
        path.relative_to(repo_root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "EVIDENCE_MANIFEST.json"
        and (suffix is None or path.name.endswith(suffix))
    }


def _task_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Return the claim-defining task and oracle fields from one result file."""

    keys = (
        "scope",
        "task",
        "oracle",
        "outcome_definitions",
        "trials_per_arm_condition",
        "trials_per_cell",
        "arms",
        "conditions",
        "fault_modes",
        "verification_modes",
        "cells",
        "profiles",
        "caveats",
        "invariants",
        "identity_coverage",
        "paid_or_remote_mutations",
    )
    return {key: document[key] for key in keys if key in document}


def _contains_numeric_metric(value: Any, names: set[str]) -> bool:
    """Return whether a retained result contains one counted metric."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key in names and isinstance(item, (int, float)) and not isinstance(item, bool):
                return True
            if _contains_numeric_metric(item, names):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_numeric_metric(item, names) for item in value)
    return False


def _metric_coverage(document: dict[str, Any]) -> dict[str, str]:
    """Derive required reliability-metric coverage from retained results."""

    return {
        "silent_incorrect_success": (
            "counted"
            if _contains_numeric_metric(
                document,
                {"silent_incorrect_success", "silent_incorrect_success_count"},
            )
            else "not_counted"
        ),
        "over_halt": (
            "counted"
            if _contains_numeric_metric(document, {"over_halt", "over_halt_count"})
            else "not_counted"
        ),
    }


def _check_task_standard(document: dict[str, Any], campaign_name: object) -> list[str]:
    """Check the minimum public evaluation contract for one campaign."""

    label = repr(campaign_name)
    problems: list[str] = []
    task = document.get("task") or document.get("scope")
    if not isinstance(task, str) or not task.strip():
        problems.append(f"campaign task/scope is missing: {label}")
    trials = document.get("trials_per_arm_condition", document.get("trials_per_cell"))
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 3:
        problems.append(f"campaign has fewer than 3 trials per condition: {label}")
    oracle = document.get("oracle")
    invariants = document.get("invariants")
    if not (isinstance(oracle, str) and oracle.strip()) and not (
        isinstance(invariants, list) and invariants
    ):
        problems.append(f"campaign oracle/invariants are missing: {label}")
    caveats = document.get("caveats")
    if (
        not isinstance(caveats, list)
        or not caveats
        or not all(isinstance(item, str) and item.strip() for item in caveats)
    ):
        problems.append(f"campaign caveats are missing: {label}")
    taxonomy = document.get("outcome_definitions")
    if not (isinstance(taxonomy, dict) and taxonomy) and not (
        isinstance(invariants, list) and invariants
    ):
        problems.append(f"campaign failure taxonomy is missing: {label}")
    return problems


def check_evidence_manifest(
    entry: dict[str, Any],
    repo_root: Path,
    approved_cloud_source_commit: str | None = None,
    trusted_admission_signers: dict[str, Any] | None = None,
    revoked_admission_ids: set[str] | None = None,
    revoked_admission_signer_key_ids: set[str] | None = None,
) -> list[str]:
    """Verify the current result set is still the exact measurement set."""

    problems: list[str] = []
    manifest_path = _safe_file(repo_root, entry.get("evidence_manifest"))
    if manifest_path is None:
        return [f"{entry['path']}: evidence_manifest is missing or unsafe"]
    try:
        binding = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{entry['path']}: evidence_manifest cannot be read: {exc}"]
    prefix = f"{entry['path']}: "
    if binding.get("schema_version") != 3:
        problems.append(prefix + "evidence manifest schema_version must be 3")
    flow = binding.get("flow")
    if not isinstance(flow, dict):
        problems.append(f"{entry['path']}: evidence manifest flow binding is missing")
    else:
        for key, expected in (
            ("version", entry.get("flow_version")),
            ("source_commit", entry.get("flow_source_commit")),
            ("release_tag", entry.get("flow_release_tag")),
            ("wheel_sha256", entry.get("wheel_sha256")),
            ("sdist_sha256", entry.get("sdist_sha256")),
        ):
            if flow.get(key) != expected:
                problems.append(
                    f"{entry['path']}: evidence manifest flow {key} disagrees with PUBLISHED_EVIDENCE"
                )
    flow_binding = flow if isinstance(flow, dict) else {}
    results_path = _safe_file(repo_root, f"{entry['path']}/results.json")
    if results_path is None:
        return problems + [f"{entry['path']}: results.json is missing"]
    evidence_root = repo_root / entry["path"]
    verifier_bindings, binding_problems = _validated_bindings(binding, "verifiers", repo_root)
    problems.extend(prefix + problem for problem in binding_problems)
    verifier_digests = {normalized: item["sha256"] for item, _, normalized in verifier_bindings}

    public_reports, binding_problems = _validated_bindings(binding, "public_reports", repo_root)
    problems.extend(prefix + problem for problem in binding_problems)
    bound_reports = {normalized for _, _, normalized in public_reports}
    actual_reports = _inventory_files(repo_root, evidence_root, ".md")
    if bound_reports != actual_reports:
        problems.append(
            prefix
            + "public_reports inventory differs from retained Markdown files: "
            + f"missing={sorted(actual_reports - bound_reports)}, extra={sorted(bound_reports - actual_reports)}"
        )

    artifacts, binding_problems = _validated_bindings(binding, "artifacts", repo_root)
    problems.extend(prefix + problem for problem in binding_problems)
    bound_artifacts = {normalized for _, _, normalized in artifacts}
    actual_artifacts = _inventory_files(repo_root, evidence_root, None) - actual_reports
    if bound_artifacts != actual_artifacts:
        problems.append(
            prefix
            + "artifacts inventory differs from retained JSON files: "
            + f"missing={sorted(actual_artifacts - bound_artifacts)}, extra={sorted(bound_artifacts - actual_artifacts)}"
        )

    campaigns = binding.get("campaigns")
    seen_results: set[str] = set()
    if not isinstance(campaigns, list) or not campaigns:
        problems.append(prefix + "campaigns must be a non-empty list")
    else:
        seen_campaigns: set[str] = set()
        verifier_paths = {normalized for _, _, normalized in verifier_bindings}
        for campaign in campaigns:
            if not isinstance(campaign, dict):
                problems.append(prefix + "campaigns contains a malformed binding")
                continue
            name = campaign.get("name")
            if not isinstance(name, str) or not name or name in seen_campaigns:
                problems.append(prefix + f"campaign name is missing or duplicate: {name!r}")
            else:
                seen_campaigns.add(name)
            result_path, normalized_result = _relative_file(repo_root, campaign.get("results_path"))
            if result_path is None or normalized_result is None:
                problems.append(
                    prefix
                    + f"campaign results path is missing or unsafe: {campaign.get('results_path')!r}"
                )
                continue
            if normalized_result in seen_results:
                problems.append(
                    prefix + f"campaign results path is duplicate: {normalized_result!r}"
                )
            seen_results.add(normalized_result)
            verifier_path = campaign.get("verifier_path")
            if verifier_path not in verifier_paths:
                problems.append(
                    prefix
                    + f"campaign verifier is not in the verifier inventory: {verifier_path!r}"
                )
            try:
                document = load_manifest(result_path)
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(
                    prefix + f"campaign results cannot be read: {normalized_result!r}: {exc}"
                )
                continue
            source = document.get("source", {})
            if source.get("runner_sha256") != verifier_digests.get(verifier_path):
                problems.append(prefix + f"campaign runner digest differs from {verifier_path!r}")
            if source.get("evals", {}).get("commit") != binding.get("evals_commit"):
                problems.append(
                    prefix + f"campaign evals commit differs from {normalized_result!r}"
                )
            campaign_flow = source.get("flow", {})
            if campaign_flow.get("version") != flow_binding.get("version") or campaign_flow.get(
                "artifact", {}
            ).get("sha256") != flow_binding.get("wheel_sha256"):
                problems.append(
                    prefix + f"campaign Flow binding differs from {normalized_result!r}"
                )
            if campaign_flow.get("commit") != flow_binding.get(
                "source_commit"
            ) or campaign_flow.get("release_tag") != flow_binding.get("release_tag"):
                problems.append(
                    prefix + f"campaign Flow source binding differs from {normalized_result!r}"
                )
            if campaign.get("environment") != document.get("environment"):
                problems.append(prefix + f"campaign environment differs from {normalized_result!r}")
            runtime = campaign.get("runtime")
            required_runtime = {
                "python_version",
                "dependency_snapshot_filename",
                "dependency_snapshot_sha256",
                "openadapt_types_version",
            }
            if not isinstance(runtime, dict) or not required_runtime.issubset(runtime):
                problems.append(prefix + f"campaign runtime facts are incomplete: {name!r}")
            else:
                for field in required_runtime:
                    if (
                        not isinstance(runtime.get(field), str)
                        or not runtime[field]
                        or runtime[field].lower() in {"unknown", "not_recorded"}
                    ):
                        problems.append(
                            prefix + f"campaign runtime field is not exact: {name!r}.{field}"
                        )
                dependency_name = runtime.get("dependency_snapshot_filename")
                dependency_relative = (
                    Path(normalized_result).parent / dependency_name
                    if isinstance(dependency_name, str)
                    else None
                )
                dependency_path = _safe_file(
                    repo_root,
                    dependency_relative.as_posix() if dependency_relative is not None else None,
                )
                if dependency_path is None or runtime.get("dependency_snapshot_sha256") != _sha256(
                    dependency_path
                ):
                    problems.append(
                        prefix + f"campaign dependency snapshot changed or is missing: {name!r}"
                    )
                if "chromium" in document.get("environment", {}):
                    for field in ("browser_version", "browser_revision"):
                        if (
                            not isinstance(runtime.get(field), str)
                            or not runtime[field]
                            or runtime[field].lower() in {"unknown", "not_recorded"}
                        ):
                            problems.append(
                                prefix + f"browser runtime field is not exact: {name!r}.{field}"
                            )
            if runtime != document.get("runtime"):
                problems.append(prefix + f"campaign runtime differs from {normalized_result!r}")
            problems.extend(prefix + problem for problem in _check_task_standard(document, name))
            metric_coverage = _metric_coverage(document)
            if campaign.get("metric_coverage") != metric_coverage:
                problems.append(
                    prefix + f"campaign metric coverage differs from {normalized_result!r}"
                )
            evidence_scope = campaign.get("evidence_scope")
            if not isinstance(evidence_scope, dict):
                problems.append(prefix + f"campaign evidence scope is missing: {name!r}")
            else:
                if evidence_scope.get("class") not in {
                    "contract_fixture",
                    "local_synthetic",
                    "customer_environment",
                    "production",
                }:
                    problems.append(prefix + f"campaign evidence class is invalid: {name!r}")
                for scope_field in (
                    "customer_workflow",
                    "hosted_execution",
                    "real_remote_session",
                ):
                    if not isinstance(evidence_scope.get(scope_field), bool):
                        problems.append(
                            prefix
                            + f"campaign evidence scope field is not boolean: {name!r}.{scope_field}"
                        )
                production_acceptance = evidence_scope.get("production_acceptance")
                if not isinstance(production_acceptance, bool):
                    problems.append(
                        prefix + f"campaign production acceptance is not boolean: {name!r}"
                    )
                elif production_acceptance:
                    problems.append(
                        prefix
                        + f"campaign production acceptance cannot be declared; use the verified importer: {name!r}"
                    )
        result_artifacts = {
            path
            for path in bound_artifacts
            if path.endswith("/results.json") or path == f"{entry['path']}/results.json"
        }
        if seen_results != result_artifacts:
            problems.append(
                prefix
                + "campaign results inventory differs from retained results: "
                + f"missing={sorted(result_artifacts - seen_results)}, extra={sorted(seen_results - result_artifacts)}"
            )
    contracts = binding.get("task_contracts")
    if not isinstance(contracts, list) or not contracts:
        problems.append(f"{entry['path']}: task_contracts are missing")
    else:
        seen_contracts: set[str] = set()
        for contract in contracts:
            if not isinstance(contract, dict):
                problems.append(f"{entry['path']}: malformed task contract")
                continue
            path = _safe_file(repo_root, contract.get("results_path"))
            if path is None:
                problems.append(f"{entry['path']}: task contract results file is missing")
                continue
            normalized = path.relative_to(repo_root).as_posix()
            if normalized in seen_contracts:
                problems.append(f"{entry['path']}: duplicate task contract: {normalized!r}")
                continue
            seen_contracts.add(normalized)
            try:
                actual = _task_contract(load_manifest(path))
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{entry['path']}: task contract results cannot be read: {exc}")
                continue
            if contract.get("value") != actual or contract.get("sha256") != _canonical_sha256(
                actual
            ):
                problems.append(
                    f"{entry['path']}: task or oracle contract changed: {contract.get('results_path')!r}"
                )
        if contracts and seen_contracts != seen_results:
            problems.append(
                prefix
                + "task contract inventory differs from campaign results: "
                + f"missing={sorted(seen_results - seen_contracts)}, extra={sorted(seen_contracts - seen_results)}"
            )
    problems.extend(
        _check_production_acceptance_import(
            entry,
            binding,
            repo_root,
            bound_artifacts,
            approved_cloud_source_commit,
            trusted_admission_signers or {},
            revoked_admission_ids or set(),
            revoked_admission_signer_key_ids or set(),
        )
    )
    return problems


def check_against_release(
    entry: dict[str, Any],
    release_version: str,
    release_wheel_sha256: Optional[str],
    today: date | None = None,
) -> list[str]:
    """The current evidence set must pin the current published release."""

    problems: list[str] = []
    if entry.get("flow_version") != release_version:
        if accepted_drift_in_force(entry, release_version, today):
            return []
        problem = (
            f"{entry['path']}: published evidence is pinned to openadapt-flow "
            f"{entry.get('flow_version')} but the current published release is "
            f"{release_version}. Re-run the comparison against "
            f"{release_version} and publish a new evidence set, or the public "
            f"numbers describe an engine nobody is running."
        )
        waiver = entry.get("accepted_drift")
        if isinstance(waiver, dict) and waiver.get("version") == release_version:
            if check_accepted_drift(entry, today):
                problem += f" The accepted drift for {release_version} is malformed."
            else:
                problem += (
                    f" The accepted drift for {release_version} expired on "
                    f"{waiver['expires']}."
                )
        problems.append(problem)
    elif release_wheel_sha256 and entry.get("wheel_sha256") != release_wheel_sha256:
        problems.append(
            f"{entry['path']}: pinned wheel digest {entry.get('wheel_sha256')} "
            f"does not match the published {release_version} wheel digest "
            f"{release_wheel_sha256}"
        )
    return problems


def fetch_current_release(package: str, timeout: float = 20.0) -> tuple[str, Optional[str]]:
    """Return the newest non-yanked (version, wheel sha256) from PyPI."""

    with urllib.request.urlopen(PYPI_URL.format(package=package), timeout=timeout) as response:
        document = json.loads(response.read())
    version = document["info"]["version"]
    wheel_sha: Optional[str] = None
    for file_info in document.get("urls", []):
        if file_info.get("packagetype") == "bdist_wheel" and not file_info.get("yanked"):
            wheel_sha = file_info["digests"]["sha256"]
            break
    return version, wheel_sha


def _external_json_object(value: str | None, label: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DriftError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DriftError(f"{label} must be a JSON object")
    return parsed


def _external_json_string_set(value: str | None, label: str) -> set[str]:
    if not value:
        return set()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DriftError(f"{label} is not valid JSON") from exc
    if (
        not isinstance(parsed, list)
        or not all(isinstance(item, str) and item for item in parsed)
        or len(parsed) != len(set(parsed))
    ):
        raise DriftError(f"{label} must be a unique JSON string list")
    return set(parsed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when published evidence drifts from the current published engine release."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run only the offline consistency checks; do not query PyPI.",
    )
    parser.add_argument(
        "--current-version",
        help="Supply the current published release instead of querying PyPI.",
    )
    parser.add_argument(
        "--current-wheel-sha256",
        help="Supply the current published wheel digest instead of querying PyPI.",
    )
    parser.add_argument(
        "--approved-cloud-source-commit",
        help=(
            "Externally reviewed Cloud commit. Required when the current evidence "
            "declares production acceptance."
        ),
    )
    parser.add_argument(
        "--trusted-admission-signers-json",
        help="External JSON trust registry for approved qualification admission signers.",
    )
    parser.add_argument(
        "--revoked-admission-ids-json",
        help="External JSON list of revoked qualification admission IDs.",
    )
    parser.add_argument(
        "--revoked-admission-signer-key-ids-json",
        help="External JSON list of revoked qualification signer key IDs.",
    )
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    package = manifest.get("package", "openadapt-flow")
    problems: list[str] = []
    try:
        entry = current_entry(manifest)
        trusted_admission_signers = _external_json_object(
            args.trusted_admission_signers_json,
            "qualification signer trust registry",
        )
        revoked_admission_ids = _external_json_string_set(
            args.revoked_admission_ids_json,
            "qualification admission revocations",
        )
        revoked_admission_signer_key_ids = _external_json_string_set(
            args.revoked_admission_signer_key_ids_json,
            "qualification signer revocations",
        )
    except DriftError as exc:
        print(f"DRIFT: {exc}", file=sys.stderr)
        return 1

    problems.extend(check_superseded_links(manifest))
    problems.extend(check_accepted_drift(entry))
    problems.extend(check_entry_matches_artifact(entry, args.repo_root))
    problems.extend(
        check_evidence_manifest(
            entry,
            args.repo_root,
            args.approved_cloud_source_commit,
            trusted_admission_signers,
            revoked_admission_ids,
            revoked_admission_signer_key_ids,
        )
    )

    release_version = args.current_version
    release_wheel = args.current_wheel_sha256
    if release_version is None and not args.offline:
        try:
            release_version, release_wheel = fetch_current_release(package)
        except (urllib.error.URLError, TimeoutError, OSError, KeyError) as exc:
            # Unreachable PyPI is not evidence of drift; say so and continue.
            print(
                f"WARNING: could not query PyPI for {package} ({exc}); skipping "
                "the release-freshness comparison",
                file=sys.stderr,
            )
    if release_version is not None:
        problems.extend(check_against_release(entry, release_version, release_wheel))

    if problems:
        for problem in problems:
            print(f"DRIFT: {problem}", file=sys.stderr)
        return 1

    if release_version is None:
        # The release-freshness comparison did not run. Skipping it is
        # deliberate (see the module docstring) and the exit code stays 0, but
        # the message must not claim the binding was verified: in a CI log
        # where only the last line is read, that reads as a passing freshness
        # check when the check never happened.
        print(
            f"Published evidence {entry['path']} passed its OFFLINE checks. "
            f"The release-freshness comparison against the current {package} "
            f"release was SKIPPED" + (" (--offline)." if args.offline else " (PyPI unreachable).")
        )
        return 0

    waiver = accepted_drift_in_force(entry, release_version)
    if waiver is not None:
        print(
            f"Published evidence {entry['path']} is pinned to {package} "
            f"{entry['flow_version']}. The current release {release_version} "
            f"has NOT been re-measured; that drift is accepted until "
            f"{waiver['expires']}: {waiver['reason']}"
        )
        return 0

    print(
        f"Published evidence {entry['path']} is bound to the current "
        f"{package} release {entry['flow_version']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
