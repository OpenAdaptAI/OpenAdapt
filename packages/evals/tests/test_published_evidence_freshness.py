from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_published_evidence_freshness.py"
MANIFEST = ROOT / "docs" / "eval_results" / "PUBLISHED_EVIDENCE.json"
SPEC = importlib.util.spec_from_file_location("published_evidence_freshness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_repo_manifest_is_internally_consistent() -> None:
    """The committed manifest must agree with the artifacts it describes.

    This is the offline half of the drift guard and needs no network, so it
    runs on every pull request.
    """

    assert MODULE.main(["--offline"]) == 0


def test_repo_manifest_keeps_old_evidence_stale_and_names_the_exact_rerun() -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    current = MODULE.current_entry(manifest)
    for old_version in ("1.31.0", "1.32.0", "1.33.0"):
        old = next(
            item
            for item in manifest["evidence"]
            if item["flow_version"] == old_version
        )
        assert old["status"] == "superseded"
        assert "not relabeled" in old["stale_reason"].lower()
    assert current["flow_version"] == "1.34.0"
    assert current["production_acceptance"] is False
    previous = next(
        item for item in manifest["evidence"] if item["flow_version"] == "1.33.0"
    )
    assert previous["superseded_by"] == current["path"]


def test_drift_is_detected_when_a_newer_release_is_published() -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    entry = MODULE.current_entry(manifest)

    problems = MODULE.check_against_release(entry, "99.0.0", None)

    assert problems
    assert "99.0.0" in problems[0]


def test_no_drift_when_the_pinned_release_is_current() -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    entry = MODULE.current_entry(manifest)

    problems = MODULE.check_against_release(entry, entry["flow_version"], entry["wheel_sha256"])

    assert problems == []


def test_a_republished_wheel_digest_is_drift() -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    entry = MODULE.current_entry(manifest)

    problems = MODULE.check_against_release(entry, entry["flow_version"], "0" * 64)

    assert problems
    assert "digest" in problems[0]


def test_two_current_evidence_sets_are_refused() -> None:
    manifest = {
        "evidence": [
            {"path": "a", "status": "current"},
            {"path": "b", "status": "current"},
        ]
    }

    with pytest.raises(MODULE.DriftError, match="exactly one"):
        MODULE.current_entry(manifest)


def test_superseded_entry_must_name_an_existing_replacement() -> None:
    dangling = {
        "evidence": [
            {"path": "a", "status": "superseded", "superseded_by": "missing"},
            {"path": "b", "status": "current"},
        ]
    }

    problems = MODULE.check_superseded_links(dangling)

    assert problems and "not in the manifest" in problems[0]

    orphan = {"evidence": [{"path": "a", "status": "superseded"}]}
    assert "no superseded_by" in MODULE.check_superseded_links(orphan)[0]


def test_manifest_that_disagrees_with_its_artifact_is_drift(tmp_path: Path) -> None:
    directory = tmp_path / "docs" / "eval_results" / "set"
    directory.mkdir(parents=True)
    (directory / "results.json").write_text(
        json.dumps({"source": {"flow": {"version": "1.24.0", "artifact": {"sha256": "a" * 64}}}}),
        encoding="utf-8",
    )
    entry = {
        "path": "docs/eval_results/set",
        "flow_version": "1.16.1",
        "wheel_sha256": "a" * 64,
    }

    problems = MODULE.check_entry_matches_artifact(entry, tmp_path)

    assert problems and "results.json recorded" in problems[0]


def test_stale_evidence_names_the_runtime_facts_that_were_not_retained() -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    entry = next(item for item in manifest["evidence"] if item["flow_version"] == "1.28.0")

    problems = MODULE.check_evidence_manifest(entry, ROOT)

    assert any("openadapt_types_version" in problem for problem in problems)
    assert any("browser_revision" in problem for problem in problems)
    assert any("dependency snapshot" in problem for problem in problems)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _valid_bound_evidence(tmp_path: Path) -> tuple[dict[str, object], Path, dict[str, object]]:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    verifier = tmp_path / "scripts" / "verify.py"
    verifier.parent.mkdir()
    verifier.write_text("# independent verifier\n", encoding="utf-8")
    dependency_snapshot = evidence / "dependencies.json"
    _write_json(dependency_snapshot, {"openadapt-types": "0.9.0", "playwright": "1.61.0"})
    report = evidence / "REPORT.md"
    report.write_text("# Bound report\n", encoding="utf-8")
    result = evidence / "results.json"
    result_value = {
        "scope": "local synthetic write acceptance",
        "task": "write one record",
        "oracle": "independent record read",
        "trials_per_cell": 3,
        "outcome_definitions": {
            "correct": "reported complete and independently confirmed",
            "silent_incorrect_success": "reported complete without the effect",
            "over_halt": "reported incomplete when the effect was confirmed",
        },
        "caveats": ["Synthetic fixture only."],
        "aggregate": {
            "silent_incorrect_success_count": 0,
            "over_halt_count": 0,
        },
        "environment": {
            "chromium": "Chromium 140.0.7339.16",
            "platform": "test-platform",
            "playwright": "1.61.0",
            "python": "3.12.13",
        },
        "source": {
            "evals": {"commit": "b" * 40},
            "runner_sha256": MODULE._sha256(verifier),
            "flow": {
                "version": "1.30.0",
                "commit": "c" * 40,
                "release_tag": "v1.30.0",
                "artifact": {"sha256": "a" * 64},
            },
        },
        "runtime": {
            "python_version": "3.12.13",
            "openadapt_types_version": "0.9.0",
            "browser_version": "140.0.7339.16",
            "browser_revision": "1194",
            "dependency_snapshot_filename": "dependencies.json",
            "dependency_snapshot_sha256": MODULE._sha256(dependency_snapshot),
        },
    }
    _write_json(result, result_value)
    contract = MODULE._task_contract(result_value)
    binding: dict[str, object] = {
        "schema_version": 3,
        "evals_commit": "b" * 40,
        "flow": {
            "version": "1.30.0",
            "source_commit": "c" * 40,
            "release_tag": "v1.30.0",
            "wheel_sha256": "a" * 64,
        },
        "verifiers": [{"path": "scripts/verify.py", "sha256": MODULE._sha256(verifier)}],
        "campaigns": [
            {
                "name": "main",
                "results_path": "evidence/results.json",
                "verifier_path": "scripts/verify.py",
                "environment": result_value["environment"],
                "runtime": result_value["runtime"],
                "evidence_scope": {
                    "class": "local_synthetic",
                    "production_acceptance": False,
                    "customer_workflow": False,
                    "hosted_execution": False,
                    "real_remote_session": False,
                },
                "metric_coverage": MODULE._metric_coverage(result_value),
            }
        ],
        "task_contracts": [
            {
                "results_path": "evidence/results.json",
                "value": contract,
                "sha256": MODULE._canonical_sha256(contract),
            }
        ],
        "artifacts": [
            {"path": "evidence/dependencies.json", "sha256": MODULE._sha256(dependency_snapshot)},
            {"path": "evidence/results.json", "sha256": MODULE._sha256(result)},
        ],
        "public_reports": [{"path": "evidence/REPORT.md", "sha256": MODULE._sha256(report)}],
    }
    manifest_path = evidence / "EVIDENCE_MANIFEST.json"
    _write_json(manifest_path, binding)
    entry: dict[str, object] = {
        "path": "evidence",
        "status": "current",
        "flow_version": "1.30.0",
        "flow_source_commit": "c" * 40,
        "flow_release_tag": "v1.30.0",
        "wheel_sha256": "a" * 64,
        "evidence_manifest": "evidence/EVIDENCE_MANIFEST.json",
        "production_acceptance": False,
    }
    return entry, manifest_path, binding


def test_complete_evidence_binding_passes(tmp_path: Path) -> None:
    entry, _, _ = _valid_bound_evidence(tmp_path)

    assert MODULE.check_evidence_manifest(entry, tmp_path) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value.__setitem__("verifiers", []), "verifiers must be a non-empty list"),
        (
            lambda value: value["artifacts"].append(dict(value["artifacts"][0])),
            "duplicate path",
        ),
        (lambda value: value["artifacts"].pop(), "artifacts inventory differs"),
        (lambda value: value.__setitem__("public_reports", []), "public_reports must be"),
        (
            lambda value: value["verifiers"][0].__setitem__("path", "../outside.py"),
            "unsafe path",
        ),
        (
            lambda value: value["campaigns"][0]["runtime"].__setitem__(
                "browser_revision", "not_recorded"
            ),
            "browser_revision",
        ),
        (
            lambda value: value["campaigns"][0].__setitem__("environment", {}),
            "campaign environment differs",
        ),
        (
            lambda value: value["campaigns"][0]["metric_coverage"].__setitem__(
                "over_halt", "not_counted"
            ),
            "metric coverage differs",
        ),
        (
            lambda value: value["campaigns"][0]["evidence_scope"].__setitem__(
                "production_acceptance", "yes"
            ),
            "production acceptance is not boolean",
        ),
        (
            lambda value: value["campaigns"][0]["evidence_scope"].__setitem__(
                "hosted_execution", "yes"
            ),
            "evidence scope field is not boolean",
        ),
        (
            lambda value: value.__setitem__("evals_commit", "c" * 40),
            "campaign evals commit differs",
        ),
        (
            lambda value: value["flow"].__setitem__("source_commit", "d" * 40),
            "campaign Flow source binding differs",
        ),
        (
            lambda value: value["task_contracts"].append(dict(value["task_contracts"][0])),
            "duplicate task contract",
        ),
    ],
)
def test_evidence_binding_refuses_omission_duplication_and_unsafe_paths(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    mutation(binding)
    _write_json(manifest_path, binding)

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any(expected in problem for problem in problems)


@pytest.mark.parametrize(
    "path",
    [
        "scripts/verify.py",
        "evidence/results.json",
        "evidence/dependencies.json",
        "evidence/REPORT.md",
    ],
)
def test_evidence_binding_refuses_any_bound_file_mutation(tmp_path: Path, path: str) -> None:
    entry, _, _ = _valid_bound_evidence(tmp_path)
    target = tmp_path / path
    target.write_bytes(target.read_bytes() + b"tampered")

    assert MODULE.check_evidence_manifest(entry, tmp_path)


def test_safe_file_rejects_leaf_symlink_to_external_private_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "private"
    repo_root.mkdir()
    outside.mkdir()
    private_campaign = outside / "qualification-campaign.json"
    private_campaign.write_text("{}\n", encoding="utf-8")
    (repo_root / "campaign.json").symlink_to(private_campaign)

    assert MODULE._safe_file(repo_root, "campaign.json") is None


def test_safe_file_rejects_parent_directory_symlink(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside = tmp_path / "private"
    repo_root.mkdir()
    outside.mkdir()
    (outside / "qualification-admission.json").write_text("{}\n", encoding="utf-8")
    (repo_root / "linked-private").symlink_to(outside, target_is_directory=True)

    assert (
        MODULE._safe_file(repo_root, "linked-private/qualification-admission.json")
        is None
    )


def test_campaign_cannot_promote_numeric_metrics_to_production(tmp_path: Path) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    result_path = tmp_path / "evidence" / "results.json"
    result = MODULE.load_manifest(result_path)
    del result["aggregate"]["over_halt_count"]
    _write_json(result_path, result)
    binding["artifacts"][1]["sha256"] = MODULE._sha256(result_path)
    contract = MODULE._task_contract(result)
    binding["task_contracts"][0]["value"] = contract
    binding["task_contracts"][0]["sha256"] = MODULE._canonical_sha256(contract)
    binding["campaigns"][0]["metric_coverage"] = MODULE._metric_coverage(result)
    binding["campaigns"][0]["evidence_scope"]["production_acceptance"] = True
    _write_json(manifest_path, binding)

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any("cannot be declared; use the verified importer" in problem for problem in problems)


def test_campaign_class_cannot_claim_production_acceptance(tmp_path: Path) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    binding["campaigns"][0]["evidence_scope"]["production_acceptance"] = True
    entry["production_acceptance"] = True
    _write_json(manifest_path, binding)

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any("cannot be declared; use the verified importer" in problem for problem in problems)


def test_registry_boolean_cannot_claim_production_acceptance(tmp_path: Path) -> None:
    entry, _, _ = _valid_bound_evidence(tmp_path)
    entry["production_acceptance"] = True

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any("declared boolean is not evidence" in problem for problem in problems)


def test_production_import_links_must_be_digest_bound(tmp_path: Path) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    entry["production_acceptance"] = True
    binding["production_acceptance_import"] = {
        "certificate": "evidence/certificate.json",
        "campaign": "evidence/campaign.json",
        "qualification_admission": "evidence/admission.json",
        "attestation_bundle": "evidence/attestation.jsonl",
        "derived_result": "evidence/derived.json",
    }
    _write_json(manifest_path, binding)

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any("link must contain path and sha256" in problem for problem in problems)


def test_production_import_cannot_select_its_own_approved_cloud_commit(
    tmp_path: Path,
) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    entry["production_acceptance"] = True
    binding["production_acceptance_import"] = {
        "certificate": {},
        "campaign": {},
        "attestation_bundle": {},
        "derived_result": {},
        "expected_cloud_source_commit": "c" * 40,
    }
    _write_json(manifest_path, binding)

    problems = MODULE.check_evidence_manifest(entry, tmp_path)

    assert any("declared boolean is not evidence" in problem for problem in problems)


def test_production_import_uses_external_admission_trust_and_revocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, manifest_path, binding = _valid_bound_evidence(tmp_path)
    entry["production_acceptance"] = True
    evidence = tmp_path / "evidence"
    names = (
        "certificate",
        "campaign",
        "qualification_admission",
        "attestation_bundle",
        "derived_result",
    )
    paths: dict[str, Path] = {}
    for name in names:
        suffix = ".jsonl" if name == "attestation_bundle" else ".json"
        path = evidence / f"{name}{suffix}"
        path.write_text("{}\n", encoding="utf-8")
        paths[name] = path
    derived = {
        "bindings": {
            "flow_version": entry["flow_version"],
            "flow_release_commit": entry["flow_source_commit"],
            "flow_wheel_sha256": f"sha256:{entry['wheel_sha256']}",
        },
        "claim_scope": MODULE.PRODUCTION.CLAIM_SCOPE,
    }
    _write_json(paths["derived_result"], derived)
    imported = {
        name: {
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": MODULE._sha256(path),
        }
        for name, path in paths.items()
    }
    binding["production_acceptance_import"] = imported
    binding["artifacts"].extend(imported.values())
    _write_json(manifest_path, binding)
    observed: dict[str, object] = {}

    def import_files(*args: object, **kwargs: object) -> dict[str, object]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return derived

    monkeypatch.setattr(MODULE.PRODUCTION, "import_files", import_files)
    trust = {"qa-ed25519-0000000000000000": {"public_key": "external"}}
    admission_revocations = {"admission-id"}
    signer_revocations = {"qa-ed25519-revoked"}

    problems = MODULE.check_evidence_manifest(
        entry,
        tmp_path,
        "f" * 40,
        trust,
        admission_revocations,
        signer_revocations,
    )

    assert problems == []
    assert observed["args"] == (
        paths["certificate"],
        paths["campaign"],
        paths["qualification_admission"],
        paths["attestation_bundle"],
        "f" * 40,
    )
    assert observed["kwargs"] == {
        "trusted_admission_signers": trust,
        "revoked_admission_ids": admission_revocations,
        "revoked_admission_signer_key_ids": signer_revocations,
    }


def test_external_trust_inputs_must_be_valid_json() -> None:
    assert MODULE.main(["--offline", "--trusted-admission-signers-json", "[]"]) == 1
    assert MODULE.main(["--offline", "--revoked-admission-ids-json", "{}"] ) == 1


def test_task_standard_requires_count_oracle_taxonomy_and_caveats() -> None:
    problems = MODULE._check_task_standard({"task": "x", "trials_per_cell": 2}, "weak")

    assert any("fewer than 3" in problem for problem in problems)
    assert any("oracle/invariants" in problem for problem in problems)
    assert any("failure taxonomy" in problem for problem in problems)
    assert any("caveats" in problem for problem in problems)


# --- Accepted drift: a short, named, expiring waiver --------------------------
#
# When a Flow release ships before the comparison can be re-run, the manifest
# may record that drift as accepted for that one release, with a reason and an
# expiry date. The waiver must never become a permanent false green.

WAIVED_ON = date(2026, 10, 10)


def _entry_with_accepted_drift(**overrides: object) -> dict[str, object]:
    entry = dict(MODULE.current_entry(MODULE.load_manifest(MANIFEST)))
    entry.pop("accepted_drift", None)
    entry["flow_version"] = "1.34.0"
    waiver: dict[str, object] = {
        "version": "1.35.1",
        "reason": "Flow 1.35.1 shipped before the comparison was re-run.",
        "expires": "2026-10-31",
    }
    waiver.update(overrides)
    entry["accepted_drift"] = waiver
    return entry


def test_accepted_drift_waives_only_the_named_release() -> None:
    entry = _entry_with_accepted_drift()

    assert MODULE.check_against_release(entry, "1.35.1", "f" * 64, today=WAIVED_ON) == []
    assert MODULE.accepted_drift_in_force(entry, "1.35.1", today=WAIVED_ON) is not None

    later = MODULE.check_against_release(entry, "1.35.2", None, today=WAIVED_ON)
    assert later and "1.35.2" in later[0]
    assert MODULE.accepted_drift_in_force(entry, "1.35.2", today=WAIVED_ON) is None


def test_expired_accepted_drift_is_drift_again() -> None:
    entry = _entry_with_accepted_drift(expires="2026-10-31")

    assert MODULE.check_against_release(entry, "1.35.1", None, today=date(2026, 10, 31)) == []
    problems = MODULE.check_against_release(entry, "1.35.1", None, today=date(2026, 11, 1))

    assert problems
    assert "1.35.1" in problems[0]
    assert "expired on 2026-10-31" in problems[0]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"reason": ""}, "reason"),
        ({"expires": "soon"}, "expires"),
        ({"expires": "2026-12-31"}, "at most 31 days"),
        ({"version": "1.34.0"}, "already pins"),
        ({"version": ""}, "version"),
        ({"approved_by": "someone"}, "unexpected keys"),
    ],
)
def test_accepted_drift_must_be_short_named_and_complete(
    overrides: dict[str, object], fragment: str
) -> None:
    entry = _entry_with_accepted_drift(**overrides)

    problems = MODULE.check_accepted_drift(entry, today=WAIVED_ON)

    assert problems and any(fragment in problem for problem in problems)
    # A malformed waiver never silences the release comparison either.
    assert MODULE.check_against_release(entry, "1.35.1", None, today=WAIVED_ON)


def test_main_never_reports_waived_drift_as_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = MODULE.load_manifest(MANIFEST)
    for item in manifest["evidence"]:
        if item["status"] == "current":
            item["accepted_drift"] = {
                "version": "99.0.0",
                "reason": "Test waiver.",
                "expires": "2026-10-31",
            }
    path = tmp_path / "PUBLISHED_EVIDENCE.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    argv = ["--manifest", str(path), "--repo-root", str(ROOT), "--current-version", "99.0.0"]

    monkeypatch.setattr(MODULE, "_today", lambda: WAIVED_ON)
    assert MODULE.main(argv) == 0
    out = capsys.readouterr().out
    assert "has NOT been re-measured" in out
    assert "until 2026-10-31" in out
    assert "is bound to the current" not in out

    monkeypatch.setattr(MODULE, "_today", lambda: date(2026, 11, 1))
    assert MODULE.main(argv) == 1


def test_readme_states_which_release_the_current_numbers_were_measured_on() -> None:
    entry = MODULE.current_entry(MODULE.load_manifest(MANIFEST))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert Path(entry["path"]).name in readme
    assert f"against Flow {entry['flow_version']}" in readme
    waiver = entry.get("accepted_drift")
    if waiver:
        # While drift is accepted, the public page must say the newer release
        # was not measured, not imply the numbers describe it.
        assert f"Flow {waiver['version']} has not been re-measured" in readme


def test_a_failing_scheduled_check_leaves_an_open_issue() -> None:
    """GitHub turns off a public repo's schedules after 60 days without a
    commit. A red scheduled run must leave a durable signal that outlives the
    schedule, so the alarm cannot vanish with nobody having acted on it."""

    workflow = (ROOT / ".github" / "workflows" / "evidence-freshness.yml").read_text(
        encoding="utf-8"
    )
    job = re.search(r"(?ms)^  report-stale-evidence:\n(.*?)(?=^  \S|\Z)", workflow)
    assert job is not None
    body = job.group(1)
    assert re.search(r"needs: \[?[^\n]*freshness", body)
    assert "failure()" in body
    assert "github.event_name == 'schedule'" in body
    assert "github.actor != 'openadapt-lifecycle[bot]'" in body
    assert "issues: write" in body
    assert "gh issue create" in body
    assert "gh issue edit" in body
    # Only this job may write issues; the check itself stays read-only.
    assert workflow.count("issues: write") == 1
