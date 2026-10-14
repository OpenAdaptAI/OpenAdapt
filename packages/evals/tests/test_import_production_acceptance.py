from __future__ import annotations

import base64
import copy
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_production_acceptance.py"
FIXTURES = ROOT / "tests" / "fixtures" / "production_acceptance"
SPEC = importlib.util.spec_from_file_location("production_acceptance_importer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

NOW = datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _certificate() -> dict[str, object]:
    return _fixture("live-certificate.json")


def _campaign() -> dict[str, object]:
    return _fixture("qualification-campaign.json")


def _admission() -> dict[str, object]:
    return _fixture("qualification-admission.json")


def _admission_trust() -> dict[str, object]:
    return _fixture("qualification-admission-trust.json")


def _attestation() -> dict[str, str]:
    return {
        "repository": MODULE.CLOUD_REPOSITORY,
        "workflow": MODULE.CLOUD_WORKFLOW,
        "certificate_identity": MODULE.CLOUD_CERTIFICATE_IDENTITY,
        "source_commit": "f" * 40,
        "bundle_sha256": "sha256:" + "a1" * 32,
    }


def _lifecycle_policy_bytes() -> bytes:
    def target(
        target_id: str,
        claim_scope: str,
        *,
        release_kind: str = "public_package",
        artifact_kinds: list[str] | None = None,
        project: str | None = None,
    ) -> dict[str, object]:
        kinds = artifact_kinds if artifact_kinds is not None else ["sdist", "wheel"]
        return {
            "id": target_id,
            "display_name": target_id.title(),
            "lifecycle_scope": "repository",
            "lifecycle_subject": target_id,
            "source_repository": f"OpenAdaptAI/{target_id}",
            "release_kind": release_kind,
            "required_claim_scope": claim_scope,
            "required_artifact_kinds": kinds,
            "package_index_project": project or target_id,
            "artifact_authority_by_kind": {kind: "pypi" for kind in kinds},
        }

    targets = [
        target("agent", "qualified_agent_bridge_release"),
        target("capture", "qualified_native_recorder_release"),
        target(
            "cloud",
            "qualified_workflow_control_plane_deployment",
            release_kind="private_deployment",
            artifact_kinds=[],
        ),
        target(
            "desktop",
            "qualified_native_workflow_desktop_release",
            artifact_kinds=[
                "linux-installer",
                "macos-installer",
                "sdist",
                "wheel",
                "windows-installer",
            ],
        ),
        target(
            "docs",
            "production_documentation_deployment",
            release_kind="public_deployment",
            artifact_kinds=["deployment-manifest", "site-archive"],
        ),
        {
            "id": "flow",
            "display_name": "OpenAdapt Flow",
            "lifecycle_scope": "repository",
            "lifecycle_subject": "openadapt-flow",
            "source_repository": "OpenAdaptAI/openadapt-flow",
            "release_kind": "public_package",
            "required_claim_scope": "qualified_workflow_runtime_release",
            "required_artifact_kinds": ["sdist", "wheel"],
            "package_index_project": "openadapt-flow",
            "artifact_authority_by_kind": {"sdist": "pypi", "wheel": "pypi"},
        },
        target("openadapt", "qualified_workflow_launcher_release"),
    ]
    policy = {
        "$schema": "schemas/production-lifecycle-policy.schema.json",
        "schema_version": "openadapt.production-lifecycle-policy/v1",
        "revision": 1,
        "maximum_admission_days": 30,
        "summary_authority": {},
        "targets": targets,
    }
    return (json.dumps(policy, indent=2) + "\n").encode()


def _flow_lifecycle_inputs(
    source: dict[str, object] | None = None,
) -> tuple[bytes, dict[str, object], dict[str, object]]:
    source = source or _derive()
    bindings = source["bindings"]
    assert isinstance(bindings, dict)
    version = bindings["flow_version"]
    commit = bindings["flow_release_commit"]
    artifacts = [
        {
            "authority": "pypi",
            "kind": "sdist",
            "name": f"openadapt_flow-{version}.tar.gz",
            "sha256": "sha256:" + "c" * 64,
            "size_bytes": 101,
            "url": (
                "https://files.pythonhosted.org/packages/cc/cc/"
                f"openadapt_flow-{version}.tar.gz"
            ),
        },
        {
            "authority": "pypi",
            "kind": "wheel",
            "name": f"openadapt_flow-{version}-py3-none-any.whl",
            "sha256": bindings["flow_wheel_sha256"],
            "size_bytes": 202,
            "url": (
                "https://files.pythonhosted.org/packages/dd/dd/"
                f"openadapt_flow-{version}-py3-none-any.whl"
            ),
        },
    ]
    release = {
        "kind": "public_package",
        "version": version,
        "tag": f"v{version}",
        "source_commit": commit,
        "immutable_release_url": (
            "https://github.com/OpenAdaptAI/openadapt-flow/commit/" + commit
        ),
        "artifacts": artifacts,
    }
    metadata = {
        "info": {"version": version},
        "urls": [
            {
                "filename": artifact["name"],
                "url": artifact["url"],
                "size": artifact["size_bytes"],
                "digests": {
                    "sha256": str(artifact["sha256"]).removeprefix("sha256:")
                },
                "yanked": False,
            }
            for artifact in artifacts
        ],
    }
    return _lifecycle_policy_bytes(), release, metadata


def _verified_flow_lifecycle(
    source: dict[str, object] | None = None,
) -> object:
    policy, release, metadata = _flow_lifecycle_inputs(source)
    return MODULE.verify_production_lifecycle_release(
        policy,
        "flow",
        release,
        pypi_release_metadata=metadata,
    )


def _resign_admission(
    admission: dict[str, object],
    campaign: dict[str, object],
) -> None:
    payload = admission["payload"]
    assert isinstance(payload, dict)
    campaign_binding = payload["campaign"]
    assert isinstance(campaign_binding, dict)
    campaign_binding["artifact_sha256"] = MODULE.canonical_sha256(campaign).removeprefix(
        "sha256:"
    )
    campaign_binding["contract_sha256"] = MODULE.canonical_sha256(
        campaign["qualification_contract"]
    ).removeprefix("sha256:")
    campaign_binding["outcomes_sha256"] = MODULE.canonical_sha256(
        {
            "conditions": campaign["conditions"],
            "invariants": campaign["invariants"],
            "excluded_trials": campaign["excluded_trials"],
        }
    ).removeprefix("sha256:")
    oracle = campaign["oracle_contract"]
    assert isinstance(oracle, dict)
    campaign_binding["oracle_id"] = oracle["schema_version"]
    campaign_binding["oracle_contract_sha256"] = MODULE.canonical_sha256(
        oracle
    ).removeprefix("sha256:")
    contract = campaign["qualification_contract"]
    assert isinstance(contract, dict)
    conditions = campaign["conditions"]
    assert isinstance(conditions, list)
    campaign_binding["tasks"] = sorted(
        [
            {
                "task": contract["task_id"],
                "condition": condition["condition_id"],
                "required_trials": condition["required_trials"],
                "observed_trials": len(condition["trials"]),
            }
            for condition in conditions
        ],
        key=lambda value: (value["task"], value["condition"]),
    )
    _sign_admission_payload(admission)


def _rebind_evidence_identity(
    certificate: dict[str, object],
    campaign: dict[str, object],
    admission: dict[str, object],
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload = admission["payload"]
    assert isinstance(payload, dict)
    identity = payload["evidence_identity"]
    assert isinstance(identity, dict)
    old_identity_digest = MODULE.evidence_identity_sha256(identity)
    identity["campaign_id"] = campaign["campaign_id"]
    identity["campaign_contract_sha256"] = MODULE.canonical_sha256(
        campaign["qualification_contract"]
    ).removeprefix("sha256:")
    identity["oracle_contract_sha256"] = MODULE.canonical_sha256(
        campaign["oracle_contract"]
    ).removeprefix("sha256:")
    identity_digest = MODULE.evidence_identity_sha256(identity)
    campaign["admission_id"] = payload["admission_id"]
    campaign["runtime_validation_id"] = payload["runtime_validation_id"]
    campaign["evidence_identity_sha256"] = identity_digest
    qualification = certificate["qualification"]
    assert isinstance(qualification, dict)
    qualification["evidence_identity_sha256"] = "sha256:" + identity_digest
    if identity_digest == old_identity_digest:
        return
    old_envelopes = campaign["receipt_envelopes"]
    assert isinstance(old_envelopes, dict)
    new_envelopes: dict[str, object] = {}
    conditions = campaign["conditions"]
    assert isinstance(conditions, list)
    for condition in conditions:
        assert isinstance(condition, dict)
        trials = condition["trials"]
        assert isinstance(trials, list)
        for row in trials:
            assert isinstance(row, dict)
            row["admission_id"] = payload["admission_id"]
            row["runtime_validation_id"] = payload["runtime_validation_id"]
            row["evidence_identity_sha256"] = identity_digest
            for receipt_type, row_field in MODULE._RECEIPT_ROW_FIELDS.items():
                old_digest = row[row_field]
                if old_digest is None:
                    continue
                assert isinstance(old_digest, str)
                envelope = copy.deepcopy(old_envelopes[old_digest])
                assert isinstance(envelope, dict)
                projection = envelope["verified_projection"]
                assert isinstance(projection, dict)
                projection["admission_id"] = payload["admission_id"]
                projection["runtime_validation_id"] = payload["runtime_validation_id"]
                projection["evidence_identity_sha256"] = identity_digest
                if receipt_type == "runner":
                    facts = projection["facts"]
                    assert isinstance(facts, dict)
                    counter = facts["model_call_counter"]
                    assert isinstance(counter, dict)
                    counter["report_sha256"] = envelope["source_artifact_sha256"]
                    counter["egress_policy_sha256"] = identity[
                        "network_policy_sha256"
                    ]
                private = Ed25519PrivateKey.from_private_bytes(
                    bytes([20 + MODULE._RECEIPT_TYPES.index(receipt_type)]) * 32
                )
                signed = {key: envelope[key] for key in MODULE._RECEIPT_SIGNED_KEYS}
                envelope["signature"] = base64.b64encode(
                    private.sign(
                        MODULE.RECEIPT_SIGNATURE_DOMAIN
                        + MODULE.canonical_json(signed).encode("utf-8")
                    )
                ).decode("ascii")
                new_digest = MODULE.canonical_sha256(envelope).removeprefix("sha256:")
                new_envelopes[new_digest] = envelope
                row[row_field] = new_digest
    campaign["receipt_envelopes"] = new_envelopes


def _sign_admission_payload(admission: dict[str, object]) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload = admission["payload"]
    assert isinstance(payload, dict)
    private = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    admission["signature"] = base64.b64encode(
        private.sign(
            MODULE.ADMISSION_SIGNATURE_DOMAIN
            + MODULE.canonical_json(payload).encode("utf-8")
        )
    ).decode("ascii")


def _rebind(
    certificate: dict[str, object],
    campaign: dict[str, object],
    admission: dict[str, object] | None = None,
) -> dict[str, object]:
    admission = admission or _admission()
    _rebind_evidence_identity(certificate, campaign, admission)
    _resign_admission(admission, campaign)
    qualification = certificate["qualification"]
    assert isinstance(qualification, dict)
    qualification["campaign_contract_sha256"] = MODULE.canonical_sha256(
        campaign["qualification_contract"]
    )
    qualification["campaign_artifact_sha256"] = MODULE.canonical_sha256(campaign)
    qualification["campaign_outcomes_sha256"] = MODULE.canonical_sha256(
        {
            "conditions": campaign["conditions"],
            "invariants": campaign["invariants"],
            "excluded_trials": campaign["excluded_trials"],
        }
    )
    qualification["oracle_contract_sha256"] = MODULE.canonical_sha256(
        campaign["oracle_contract"]
    )
    qualification["qualification_admission_sha256"] = MODULE.canonical_sha256(admission)
    return admission


def _derive(
    certificate: dict[str, object] | None = None,
    campaign: dict[str, object] | None = None,
    admission: dict[str, object] | None = None,
    attestation: dict[str, str] | None = None,
    *,
    now: datetime = NOW,
    trusted_admission_signers: dict[str, object] | None = None,
    revoked_admission_ids: set[str] | frozenset[str] = frozenset(),
    revoked_admission_signer_key_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, object]:
    return MODULE.derive_production_acceptance(
        certificate or _certificate(),
        campaign or _campaign(),
        admission or _admission(),
        attestation=attestation or _attestation(),
        expected_cloud_source_commit="f" * 40,
        trusted_admission_signers=(
            trusted_admission_signers
            if trusted_admission_signers is not None
            else _admission_trust()
        ),
        revoked_admission_ids=revoked_admission_ids,
        revoked_admission_signer_key_ids=revoked_admission_signer_key_ids,
        now=now,
    )


def _trial(campaign: dict[str, object], condition: int = 0, trial: int = 0) -> dict[str, object]:
    conditions = campaign["conditions"]
    assert isinstance(conditions, list)
    condition_value = conditions[condition]
    assert isinstance(condition_value, dict)
    trials = condition_value["trials"]
    assert isinstance(trials, list)
    value = trials[trial]
    assert isinstance(value, dict)
    return value


def _replace_receipt_verdict(
    campaign: dict[str, object],
    *,
    receipt_type: str,
    verdict: str,
    condition: int = 0,
    trial: int = 0,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    row = _trial(campaign, condition, trial)
    row_field = MODULE._RECEIPT_ROW_FIELDS[receipt_type]
    old_digest = row[row_field]
    assert isinstance(old_digest, str)
    envelopes = campaign["receipt_envelopes"]
    assert isinstance(envelopes, dict)
    envelope = copy.deepcopy(envelopes.pop(old_digest))
    projection = envelope["verified_projection"]
    assert isinstance(projection, dict)
    projection["verdict"] = verdict
    private = Ed25519PrivateKey.from_private_bytes(
        bytes([20 + MODULE._RECEIPT_TYPES.index(receipt_type)]) * 32
    )
    signed = {key: envelope[key] for key in MODULE._RECEIPT_SIGNED_KEYS}
    envelope["signature"] = base64.b64encode(
        private.sign(
            MODULE.RECEIPT_SIGNATURE_DOMAIN
            + MODULE.canonical_json(signed).encode("utf-8")
        )
    ).decode("ascii")
    new_digest = MODULE.canonical_sha256(envelope).removeprefix("sha256:")
    envelopes[new_digest] = envelope
    row[row_field] = new_digest


def _replace_receipt_envelope(
    campaign: dict[str, object],
    *,
    receipt_type: str,
    mutate: object,
    resign: bool = False,
    condition: int = 0,
    trial: int = 0,
) -> None:
    row = _trial(campaign, condition, trial)
    row_field = MODULE._RECEIPT_ROW_FIELDS[receipt_type]
    old_digest = row[row_field]
    assert isinstance(old_digest, str)
    envelopes = campaign["receipt_envelopes"]
    assert isinstance(envelopes, dict)
    envelope = copy.deepcopy(envelopes.pop(old_digest))
    mutate(envelope)
    if resign:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.from_private_bytes(
            bytes([20 + MODULE._RECEIPT_TYPES.index(receipt_type)]) * 32
        )
        signed = {key: envelope[key] for key in MODULE._RECEIPT_SIGNED_KEYS}
        envelope["signature"] = base64.b64encode(
            private.sign(
                MODULE.RECEIPT_SIGNATURE_DOMAIN
                + MODULE.canonical_json(signed).encode("utf-8")
            )
        ).decode("ascii")
    new_digest = MODULE.canonical_sha256(envelope).removeprefix("sha256:")
    envelopes[new_digest] = envelope
    row[row_field] = new_digest


def _verified_provenance(
    certificate_path: Path,
    *,
    commit: str = "f" * 40,
    ref: str = "refs/heads/main",
) -> list[dict[str, object]]:
    return [
        {
            "attestation": {},
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "certificateIssuer": "CN=Fulcio Intermediate l2,O=GitHub\\, Inc.",
                        "subjectAlternativeName": MODULE.CLOUD_CERTIFICATE_IDENTITY,
                        "issuer": MODULE.GITHUB_OIDC_ISSUER,
                        "githubWorkflowTrigger": "workflow_dispatch",
                        "githubWorkflowSHA": commit,
                        "githubWorkflowName": "Execute live synthetic acceptance",
                        "githubWorkflowRepository": MODULE.CLOUD_REPOSITORY,
                        "githubWorkflowRef": ref,
                        "buildSignerURI": MODULE.CLOUD_CERTIFICATE_IDENTITY,
                        "buildSignerDigest": commit,
                        "runnerEnvironment": "github-hosted",
                        "sourceRepositoryURI": (
                            "https://github.com/OpenAdaptAI/openadapt-cloud"
                        ),
                        "sourceRepositoryDigest": commit,
                        "sourceRepositoryRef": ref,
                        "sourceRepositoryIdentifier": "123456",
                        "sourceRepositoryOwnerURI": "https://github.com/OpenAdaptAI",
                        "sourceRepositoryOwnerIdentifier": "7890",
                        "buildConfigURI": MODULE.CLOUD_CERTIFICATE_IDENTITY,
                        "buildConfigDigest": commit,
                        "buildTrigger": "workflow_dispatch",
                        "runInvocationURI": (
                            "https://github.com/OpenAdaptAI/openadapt-cloud/"
                            "actions/runs/123456789/attempts/1"
                        ),
                        "sourceRepositoryVisibilityAtSigning": "private",
                    }
                },
                # Both entries, and both time formats, exactly as gh 2.67.0
                # emitted them when it verified a cosign-signed bundle produced
                # by the private OpenAdaptAI/openadapt-cloud repository.
                "verifiedTimestamps": [
                    {
                        "type": "Tlog",
                        "uri": MODULE.PUBLIC_TRANSPARENCY_LOG,
                        "timestamp": "2026-08-18T08:00:05-04:00",
                    },
                    {
                        "type": "TimestampAuthority",
                        "uri": MODULE.PUBLIC_TIMESTAMP_AUTHORITY,
                        "timestamp": "2026-08-18T12:00:04Z",
                    },
                ],
                "verifiedIdentity": MODULE.CLOUD_CERTIFICATE_IDENTITY,
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "subject": [
                        {
                            "name": certificate_path.name,
                            "digest": {
                                "sha256": MODULE.file_sha256(certificate_path).removeprefix(
                                    "sha256:"
                                )
                            },
                        }
                    ],
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {
                        "buildDefinition": {
                            "buildType": (
                                "https://actions.github.io/buildtypes/workflow/v1"
                            ),
                            "externalParameters": {
                                "workflow": {
                                    "ref": ref,
                                    "repository": "https://github.com/OpenAdaptAI/openadapt-cloud",
                                    "path": ".github/workflows/execute-live-acceptance.yml",
                                }
                            },
                            "internalParameters": {
                                "github": {
                                    "event_name": "workflow_dispatch",
                                    "repository_id": "123456",
                                    "repository_owner_id": "7890",
                                    "runner_environment": "github-hosted",
                                }
                            },
                            "resolvedDependencies": [
                                {
                                    "uri": (
                                        "git+https://github.com/OpenAdaptAI/"
                                        f"openadapt-cloud@{ref}"
                                    ),
                                    "digest": {"gitCommit": commit},
                                }
                            ],
                        },
                        "runDetails": {
                            "builder": {"id": MODULE.CLOUD_CERTIFICATE_IDENTITY},
                            "metadata": {
                                "invocationId": (
                                    "https://github.com/OpenAdaptAI/openadapt-cloud/"
                                    "actions/runs/123456789/attempts/1"
                                )
                            },
                        },
                    },
                },
            },
        }
    ]


def _reviewed_gh_run(
    certificate_path: Path,
    *,
    provenance: list[dict[str, object]] | None = None,
    verification_returncode: int = 0,
    verification_stdout: str | None = None,
    verification_stderr: str = "",
    version: str = MODULE.REVIEWED_GITHUB_CLI_VERSION,
    version_returncode: int = 0,
    version_stdout: str | None = None,
    version_stderr: str = "",
    observed: list[list[str]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if observed is not None:
            observed.append(command)
        if command == ["gh", "--version"]:
            version_output = version_stdout
            if version_output is None:
                version_output = (
                    f"gh version {version} (2025-02-11)\n"
                    f"https://github.com/cli/cli/releases/tag/v{version}\n"
                )
            return subprocess.CompletedProcess(
                command,
                version_returncode,
                stdout=version_output,
                stderr=version_stderr,
            )
        stdout = verification_stdout
        if stdout is None:
            stdout = json.dumps(provenance or _verified_provenance(certificate_path))
        return subprocess.CompletedProcess(
            command,
            verification_returncode,
            stdout=stdout,
            stderr=verification_stderr,
        )

    return run


def test_derives_only_the_scoped_claim_and_counts_retained_rows() -> None:
    result = _derive()

    assert result["verdict"] == "accepted"
    assert result["evidence_class"] == "qualified_browser_production_acceptance"
    assert result["claim_scope"] == "qualified_browser_workflow_on_bound_environment"
    assert result["claim_limit"] == "not_general_product_production_readiness"
    task_id_sha256 = (
        "sha256:65288c82e132b7e66f264e5828b7427f7ad56121081c20a4fc662bb1a97fcf94"
    )
    condition_ids = [
        "ambiguous-target",
        "healthy-01",
        "healthy-02",
        "healthy-03",
        "idempotency-replay",
        "stale-session",
        "verifier-unavailable",
        "weak-effect-only",
        "wrong-reference",
    ]
    assert result["trial_inventory"] == {
        "task_count": 1,
        "condition_count": 9,
        "required_trial_count": 27,
        "observed_trial_count": 27,
        "trial_count": 27,
        "minimum_trials_per_condition": 3,
        "conditions": [
            {
                "task_id_sha256": task_id_sha256,
                "condition_id_sha256": MODULE.privacy_safe_campaign_label_sha256(
                    "qualification condition",
                    _campaign()["campaign_id"],
                    condition_id,
                ),
                "required_trial_count": 3,
                "observed_trial_count": 3,
            }
            for condition_id in condition_ids
        ],
        "excluded_trial_count": 0,
    }
    assert result["derived_outcomes"] == {
        "verified": 12,
        "safe_halt": 15,
        "silent_incorrect_success": 0,
        "over_halt": 0,
        "wrong_record": 0,
        "duplicate_effect": 0,
        "collateral_effect": 0,
        "uncertain_delivery": 0,
        "platform_failure": 0,
        "operator_intervention": 0,
        "healthy_path_model_call": 0,
    }
    qualification = _certificate()["qualification"]
    for field in (
        "campaign_contract_sha256",
        "campaign_outcomes_sha256",
        "oracle_contract_sha256",
        "task_count",
        "condition_count",
        "required_trial_count",
        "observed_trial_count",
    ):
        assert result["bindings"][field] == qualification[field]
    assert set(result["reliability"].values()) == {0}
    assert result["bindings"]["evidence_runner_signer_sha256"] == (
        _certificate()["identities"]["evidence_runner_signer_sha256"]
    )
    assert "managed_runner_signer_sha256" not in result["bindings"]
    rendered = json.dumps(result, sort_keys=True)
    assert "execute-acceptance" not in rendered
    assert all(condition_id not in rendered for condition_id in condition_ids)
    assert "production_acceptance" not in result
    assert "class" not in result


def test_fixture_digests_are_exact() -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()

    assert MODULE.canonical_sha256(admission) == certificate["qualification"][
        "qualification_admission_sha256"
    ]
    assert MODULE.canonical_sha256(campaign["qualification_contract"]) == certificate[
        "qualification"
    ]["campaign_contract_sha256"]
    assert MODULE.canonical_sha256(campaign) == certificate["qualification"][
        "campaign_artifact_sha256"
    ]
    assert MODULE.canonical_sha256(campaign["oracle_contract"]) == certificate[
        "qualification"
    ]["oracle_contract_sha256"]


@pytest.mark.parametrize("field,value", [("production_acceptance", True), ("class", "production")])
def test_rejects_author_declared_production_labels(field: str, value: object) -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign[field] = value
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="campaign keys differ"):
        _derive(certificate, campaign, admission)


def test_rejects_author_declared_summary_counts() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["summary"] = {
        "trial_count": 999,
        "silent_incorrect_success_count": 0,
        "over_halt_count": 0,
    }
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="campaign keys differ"):
        _derive(certificate, campaign, admission)


def test_rejects_count_only_condition_without_trial_rows() -> None:
    certificate = _certificate()
    campaign = _campaign()
    condition = campaign["conditions"][0]
    condition["trials"] = {"trial_count": 3, "verified_count": 3}
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="task inventory is malformed"):
        _derive(certificate, campaign, admission)


def test_rejects_fewer_than_three_trials_in_one_condition() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["conditions"][0]["trials"] = campaign["conditions"][0]["trials"][:2]
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="fewer than 3 retained trials"):
        _derive(certificate, campaign, admission)


def test_rejects_duplicate_attempt_ids() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 1)["attempt_id_sha256"] = _trial(campaign, 0, 0)[
        "attempt_id_sha256"
    ]
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="attempt ID is duplicate"):
        _derive(certificate, campaign, admission)


def test_rejects_reused_signed_receipt_envelope() -> None:
    certificate = _certificate()
    campaign = _campaign()
    first = _trial(campaign, 0, 0)
    second = _trial(campaign, 0, 1)
    second["runner_receipt_sha256"] = first["runner_receipt_sha256"]
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="reuses a receipt envelope"):
        _derive(certificate, campaign, admission)


def test_rejects_digest_only_row_without_signed_receipt_body() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 1)["runner_receipt_sha256"] = "0" * 64
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="receipt body is missing"):
        _derive(certificate, campaign, admission)


@pytest.mark.parametrize(
    "replacement",
    [
        {
            "id": "no_wrong_or_duplicate_effect",
            "holds": True,
            "observations": 0,
            "violations": 0,
        },
        {
            "id": "no_wrong_or_duplicate_effect",
            "holds": False,
            "observations": 27,
            "violations": 1,
        },
    ],
)
def test_rejects_vacuous_or_false_invariants(replacement: dict[str, object]) -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["invariants"][0] = replacement
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="differs from retained trials"):
        _derive(certificate, campaign, admission)


def test_rejects_invented_invariant_name() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["invariants"][0]["id"] = "author_says_everything_is_good"
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="invariant is not derived"):
        _derive(certificate, campaign, admission)


def test_rejects_campaign_digest_mismatch() -> None:
    campaign = _campaign()
    campaign["generated_at"] = "2026-08-18T11:56:00Z"

    with pytest.raises(MODULE.AcceptanceError, match="campaign artifact digest differs"):
        _derive(campaign=campaign)


def test_rejects_qualification_contract_digest_mismatch() -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    certificate["qualification"]["campaign_contract_sha256"] = "sha256:" + "3a" * 32
    qualification = certificate["qualification"]
    qualification["campaign_artifact_sha256"] = MODULE.canonical_sha256(campaign)

    with pytest.raises(MODULE.AcceptanceError, match="contract digest differs"):
        _derive(certificate, campaign, admission)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda contract: contract["cases"][3].__setitem__(
                "replays_case", "healthy-02"
            ),
            "replay contract is not reviewed",
        ),
        (
            lambda contract: contract["fault_case_receipts"].__setitem__(
                "required", False
            ),
            "fault-receipt contract is not reviewed",
        ),
        (
            lambda contract: contract["cleanup"].__setitem__("required", False),
            "cleanup contract is not reviewed",
        ),
        (
            lambda contract: contract.__setitem__(
                "post_campaign_invariant", "cleanup was probably successful"
            ),
            "post-campaign invariant is not reviewed",
        ),
    ],
)
def test_rejects_weakened_replay_fault_or_cleanup_contract(
    mutation: object,
    expected: str,
) -> None:
    certificate = _certificate()
    campaign = _campaign()
    mutation(campaign["qualification_contract"])
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        _derive(certificate, campaign, admission)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflow_version_id", "77777777-7777-4777-8777-777777777777"),
        ("runtime_validation_id", "88888888-8888-4888-8888-888888888888"),
        ("bundle_artifact_sha256", "0" * 64),
    ],
)
def test_rejects_trial_binding_mismatches(field: str, value: str) -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 0)[field] = value
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="differs from admission"):
        _derive(certificate, campaign, admission)


def test_rejects_unknown_certificate_signer_repository() -> None:
    certificate = _certificate()
    certificate["identities"]["producer"]["repository"] = "attacker/openadapt-cloud"

    with pytest.raises(MODULE.AcceptanceError, match="producer identity"):
        _derive(certificate)


def test_rejects_certificate_identity_policy_drift() -> None:
    certificate = _certificate()
    certificate["identities"]["producer"]["source_ref"] = "refs/heads/unreviewed"

    with pytest.raises(MODULE.AcceptanceError, match="producer identity"):
        _derive(certificate)

    certificate = _certificate()
    certificate["identities"]["verifier"]["hosted_runner_required"] = False

    with pytest.raises(MODULE.AcceptanceError, match="verifier identity"):
        _derive(certificate)


def test_rejects_unknown_attestation_workflow_or_ref() -> None:
    attestation = _attestation()
    attestation["certificate_identity"] = (
        "https://github.com/OpenAdaptAI/openadapt-cloud/"
        ".github/workflows/execute-live-acceptance.yml@refs/heads/attacker"
    )

    with pytest.raises(MODULE.AcceptanceError, match="attestation identity"):
        _derive(attestation=attestation)


def test_rejects_expired_object_lock_retention() -> None:
    expired = datetime(2027, 8, 18, 12, 2, tzinfo=timezone.utc)

    with pytest.raises(MODULE.AcceptanceError, match="has expired"):
        _derive(now=expired)


def test_rejects_retention_dated_before_acceptance() -> None:
    certificate = _certificate()
    certificate["retention"]["retained_at"] = "2026-08-18T11:59:59.000Z"

    with pytest.raises(MODULE.AcceptanceError, match="chronology is invalid"):
        _derive(certificate)


def test_rejects_retention_dated_in_the_future() -> None:
    certificate = _certificate()
    certificate["retention"]["retained_at"] = "2099-01-01T00:00:00.000Z"

    with pytest.raises(MODULE.AcceptanceError, match="dated in the future"):
        _derive(certificate)



@pytest.mark.parametrize(
    "route",
    [
        # The retired route. GitHub artifact attestations are unavailable to a
        # private repository on the Free, Pro, and Team plans, so no genuine
        # Cloud certificate can carry this value.
        "github-artifact-attestation-v4",
        "github-artifact-attestation-v5",
        "sigstore-public-good-slsa-provenance-v2",
        "unreviewed-v1",
        "",
    ],
)
def test_rejects_any_retention_provenance_but_the_reviewed_route(route: str) -> None:
    certificate = _certificate()
    certificate["retention"]["provenance_attestation"] = route

    with pytest.raises(MODULE.AcceptanceError, match="provenance attestation version"):
        _derive(certificate)


def test_the_reviewed_retention_route_is_the_public_good_instance() -> None:
    assert MODULE.RETENTION_PROVENANCE_ROUTE == "sigstore-public-good-slsa-provenance-v1"
    assert (
        MODULE.production_acceptance_policy()["required_retention_provenance"]
        == MODULE.RETENTION_PROVENANCE_ROUTE
    )
    assert _certificate()["retention"]["provenance_attestation"] == (
        MODULE.RETENTION_PROVENANCE_ROUTE
    )


def test_rejects_unhashed_workflow_binding() -> None:
    certificate = _certificate()
    certificate["qualification"]["workflow_digest"] = "workflow-v1"

    with pytest.raises(MODULE.AcceptanceError, match="must be a sha256 digest"):
        _derive(certificate)


@pytest.mark.parametrize(
    "image",
    [
        "python:3.11-slim",
        "python:3.11-slim@sha256:abc",
        "python:3.11-slim@sha256:" + "g" * 64,
        "@sha256:" + "4" * 64,
        "python:3.11@sha256:" + "4" * 64 + "-suffix",
        "Python:3.11-slim@sha256:" + "4" * 64,
        "python:3.11-SLIM@sha256:" + "4" * 64,
        "python :3.11-slim@sha256:" + "4" * 64,
    ],
)
def test_rejects_malformed_browser_image_digest(image: str) -> None:
    certificate = _certificate()
    certificate["product"]["managed_runtime"]["browser_base_image"] = image

    with pytest.raises(MODULE.AcceptanceError, match="browser image is not digest-pinned"):
        _derive(certificate)


@pytest.mark.parametrize(
    "image",
    [
        "Python:3.11-slim@sha256:" + "4" * 64,
        "python:3.11-SLIM@sha256:" + "4" * 64,
        "python:3.11-slim@sha256:" + "A" * 64,
        "python:3.11-slim @sha256:" + "4" * 64,
    ],
)
def test_rejects_noncanonical_browser_image_in_admitted_runtime(image: str) -> None:
    admission = _admission()
    admission["payload"]["evidence_identity"]["runtime_build_identity"][
        "managed_browser"
    ]["browser_base_image"] = image

    with pytest.raises(MODULE.AcceptanceError, match="browser image is not digest-pinned"):
        _derive(admission=admission)


@pytest.mark.parametrize(
    "value",
    [
        "11111111-1111-1111-8111-111111111111",
        "88888888-8888-8888-8888-888888888888",
    ],
)
def test_accepts_canonical_rfc_uuid_versions_one_through_eight(value: str) -> None:
    assert MODULE._canonical_uuid(value, "test UUID") == value


@pytest.mark.parametrize(
    "value",
    [
        "11111111-1111-4111-8111-11111111111A",
        "11111111-1111-0111-8111-111111111111",
        "11111111-1111-9111-8111-111111111111",
        "11111111-1111-4111-7111-111111111111",
    ],
)
def test_rejects_uuid_outside_cloud_canonical_rfc_grammar(value: str) -> None:
    with pytest.raises(MODULE.AcceptanceError, match="must be a canonical UUID"):
        MODULE._canonical_uuid(value, "test UUID")


def test_rejects_unknown_certificate_fields() -> None:
    certificate = _certificate()
    certificate["production_ready"] = True

    with pytest.raises(MODULE.AcceptanceError, match="certificate keys differ"):
        _derive(certificate)


def test_rejects_declared_contract_boolean_that_is_not_verified() -> None:
    certificate = _certificate()
    certificate["contracts"]["independent_effect_verified"] = False

    with pytest.raises(MODULE.AcceptanceError, match="contracts are not all verified"):
        _derive(certificate)


def test_rejects_non_idempotent_accepted_responses() -> None:
    certificate = _certificate()
    certificate["transaction"]["duplicate_response_sha256"] = "sha256:" + "0" * 64

    with pytest.raises(MODULE.AcceptanceError, match="different digests"):
        _derive(certificate)


def test_rejects_certificate_that_omits_a_hashed_runner_link() -> None:
    certificate = _certificate()
    del certificate["transaction"]["request_sha256"]

    with pytest.raises(MODULE.AcceptanceError, match="transaction keys differ"):
        _derive(certificate)


def test_rejects_missing_or_invalid_runner_permit_binding() -> None:
    certificate = _certificate()
    del certificate["transaction"]["runner_permit_sha256"]

    with pytest.raises(MODULE.AcceptanceError, match="transaction keys differ"):
        _derive(certificate)

    certificate = _certificate()
    certificate["transaction"]["runner_permit_sha256"] = "permit-1"

    with pytest.raises(MODULE.AcceptanceError, match="must be a sha256 digest"):
        _derive(certificate)


def test_rejects_certificate_with_a_different_admission_digest() -> None:
    certificate = _certificate()
    certificate["qualification"]["qualification_admission_sha256"] = "sha256:" + "9a" * 32

    with pytest.raises(MODULE.AcceptanceError, match="admission digest differs"):
        _derive(certificate)


@pytest.mark.parametrize(
    "field",
    [
        "admission_id_sha256",
        "campaign_id_sha256",
        "runtime_validation_id_sha256",
        "workflow_version_id_sha256",
    ],
)
def test_rejects_public_id_digest_not_recomputed_from_admission(field: str) -> None:
    certificate = _certificate()
    certificate["qualification"][field] = "sha256:" + "0" * 64

    with pytest.raises(MODULE.AcceptanceError, match="retained admission"):
        _derive(certificate)


@pytest.mark.parametrize("field", ["organization_id_sha256", "workflow_id_sha256"])
def test_rejects_public_identity_digest_not_recomputed_from_admission(field: str) -> None:
    certificate = _certificate()
    certificate["identities"][field] = "sha256:" + "0" * 64

    with pytest.raises(MODULE.AcceptanceError, match="retained admission"):
        _derive(certificate)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("campaign_outcomes_sha256", "sha256:" + "0" * 64),
        ("oracle_contract_sha256", "sha256:" + "0" * 64),
        ("task_count", 2),
        ("condition_count", 10),
        ("required_trial_count", 26),
        ("observed_trial_count", 28),
    ],
)
def test_rejects_one_field_public_result_binding_substitution(
    field: str,
    replacement: str | int,
) -> None:
    certificate = _certificate()
    certificate["qualification"][field] = replacement

    with pytest.raises(MODULE.AcceptanceError, match="differs"):
        _derive(certificate)


@pytest.mark.parametrize(
    "field,label_kind,replacement",
    [
        ("task_id_sha256", "qualification task", "execute-acceptance-mutated"),
        (
            "condition_id_sha256",
            "qualification condition",
            "healthy-01-mutated",
        ),
    ],
)
def test_one_field_task_condition_identity_mutation_changes_result_digest(
    field: str,
    label_kind: str,
    replacement: str,
) -> None:
    result = _derive()
    original_digest = MODULE.canonical_sha256(result)
    mutated = copy.deepcopy(result)
    mutated["trial_inventory"]["conditions"][0][field] = (
        MODULE.privacy_safe_campaign_label_sha256(
            label_kind,
            _campaign()["campaign_id"],
            replacement,
        )
    )

    assert MODULE.canonical_sha256(mutated) != original_digest


@pytest.mark.parametrize("failure", MODULE._FAILURE_TAXONOMY)
def test_one_field_failure_taxonomy_count_mutation_changes_result_digest(
    failure: str,
) -> None:
    result = _derive()
    original_digest = MODULE.canonical_sha256(result)
    mutated = copy.deepcopy(result)
    mutated["derived_outcomes"][failure] += 1

    assert MODULE.canonical_sha256(mutated) != original_digest


def test_rejects_unknown_signer_fingerprint_scheme() -> None:
    certificate = _certificate()
    certificate["identities"]["signer_fingerprint_scheme"] = "opaque-key-id"

    with pytest.raises(MODULE.AcceptanceError, match="fingerprint scheme"):
        _derive(certificate)


def test_rejects_unverified_object_lock_receipt() -> None:
    certificate = _certificate()
    certificate["retention"]["object_lock_verified"] = False

    with pytest.raises(MODULE.AcceptanceError, match="object_lock_verified"):
        _derive(certificate)


def test_file_import_stays_refused_until_private_export_is_approved(tmp_path: Path) -> None:
    with pytest.raises(MODULE.AcceptanceError, match="pending an approved private-export"):
        MODULE.import_files(
            tmp_path / "certificate.json",
            tmp_path / "campaign.json",
            tmp_path / "admission.json",
            tmp_path / "bundle.jsonl",
            "f" * 40,
            trusted_admission_signers={},
        )


def test_cli_refuses_before_it_reads_private_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "derived.json"
    result = MODULE.main(
        [
            "--certificate",
            str(tmp_path / "missing-certificate.json"),
            "--campaign",
            str(tmp_path / "missing-campaign.json"),
            "--qualification-admission",
            str(tmp_path / "missing-admission.json"),
            "--attestation-bundle",
            str(tmp_path / "missing-bundle.jsonl"),
            "--expected-cloud-source-commit",
            "f" * 40,
            "--trusted-admission-signers",
            str(FIXTURES / "qualification-admission-trust.json"),
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert "pending an approved private-export contract" in capsys.readouterr().err
    assert not output.exists()


def test_rejects_collapsed_runner_observer_signing_identities() -> None:
    certificate = _certificate()
    certificate["identities"]["evidence_runner_signer_sha256"] = certificate["identities"][
        "target_observer_signer_sha256"
    ]

    with pytest.raises(MODULE.AcceptanceError, match="signing identities are not separate"):
        _derive(certificate)


@pytest.mark.parametrize(
    "receipt_type,verdict,row_updates,expected",
    [
        (
            "runner",
            "halted",
            {"execution_outcome": "halted", "failure_class": "over_halt"},
            "over_halt",
        ),
        (
            "observer",
            "refuted",
            {"oracle_verdict": "refuted", "failure_class": "silent_incorrect_success"},
            "silent_incorrect_success",
        ),
        (
            "webhook",
            "missing",
            {"failure_class": "uncertain_delivery"},
            "uncertain_delivery",
        ),
        (
            "runner",
            "failed",
            {"execution_outcome": "failed", "failure_class": "platform_failure"},
            "platform_failure",
        ),
    ],
)
def test_rejects_failures_derived_from_signed_receipts(
    receipt_type: str,
    verdict: str,
    row_updates: dict[str, object],
    expected: str,
) -> None:
    certificate = _certificate()
    campaign = _campaign()
    _replace_receipt_verdict(
        campaign,
        receipt_type=receipt_type,
        verdict=verdict,
        trial=1,
    )
    _trial(campaign, 0, 1).update(row_updates)
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="production-acceptance failures") as error:
        _derive(certificate, campaign, admission)
    assert expected in str(error.value)


def test_rejects_contract_that_permits_model_calls_on_the_healthy_path() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["qualification_contract"]["runtime"]["openadapt_flow"][
        "healthy_path_model_call_limit"
    ] = 1
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="permits model calls"):
        _derive(certificate, campaign, admission)


def test_rejects_excluded_trials() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["excluded_trials"] = ["failed-first-attempt"]
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="excluded or hidden"):
        _derive(certificate, campaign, admission)


def test_rejects_missing_contract_condition() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["conditions"].pop()
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="every contract condition"):
        _derive(certificate, campaign, admission)


def test_result_does_not_disclose_private_admission_identifiers() -> None:
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)

    rendered = json.dumps(_derive(admission=admission), sort_keys=True)

    for key in MODULE._ADMISSION_UUID_KEYS:
        assert payload[key] not in rendered


def test_rejects_invalid_qualification_admission_signature() -> None:
    admission = _admission()
    admission["signature"] = "A" * 86 + "=="

    with pytest.raises(MODULE.AcceptanceError, match="signature is invalid"):
        _derive(admission=admission)


def test_rejects_unknown_or_revoked_admission_signer() -> None:
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)
    issuer = payload["issuer"]
    assert isinstance(issuer, dict)
    key_id = issuer["key_id"]
    assert isinstance(key_id, str)

    with pytest.raises(MODULE.AcceptanceError, match="signer registry keys differ"):
        _derive(admission=admission, trusted_admission_signers={})
    with pytest.raises(MODULE.AcceptanceError, match="signer is revoked"):
        _derive(admission=admission, revoked_admission_signer_key_ids={key_id})


def test_rejects_revoked_admission_id() -> None:
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)
    admission_id = payload["admission_id"]
    assert isinstance(admission_id, str)

    with pytest.raises(MODULE.AcceptanceError, match="admission is revoked"):
        _derive(admission=admission, revoked_admission_ids={admission_id})


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("workflow", "attacker/repo/.github/workflows/admit.yml", "workflow is not approved"),
        ("ref", "refs/heads/feature@" + "a" * 40, "ref is not approved"),
    ],
)
def test_rejects_unapproved_admission_provenance(
    field: str,
    value: str,
    expected: str,
) -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    issuer = admission["payload"]["issuer"]
    issuer[field] = value
    _sign_admission_payload(admission)
    certificate["qualification"]["qualification_admission_sha256"] = (
        MODULE.canonical_sha256(admission)
    )

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        _derive(certificate, campaign, admission)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("expires_at", "2026-08-18T12:30:00Z", "has expired"),
        ("not_before", "2026-08-18T11:58:00.000Z", "whole-second UTC"),
    ],
)
def test_rejects_invalid_admission_time_contract(
    field: str,
    value: str,
    expected: str,
) -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    admission["payload"][field] = value
    _sign_admission_payload(admission)
    certificate["qualification"]["qualification_admission_sha256"] = (
        MODULE.canonical_sha256(admission)
    )

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        _derive(certificate, campaign, admission)


def test_rejects_future_issued_admission() -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    payload = admission["payload"]
    payload["issued_at"] = "2026-08-18T13:06:00Z"
    payload["not_before"] = "2026-08-18T13:05:00Z"
    payload["expires_at"] = "2026-09-17T13:06:00Z"
    _sign_admission_payload(admission)
    certificate["qualification"]["qualification_admission_sha256"] = (
        MODULE.canonical_sha256(admission)
    )

    with pytest.raises(MODULE.AcceptanceError, match="future-issued"):
        _derive(certificate, campaign, admission)


def test_rejects_author_supplied_failure_class_not_supported_by_receipts() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 0)["failure_class"] = "wrong_record"
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="failure class differs"):
        _derive(certificate, campaign, admission)


def test_rejects_invalid_normalized_receipt_signature() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _replace_receipt_envelope(
        campaign,
        receipt_type="runner",
        mutate=lambda envelope: envelope.__setitem__("signature", "A" * 86 + "=="),
    )
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="signature is invalid"):
        _derive(certificate, campaign, admission)


def test_rejects_receipt_projection_that_differs_from_its_row() -> None:
    certificate = _certificate()
    campaign = _campaign()

    def mutate(envelope: dict[str, object]) -> None:
        projection = envelope["verified_projection"]
        assert isinstance(projection, dict)
        projection["attempt_id_sha256"] = "0" * 64

    _replace_receipt_envelope(campaign, receipt_type="observer", mutate=mutate)
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="projection differs from its row"):
        _derive(certificate, campaign, admission)


def test_rejects_unreferenced_receipt_envelope() -> None:
    certificate = _certificate()
    campaign = _campaign()
    envelopes = campaign["receipt_envelopes"]
    assert isinstance(envelopes, dict)
    extra = copy.deepcopy(next(iter(envelopes.values())))
    extra["signature"] = "A" * 86 + "=="
    envelopes[MODULE.canonical_sha256(extra).removeprefix("sha256:")] = extra
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="unreferenced or hidden"):
        _derive(certificate, campaign, admission)


def test_rejects_receipt_authority_key_id_not_derived_from_public_key() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["authority_contract"]["runner"]["key_id"] = "qe-ed25519-" + "0" * 16
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="key ID differs from its key"):
        _derive(certificate, campaign, admission)


def test_rejects_collapsed_normalized_receipt_authorities() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["authority_contract"]["observer"] = copy.deepcopy(
        campaign["authority_contract"]["runner"]
    )
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="not separated by type"):
        _derive(certificate, campaign, admission)


def test_rejects_reused_source_evidence_behind_distinct_receipts() -> None:
    certificate = _certificate()
    campaign = _campaign()
    first = _trial(campaign, 0, 0)
    first_digest = first["runner_receipt_sha256"]
    first_envelope = campaign["receipt_envelopes"][first_digest]
    source_digest = first_envelope["source_artifact_sha256"]

    def mutate(envelope: dict[str, object]) -> None:
        envelope["source_artifact_sha256"] = source_digest
        projection = envelope["verified_projection"]
        assert isinstance(projection, dict)
        projection["evidence_sha256"] = source_digest
        facts = projection["facts"]
        assert isinstance(facts, dict)
        counter = facts["model_call_counter"]
        assert isinstance(counter, dict)
        counter["report_sha256"] = source_digest

    _replace_receipt_envelope(
        campaign,
        receipt_type="runner",
        mutate=mutate,
        resign=True,
        trial=1,
    )
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="reuses a source evidence artifact"):
        _derive(certificate, campaign, admission)


def test_rejects_noncontiguous_trial_index() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 1)["trial_index"] = 9
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="one-based and contiguous"):
        _derive(certificate, campaign, admission)


def test_rejects_boolean_trial_index() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 0, 0)["trial_index"] = True
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="one-based and contiguous"):
        _derive(certificate, campaign, admission)


def test_rejects_missing_fault_or_cleanup_evidence() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _trial(campaign, 4, 0)["fault_receipt_sha256"] = None
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="fault receipt is required"):
        _derive(certificate, campaign, admission)


def test_rejects_embedded_admission_trust_registry() -> None:
    certificate = _certificate()
    campaign = _campaign()
    campaign["trusted_admission_signers"] = _admission_trust()
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="campaign keys differ"):
        _derive(certificate, campaign, admission)


def test_verifier_uses_exact_repository_workflow_ref_and_hosted_runner_policy(
    tmp_path: Path,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    observed: list[list[str]] = []
    run = _reviewed_gh_run(certificate_path, observed=observed)

    result = MODULE.verify_github_attestation(
        certificate_path,
        bundle_path,
        "f" * 40,
        run=run,
    )

    assert observed[0] == ["gh", "--version"]
    verify_command = observed[1]
    assert verify_command[:3] == ["gh", "attestation", "verify"]
    assert verify_command[verify_command.index("--repo") + 1] == (
        "OpenAdaptAI/openadapt-cloud"
    )
    assert verify_command[verify_command.index("--cert-identity") + 1].endswith(
        "execute-live-acceptance.yml@refs/heads/main"
    )
    assert verify_command[verify_command.index("--cert-oidc-issuer") + 1] == (
        MODULE.GITHUB_OIDC_ISSUER
    )
    assert verify_command[verify_command.index("--hostname") + 1] == "github.com"
    assert "--source-digest" not in verify_command
    assert "--source-ref" not in verify_command
    assert "--deny-self-hosted-runners" in verify_command
    # The Cloud certificate is signed on the Sigstore public-good instance, so
    # the flag that refuses that instance must stay off. The private-repository
    # binding is carried by sourceRepositoryVisibilityAtSigning instead.
    assert "--no-public-good" not in verify_command
    assert result["bundle_sha256"] == MODULE.file_sha256(bundle_path)


def test_verifier_refuses_failed_or_empty_attestation(tmp_path: Path) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(MODULE.AcceptanceError, match="invalid: bad signature"):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(
                certificate_path,
                verification_returncode=1,
                verification_stdout="",
                verification_stderr="bad signature",
            ),
        )

    with pytest.raises(MODULE.AcceptanceError, match="must return one verified statement"):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, verification_stdout="[]"),
        )


@pytest.mark.parametrize(
    "version,returncode,stdout,stderr",
    [
        ("2.66.0", 0, None, ""),
        ("2.68.0", 0, None, ""),
        (MODULE.REVIEWED_GITHUB_CLI_VERSION, 1, None, ""),
        (MODULE.REVIEWED_GITHUB_CLI_VERSION, 0, None, "unexpected warning"),
        (
            MODULE.REVIEWED_GITHUB_CLI_VERSION,
            0,
            "gh version 2.67.0 (2025-02-11)\nhttps://attacker.example/v2.67.0\n",
            "",
        ),
        (
            MODULE.REVIEWED_GITHUB_CLI_VERSION,
            0,
            (
                "gh version 2.67.0 (2025-02-11)\n"
                "https://github.com/cli/cli/releases/tag/v2.67.0\n"
                "unreviewed extra line\n"
            ),
            "",
        ),
    ],
)
def test_verifier_refuses_an_unreviewed_github_cli(
    tmp_path: Path,
    version: str,
    returncode: int,
    stdout: str | None,
    stderr: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(MODULE.AcceptanceError, match="requires reviewed gh version"):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(
                certificate_path,
                version=version,
                version_returncode=returncode,
                version_stdout=stdout,
                version_stderr=stderr,
            ),
        )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda value: value[0]["verificationResult"]["signature"]["certificate"].__setitem__(
                "sourceRepositoryDigest", "0" * 40
            ),
            "sourceRepositoryDigest is not approved",
        ),
        (
            lambda value: value[0]["verificationResult"]["signature"]["certificate"].__setitem__(
                "sourceRepositoryRef", "refs/heads/unreviewed"
            ),
            "sourceRepositoryRef is not approved",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"]["predicate"][
                "buildDefinition"
            ]["resolvedDependencies"][0]["digest"].__setitem__("gitCommit", "0" * 40),
            "resolved source commit",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"]["subject"][0][
                "digest"
            ].__setitem__("sha256", "0" * 64),
            "exactly one subject for the certificate bytes",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"]["predicate"][
                "buildDefinition"
            ]["internalParameters"]["github"].__setitem__(
                "runner_environment", "self-hosted"
            ),
            "runner is not GitHub-hosted",
        ),
        (
            lambda value: value[0]["verificationResult"]["statement"]["predicate"][
                "runDetails"
            ]["builder"].__setitem__("id", "https://github.com/attacker/workflow@main"),
            "builder is not the reviewed Cloud workflow",
        ),
    ],
)
def test_verifier_refuses_unapproved_or_malformed_provenance(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    mutation(provenance)

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, provenance=provenance),
        )


def test_verifier_requires_an_external_approved_cloud_commit(tmp_path: Path) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(MODULE.AcceptanceError, match="not the approved commit"):
        MODULE.verify_github_attestation(certificate_path, bundle_path, "0" * 40)


def test_verifier_allows_other_subjects_but_only_one_matching_certificate(
    tmp_path: Path,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    subjects = provenance[0]["verificationResult"]["statement"]["subject"]
    subjects.append({"name": "other.json", "digest": {"sha256": "0" * 64}})

    run = _reviewed_gh_run(certificate_path, provenance=provenance)

    MODULE.verify_github_attestation(certificate_path, bundle_path, "f" * 40, run=run)
    subjects.append(copy.deepcopy(subjects[0]))

    with pytest.raises(MODULE.AcceptanceError, match="exactly one subject"):
        MODULE.verify_github_attestation(certificate_path, bundle_path, "f" * 40, run=run)


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("runnerEnvironment", "self-hosted", "runnerEnvironment is not approved"),
        (
            "runInvocationURI",
            "https://github.com/OpenAdaptAI/openadapt-cloud/actions/runs/9/attempts/9",
            "runInvocationURI is not approved",
        ),
        ("buildConfigDigest", "0" * 40, "buildConfigDigest is not approved"),
        (
            "sourceRepositoryVisibilityAtSigning",
            "public",
            "sourceRepositoryVisibilityAtSigning is not approved",
        ),
    ],
)
def test_verifier_rejects_real_gh_certificate_policy_drift(
    tmp_path: Path,
    field: str,
    value: str,
    expected: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    provenance[0]["verificationResult"]["signature"]["certificate"][field] = value

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, provenance=provenance),
        )


def test_verifier_binds_observed_time_to_record_issuance(tmp_path: Path) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    provenance[0]["verificationResult"]["verifiedTimestamps"][0][
        "timestamp"
    ] = "2026-08-18T11:59:59.000Z"

    with pytest.raises(MODULE.AcceptanceError, match="not bound to record issuance"):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, provenance=provenance),
        )


def _time(value: str) -> dict[str, str]:
    return {"type": "Tlog", "uri": MODULE.PUBLIC_TRANSPARENCY_LOG, "timestamp": value}


def _authority(value: str) -> dict[str, str]:
    return {
        "type": "TimestampAuthority",
        "uri": MODULE.PUBLIC_TIMESTAMP_AUTHORITY,
        "timestamp": value,
    }


@pytest.mark.parametrize(
    "timestamps,expected",
    [
        # No public transparency-log time at all. A timestamp authority alone
        # proves when, not that the signature is in an append-only public log.
        ([_authority("2026-08-18T12:00:05Z")], "one public transparency-log time"),
        # The GitHub private instance. Its timestamp authority is not the
        # public-good one, and it publishes to no log.
        (
            [
                {
                    "type": "TimestampAuthority",
                    "uri": "timestamp.githubapp.com",
                    "timestamp": "2026-08-18T12:00:05Z",
                }
            ],
            "unapproved timestamp observer",
        ),
        # A clock reading is not an observer.
        (
            [{"type": "CurrentTime", "uri": "", "timestamp": "2026-08-18T12:00:05Z"}],
            "unapproved timestamp observer",
        ),
        # An unknown log, even one that calls itself Tlog.
        (
            [
                {
                    "type": "Tlog",
                    "uri": "https://rekor.example.test",
                    "timestamp": "2026-08-18T12:00:05Z",
                }
            ],
            "unapproved timestamp observer",
        ),
        # Two log times, so the run cannot be pinned to one public entry.
        (
            [_time("2026-08-18T12:00:05Z"), _time("2026-08-18T12:00:06Z")],
            "one public transparency-log time",
        ),
        # Two authority times alongside the log time.
        (
            [
                _time("2026-08-18T12:00:05Z"),
                _authority("2026-08-18T12:00:04Z"),
                _authority("2026-08-18T12:00:03Z"),
            ],
            "one public transparency-log time",
        ),
        # An authority time outside the issuance window, while the log time is
        # inside it. Every observed time must bind, not just the first.
        (
            [_time("2026-08-18T12:00:05Z"), _authority("2026-08-18T13:30:00Z")],
            "not bound to record issuance",
        ),
        ([], "no verified observed timestamp"),
    ],
)
def test_verifier_requires_one_public_transparency_log_time(
    tmp_path: Path,
    timestamps: list[dict[str, str]],
    expected: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    provenance[0]["verificationResult"]["verifiedTimestamps"] = timestamps

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, provenance=provenance),
        )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (
            lambda value: value.__setitem__("unreviewed", "hidden"),
            "GitHub verified timestamp keys differ",
        ),
        (
            lambda value: value.__setitem__("timestamp", "2026-08-18T12:00:05"),
            "has no timezone",
        ),
        (
            lambda value: value.__setitem__("timestamp", "not-a-time"),
            "is invalid",
        ),
        (
            lambda value: value.__setitem__("timestamp", 1755518405),
            "is invalid",
        ),
    ],
)
def test_verifier_refuses_noncanonical_github_timestamp_objects(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    timestamp = provenance[0]["verificationResult"]["verifiedTimestamps"][0]
    mutation(timestamp)

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_github_attestation(
            certificate_path,
            bundle_path,
            "f" * 40,
            run=_reviewed_gh_run(certificate_path, provenance=provenance),
        )


@pytest.mark.parametrize(
    "observed",
    [
        # Exactly what `gh attestation verify --format json` emits: Go renders a
        # time.Time in the runner's own location and drops zero sub-second
        # digits. Captured from a real verification of the sigstore 4.5.0 wheel
        # against its PyPI provenance bundle.
        "2026-08-18T08:00:05-04:00",
        "2026-08-18T12:00:05Z",
        "2026-08-18T12:00:05.000Z",
        "2026-08-18T21:00:05+09:00",
    ],
)
def test_verifier_accepts_every_rfc3339_form_the_github_cli_emits(
    tmp_path: Path,
    observed: str,
) -> None:
    certificate_path = tmp_path / "certificate.json"
    bundle_path = tmp_path / "bundle.jsonl"
    certificate_path.write_text(json.dumps(_certificate()), encoding="utf-8")
    bundle_path.write_text("{}\n", encoding="utf-8")
    provenance = _verified_provenance(certificate_path)
    provenance[0]["verificationResult"]["verifiedTimestamps"][0]["timestamp"] = observed

    result = MODULE.verify_github_attestation(
        certificate_path,
        bundle_path,
        "f" * 40,
        run=_reviewed_gh_run(certificate_path, provenance=provenance),
    )

    assert result["repository"] == MODULE.CLOUD_REPOSITORY


@pytest.mark.parametrize(
    "section,field,value",
    [
        (("product", "flow"), "version", "9.9.9"),
        (("product", "flow"), "release_commit", "b" * 40),
        (("product", "flow"), "wheel_sha256", "sha256:" + "b" * 64),
        (
            ("product", "managed_runtime"),
            "manifest_sha256",
            "sha256:" + "b" * 64,
        ),
        (("product", "managed_runtime"), "runner_build", "different-runner"),
        (
            ("product", "managed_runtime"),
            "runner_artifact_sha256",
            "sha256:" + "b" * 64,
        ),
        (("product", "managed_runtime"), "playwright_version", "9.9.9"),
        (
            ("product", "managed_runtime"),
            "browser_base_image",
            "python:3.12@sha256:" + "b" * 64,
        ),
        (("qualification",), "workflow_digest", "sha256:" + "b" * 64),
        (("qualification",), "environment_digest", "sha256:" + "b" * 64),
        (
            ("identities",),
            "evidence_runner_signer_sha256",
            "sha256:" + "b" * 64,
        ),
    ],
)
def test_rejects_each_unbound_certificate_execution_identity_field(
    section: tuple[str, ...],
    field: str,
    value: str,
) -> None:
    certificate = _certificate()
    target: dict[str, object] = certificate
    for key in section:
        next_target = target[key]
        assert isinstance(next_target, dict)
        target = next_target
    target[field] = value

    with pytest.raises(MODULE.AcceptanceError, match="execution identity differs"):
        _derive(certificate=certificate)


@pytest.mark.parametrize(
    "field",
    [
        "application_contract_sha256",
        "substrate_contract_sha256",
        "environment_contract_sha256",
        "runtime_environment_sha256",
        "runtime_contract_sha256",
        "governed_authorization_template_sha256",
        "input_policy_sha256",
        "action_policy_sha256",
        "network_policy_sha256",
        "identity_contract_sha256",
        "effect_contract_sha256",
        "operator_contract_sha256",
    ],
)
def test_rejects_each_admission_contract_outside_shared_identity(field: str) -> None:
    certificate = _certificate()
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)
    payload[field] = "9" * 64
    _sign_admission_payload(admission)
    qualification = certificate["qualification"]
    assert isinstance(qualification, dict)
    qualification["qualification_admission_sha256"] = MODULE.canonical_sha256(admission)

    with pytest.raises(MODULE.AcceptanceError, match=f"{field} differs from admission"):
        _derive(certificate=certificate, admission=admission)


def test_rejects_equal_raw_admission_and_runtime_validation_ids() -> None:
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)
    payload["admission_id"] = payload["runtime_validation_id"]

    with pytest.raises(MODULE.AcceptanceError, match="equals the runtime-validation ID"):
        _derive(admission=admission)


@pytest.mark.parametrize(
    "surface",
    ["campaign", "trial", "receipt"],
)
def test_rejects_raw_admission_id_mismatch_on_every_private_surface(surface: str) -> None:
    certificate = _certificate()
    campaign = _campaign()
    replacement = "99999999-9999-4999-8999-999999999999"
    if surface == "campaign":
        campaign["admission_id"] = replacement
    elif surface == "trial":
        _trial(campaign)["admission_id"] = replacement
    else:
        _replace_receipt_envelope(
            campaign,
            receipt_type="runner",
            mutate=lambda envelope: envelope["verified_projection"].__setitem__(
                "admission_id", replacement
            ),
            resign=True,
        )
    admission = _admission() if surface == "campaign" else _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="admission|projection"):
        _derive(certificate, campaign, admission)


@pytest.mark.parametrize("surface", ["campaign", "trial", "receipt"])
def test_rejects_shared_identity_mismatch_on_every_private_surface(surface: str) -> None:
    certificate = _certificate()
    campaign = _campaign()
    if surface == "campaign":
        campaign["evidence_identity_sha256"] = "0" * 64
    elif surface == "trial":
        _trial(campaign)["evidence_identity_sha256"] = "0" * 64
    else:
        _replace_receipt_envelope(
            campaign,
            receipt_type="observer",
            mutate=lambda envelope: envelope["verified_projection"].__setitem__(
                "evidence_identity_sha256", "0" * 64
            ),
            resign=True,
        )
    admission = _admission() if surface == "campaign" else _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="evidence identity|projection"):
        _derive(certificate, campaign, admission)


def test_rejects_mixed_old_campaign_and_new_certificate_identity() -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    payload = admission["payload"]
    assert isinstance(payload, dict)
    identity = payload["evidence_identity"]
    assert isinstance(identity, dict)
    runtime = identity["runtime_build_identity"]
    assert isinstance(runtime, dict)
    runtime["flow_version"] = "9.9.9"
    identity_digest = MODULE.evidence_identity_sha256(identity)
    certificate["product"]["flow"]["version"] = "9.9.9"
    certificate["qualification"]["evidence_identity_sha256"] = "sha256:" + identity_digest
    _sign_admission_payload(admission)
    certificate["qualification"]["qualification_admission_sha256"] = (
        MODULE.canonical_sha256(admission)
    )

    with pytest.raises(MODULE.AcceptanceError, match="campaign evidence identity differs"):
        _derive(certificate, campaign, admission)


def test_rejects_identity_that_selects_more_than_one_runtime_detail() -> None:
    admission = _admission()
    identity = admission["payload"]["evidence_identity"]
    runtime = identity["runtime_build_identity"]
    runtime["native_desktop"] = {
        "desktop_version": "1.0.0",
        "desktop_release_commit": "a" * 40,
        "desktop_artifact_sha256": "a" * 64,
        "os_family": "windows",
        "runtime_boundary_sha256": "b" * 64,
    }

    with pytest.raises(MODULE.AcceptanceError, match="exactly one substrate detail"):
        _derive(admission=admission)


def test_rejects_fixed_admission_policy_digest_mutation() -> None:
    admission = _admission()
    admission["payload"]["evidence_identity"]["admission_policy_sha256"] = "0" * 64

    with pytest.raises(MODULE.AcceptanceError, match="admission policy differs"):
        _derive(admission=admission)


def test_fixed_admission_policy_digest_matches_cloud_v2() -> None:
    assert MODULE.admission_policy_sha256() == (
        "2d3969d8c5fcfe0c0a967f775562802ca20b6598a8a3199185d0da6b7a36fb6b"
    )


def test_rejects_signer_registry_digest_and_lifetime_mutations() -> None:
    registry = _admission_trust()
    registry["revision"] = 2
    with pytest.raises(MODULE.AcceptanceError, match="registry differs from admission"):
        _derive(trusted_admission_signers=registry)

    registry = _admission_trust()
    registry["expires_at"] = "2026-08-26T11:50:01Z"
    with pytest.raises(MODULE.AcceptanceError, match="registry lifetime is invalid"):
        _derive(trusted_admission_signers=registry)


def test_rejects_signer_registry_revision_substitution_in_admission() -> None:
    admission = _admission()
    admission["payload"]["evidence_identity"][
        "qualification_signer_registry_revision"
    ] = 2

    with pytest.raises(
        MODULE.AcceptanceError,
        match="registry revision differs from admission",
    ):
        _derive(admission=admission)


def _replace_healthy_observer_effect_facts(
    campaign: dict[str, object],
    failure: str,
) -> None:
    def mutate(envelope: dict[str, object]) -> None:
        projection = envelope["verified_projection"]
        assert isinstance(projection, dict)
        facts = projection["facts"]
        assert isinstance(facts, dict)
        expected_record = facts["expected_record_id_sha256"]
        expected_transaction = facts["expected_transaction_ref_sha256"]
        effects = facts["effect_inventory"]
        assert isinstance(effects, list)
        if failure == "wrong_record":
            facts["effect_inventory"] = [
                {
                    "effect_id_sha256": "8" * 64,
                    "record_id_sha256": "9" * 64,
                    "transaction_ref_sha256": expected_transaction,
                }
            ]
            facts["derived_classifications"] = {
                "intended_effect_count": 0,
                "wrong_record_count": 1,
                "duplicate_effect_count": 0,
                "collateral_effect_count": 0,
            }
        elif failure == "duplicate_effect":
            effects.append(
                {
                    "effect_id_sha256": "8" * 64,
                    "record_id_sha256": expected_record,
                    "transaction_ref_sha256": expected_transaction,
                }
            )
            facts["derived_classifications"] = {
                "intended_effect_count": 2,
                "wrong_record_count": 0,
                "duplicate_effect_count": 1,
                "collateral_effect_count": 0,
            }
        else:
            effects.append(
                {
                    "effect_id_sha256": "8" * 64,
                    "record_id_sha256": expected_record,
                    "transaction_ref_sha256": "9" * 64,
                }
            )
            facts["derived_classifications"] = {
                "intended_effect_count": 1,
                "wrong_record_count": 0,
                "duplicate_effect_count": 0,
                "collateral_effect_count": 1,
            }

    _replace_receipt_envelope(
        campaign,
        receipt_type="observer",
        mutate=mutate,
        resign=True,
    )
    _trial(campaign)["failure_class"] = failure


@pytest.mark.parametrize(
    "failure",
    ["wrong_record", "duplicate_effect", "collateral_effect"],
)
def test_rejects_each_effect_failure_derived_from_signed_inventory(failure: str) -> None:
    certificate = _certificate()
    campaign = _campaign()
    _replace_healthy_observer_effect_facts(campaign, failure)
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="production-acceptance failures") as exc:
        _derive(certificate, campaign, admission)
    assert failure in str(exc.value)


def test_rejects_operator_intervention_derived_from_signed_inventory() -> None:
    certificate = _certificate()
    campaign = _campaign()

    def mutate(envelope: dict[str, object]) -> None:
        facts = envelope["verified_projection"]["facts"]
        facts["operator_intervention_ids_sha256"] = ["8" * 64]

    _replace_receipt_envelope(
        campaign, receipt_type="runner", mutate=mutate, resign=True
    )
    _trial(campaign)["failure_class"] = "operator_intervention"
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="operator_intervention"):
        _derive(certificate, campaign, admission)


def _add_signed_model_call(campaign: dict[str, object], *, condition: int) -> None:
    def mutate(envelope: dict[str, object]) -> None:
        facts = envelope["verified_projection"]["facts"]
        counter = facts["model_call_counter"]
        counter.update(
            {
                "attempted": 1,
                "completed": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_microusd": 2,
                "call_ids_sha256": ["8" * 64],
                "provider_models": [{"provider": "fixture", "model": "fixture-v1"}],
            }
        )

    _replace_receipt_envelope(
        campaign,
        receipt_type="runner",
        mutate=mutate,
        resign=True,
        condition=condition,
    )


def test_rejects_healthy_path_model_call_derived_from_signed_counter() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _add_signed_model_call(campaign, condition=0)
    _trial(campaign)["failure_class"] = "healthy_path_model_call"
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="healthy_path_model_call"):
        _derive(certificate, campaign, admission)


def test_counts_governed_halt_model_call_instead_of_hard_coding_zero() -> None:
    certificate = _certificate()
    campaign = _campaign()
    _add_signed_model_call(campaign, condition=4)
    admission = _rebind(certificate, campaign)

    result = _derive(certificate, campaign, admission)

    assert result["reliability"]["model_call_count"] == 1


def test_rejects_declared_effect_classification_not_derived_from_inventory() -> None:
    certificate = _certificate()
    campaign = _campaign()

    def mutate(envelope: dict[str, object]) -> None:
        facts = envelope["verified_projection"]["facts"]
        facts["derived_classifications"]["wrong_record_count"] = 1

    _replace_receipt_envelope(
        campaign, receipt_type="observer", mutate=mutate, resign=True
    )
    admission = _rebind(certificate, campaign)

    with pytest.raises(MODULE.AcceptanceError, match="differ from its inventory"):
        _derive(certificate, campaign, admission)


def test_production_acceptance_target_scope_map_is_closed() -> None:
    assert MODULE.PRODUCTION_ACCEPTANCE_TARGET_SCOPES == {
        "agent": "qualified_agent_bridge_release",
        "capture": "qualified_native_recorder_release",
        "cloud": "qualified_workflow_control_plane_deployment",
        "desktop": "qualified_native_workflow_desktop_release",
        "docs": "production_documentation_deployment",
        "flow": "qualified_workflow_runtime_release",
        "openadapt": "qualified_workflow_launcher_release",
    }
    assert MODULE.production_acceptance_policy_sha256() == (
        "sha256:77130f494453c2443ed824a863069bd055a831da483880386e95b092eabaf5bb"
    )


def test_builds_only_complete_target_neutral_production_manifest() -> None:
    source = _derive()
    original = copy.deepcopy(source)
    lifecycle = _verified_flow_lifecycle(source)

    manifest = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle,
    )

    assert set(manifest) == {
        "schema_version",
        "target",
        "claim_scope",
        "verdict",
        "acceptance_policy_sha256",
        "lifecycle_policy_sha256",
        "target_release_sha256",
        "target_artifact_inventory_sha256",
        "evidence_identity_sha256",
        "source_evidence",
        "qualification",
        "failure_taxonomy_counts",
        "reliability",
        "retention",
    }
    assert manifest["schema_version"] == "openadapt.production-acceptance/v1"
    assert manifest["target"] == "flow"
    assert manifest["claim_scope"] == "qualified_workflow_runtime_release"
    assert manifest["verdict"] == "accepted"
    assert manifest[
        "acceptance_policy_sha256"
    ] == MODULE.production_acceptance_policy_sha256()
    assert manifest["lifecycle_policy_sha256"] == (
        "sha256:" + MODULE.hashlib.sha256(_lifecycle_policy_bytes()).hexdigest()
    )
    assert set(manifest["source_evidence"]) == MODULE._PRODUCTION_SOURCE_EVIDENCE_KEYS
    assert set(manifest["qualification"]) == MODULE._PRODUCTION_QUALIFICATION_KEYS
    assert set(manifest["failure_taxonomy_counts"]) == set(MODULE._FAILURE_TAXONOMY)
    assert set(manifest["reliability"]) == MODULE._RELIABILITY_KEYS
    assert set(manifest["retention"]) == MODULE._RETENTION_KEYS
    assert MODULE.validate_production_acceptance_manifest(
        manifest,
        source,
        lifecycle_release=lifecycle,
    ) == manifest
    assert source == original
    rendered = json.dumps(manifest, sort_keys=True)
    assert "execute-acceptance" not in rendered
    assert "healthy-01" not in rendered
    assert '"conditions"' not in rendered


def test_manifest_uses_exact_lifecycle_release_and_artifact_digest_domains() -> None:
    source = _derive()
    lifecycle = _verified_flow_lifecycle(source)
    manifest = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle,
    )
    release = lifecycle.release()
    artifacts = lifecycle.artifacts()
    assert manifest["target_release_sha256"] == MODULE._domain_sha256(
        b"OpenAdapt production lifecycle target release v1\0",
        {
            "target": "flow",
            "claim_scope": "qualified_workflow_runtime_release",
            "release": release,
        },
    )
    assert manifest["target_artifact_inventory_sha256"] == MODULE._domain_sha256(
        b"OpenAdapt production lifecycle artifact inventory v1\0",
        {
            "target": "flow",
            "claim_scope": "qualified_workflow_runtime_release",
            "artifacts": artifacts,
        },
    )


def test_cloud_manifest_requires_reviewed_deployment_manifest_binding() -> None:
    source = _derive()

    with pytest.raises(MODULE.AcceptanceError, match="reviewed deployment-manifest binding"):
        MODULE.build_production_acceptance_manifest(
            source,
            "cloud",
            lifecycle_release=_verified_flow_lifecycle(source),
        )


@pytest.mark.parametrize(
    "target",
    ["agent", "capture", "desktop", "docs", "openadapt"],
)
def test_browser_source_refuses_target_without_its_evidence_adapter(target: str) -> None:
    with pytest.raises(MODULE.AcceptanceError, match="requires its own evidence adapter"):
        MODULE.build_production_acceptance_manifest(
            _derive(),
            target,
            lifecycle_release=_verified_flow_lifecycle(),
        )


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("schema_version",), "openadapt.production-acceptance/v2"),
        (("target",), "cloud"),
        (("claim_scope",), "qualified_workflow_control_plane_deployment"),
        (("verdict",), "rejected"),
        (("acceptance_policy_sha256",), "sha256:" + "0" * 64),
        (("lifecycle_policy_sha256",), "sha256:" + "0" * 64),
        (("target_release_sha256",), "sha256:" + "0" * 64),
        (("target_artifact_inventory_sha256",), "sha256:" + "0" * 64),
        (("evidence_identity_sha256",), "sha256:" + "0" * 64),
        (("source_evidence", "certificate_sha256"), "sha256:" + "0" * 64),
        (("qualification", "oracle_contract_sha256"), "sha256:" + "0" * 64),
        (("failure_taxonomy_counts", "verified"), 13),
        (("reliability", "model_call_count"), 1),
        (("retention", "head_verified"), False),
    ],
)
def test_one_field_production_manifest_mutation_refuses(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    source = _derive()
    lifecycle = _verified_flow_lifecycle(source)
    manifest = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle,
    )
    mutated = copy.deepcopy(manifest)
    target: dict[str, object] = mutated
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = replacement

    with pytest.raises(MODULE.AcceptanceError):
        MODULE.validate_production_acceptance_manifest(
            mutated,
            source,
            lifecycle_release=lifecycle,
        )


def test_production_manifest_rejects_missing_or_extra_field() -> None:
    source = _derive()
    lifecycle = _verified_flow_lifecycle(source)
    manifest = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle,
    )
    missing = copy.deepcopy(manifest)
    del missing["acceptance_policy_sha256"]
    extra = copy.deepcopy(manifest)
    extra["production_ready"] = True

    for mutation in (missing, extra):
        with pytest.raises(MODULE.AcceptanceError, match="manifest keys differ"):
            MODULE.validate_production_acceptance_manifest(
                mutation,
                source,
                lifecycle_release=lifecycle,
            )


def test_lifecycle_release_must_be_verifier_derived_and_is_immutable() -> None:
    source = _derive()
    with pytest.raises(MODULE.AcceptanceError, match="verifier-derived"):
        MODULE.build_production_acceptance_manifest(
            source,
            "flow",
            lifecycle_release={},
        )
    lifecycle = _verified_flow_lifecycle(source)
    with pytest.raises(AttributeError, match="immutable"):
        lifecycle._target = "cloud"
    release_copy = lifecycle.release()
    release_copy["version"] = "9.9.9"
    assert lifecycle.release()["version"] != "9.9.9"


def test_lifecycle_release_cannot_be_constructed_from_caller_digests() -> None:
    with pytest.raises(TypeError, match="only be created by the lifecycle verifier"):
        MODULE.VerifiedProductionLifecycleRelease(
            target="flow",
            claim_scope="qualified_workflow_runtime_release",
            lifecycle_policy_sha256="sha256:" + "0" * 64,
            release={},
            artifacts=[],
            _seal=object(),
        )


def test_lifecycle_policy_digest_binds_exact_raw_bytes() -> None:
    source = _derive()
    policy, release, metadata = _flow_lifecycle_inputs(source)
    lifecycle_a = MODULE.verify_production_lifecycle_release(
        policy,
        "flow",
        release,
        pypi_release_metadata=metadata,
    )
    lifecycle_b = MODULE.verify_production_lifecycle_release(
        b"\n" + policy,
        "flow",
        release,
        pypi_release_metadata=metadata,
    )
    assert lifecycle_a.lifecycle_policy_sha256 != lifecycle_b.lifecycle_policy_sha256
    manifest_a = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle_a,
    )
    manifest_b = MODULE.build_production_acceptance_manifest(
        source,
        "flow",
        lifecycle_release=lifecycle_b,
    )
    assert manifest_a["lifecycle_policy_sha256"] != manifest_b[
        "lifecycle_policy_sha256"
    ]
    assert manifest_a["acceptance_policy_sha256"] == manifest_b[
        "acceptance_policy_sha256"
    ]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda release: release.__setitem__("kind", "private_deployment"), "identity"),
        (lambda release: release.__setitem__("tag", "v9.9.9"), "identity"),
        (lambda release: release.__setitem__("source_commit", "bad"), "commit"),
        (
            lambda release: release.__setitem__(
                "immutable_release_url",
                "https://github.com/OpenAdaptAI/openadapt-flow/releases/tag/v1.0.0",
            ),
            "exact commit",
        ),
        (lambda release: release.__setitem__("extra", True), "keys differ"),
    ],
)
def test_lifecycle_verifier_refuses_invalid_release_shape(
    mutation: object,
    expected: str,
) -> None:
    policy, release, metadata = _flow_lifecycle_inputs()
    mutation(release)
    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_production_lifecycle_release(
            policy,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda artifacts: artifacts.pop(), "one sdist and one wheel"),
        (lambda artifacts: artifacts.reverse(), "sorted sdist and wheel"),
        (
            lambda artifacts: artifacts[0].__setitem__("authority", "github_release"),
            "authority is invalid",
        ),
        (lambda artifacts: artifacts[0].__setitem__("name", "bad name"), "name"),
        (lambda artifacts: artifacts[0].__setitem__("sha256", "bad"), "digest"),
        (lambda artifacts: artifacts[0].__setitem__("size_bytes", 0), "size"),
        (
            lambda artifacts: artifacts[0].__setitem__(
                "url", "https://example.com/openadapt_flow.tar.gz"
            ),
            "not from PyPI",
        ),
        (
            lambda artifacts: artifacts[0].__setitem__(
                "url",
                str(artifacts[0]["url"]) + "?download=1",
            ),
            "clean HTTPS URL",
        ),
        (lambda artifacts: artifacts[0].__setitem__("extra", True), "keys differ"),
    ],
)
def test_lifecycle_verifier_refuses_invalid_artifact_inventory(
    mutation: object,
    expected: str,
) -> None:
    policy, release, metadata = _flow_lifecycle_inputs()
    artifacts = release["artifacts"]
    assert isinstance(artifacts, list)
    mutation(artifacts)
    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.verify_production_lifecycle_release(
            policy,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("filename", "other.whl"),
        ("url", "https://files.pythonhosted.org/packages/other.whl"),
        ("size", 999),
        ("yanked", True),
    ],
)
def test_lifecycle_verifier_refuses_pypi_metadata_substitution(
    field: str,
    replacement: object,
) -> None:
    policy, release, metadata = _flow_lifecycle_inputs()
    metadata["urls"][1][field] = replacement
    with pytest.raises(MODULE.AcceptanceError, match="does not verify exact artifact"):
        MODULE.verify_production_lifecycle_release(
            policy,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )


def test_lifecycle_verifier_refuses_pypi_digest_and_duplicate_match() -> None:
    policy, release, metadata = _flow_lifecycle_inputs()
    metadata["urls"][1]["digests"]["sha256"] = "0" * 64
    with pytest.raises(MODULE.AcceptanceError, match="does not verify exact artifact"):
        MODULE.verify_production_lifecycle_release(
            policy,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )

    policy, release, metadata = _flow_lifecycle_inputs()
    metadata["urls"].append(copy.deepcopy(metadata["urls"][1]))
    with pytest.raises(MODULE.AcceptanceError, match="does not verify exact artifact"):
        MODULE.verify_production_lifecycle_release(
            policy,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )


def test_lifecycle_verifier_refuses_policy_target_drift_and_duplicate_keys() -> None:
    policy, release, metadata = _flow_lifecycle_inputs()
    policy_value = json.loads(policy)
    flow = next(item for item in policy_value["targets"] if item["id"] == "flow")
    flow["required_claim_scope"] = "qualified_other_release"
    with pytest.raises(MODULE.AcceptanceError, match="policy differs"):
        MODULE.verify_production_lifecycle_release(
            json.dumps(policy_value).encode(),
            "flow",
            release,
            pypi_release_metadata=metadata,
        )
    duplicate = policy.replace(
        b'"revision": 1,',
        b'"revision": 1, "revision": 1,',
        1,
    )
    with pytest.raises(MODULE.AcceptanceError, match="duplicate key"):
        MODULE.verify_production_lifecycle_release(
            duplicate,
            "flow",
            release,
            pypi_release_metadata=metadata,
        )


@pytest.mark.parametrize("field", ["version", "source_commit", "wheel_sha256"])
def test_flow_lifecycle_binding_substitution_refuses(field: str) -> None:
    source = _derive()
    policy, release, metadata = _flow_lifecycle_inputs(source)
    if field == "version":
        release["version"] = "9.9.9"
        release["tag"] = "v9.9.9"
        metadata["info"]["version"] = "9.9.9"
    elif field == "source_commit":
        release["source_commit"] = "0" * 40
        release["immutable_release_url"] = (
            "https://github.com/OpenAdaptAI/openadapt-flow/commit/" + "0" * 40
        )
    else:
        release["artifacts"][1]["sha256"] = "sha256:" + "0" * 64
        metadata["urls"][1]["digests"]["sha256"] = "0" * 64
    lifecycle = MODULE.verify_production_lifecycle_release(
        policy,
        "flow",
        release,
        pypi_release_metadata=metadata,
    )
    with pytest.raises(MODULE.AcceptanceError, match="differs from verified evidence"):
        MODULE.build_production_acceptance_manifest(
            source,
            "flow",
            lifecycle_release=lifecycle,
        )


def test_unbound_cloud_deployment_manifest_cannot_enter_an_accepted_record() -> None:
    source = _derive()
    with pytest.raises(MODULE.AcceptanceError, match="reviewed deployment-manifest binding"):
        MODULE.build_production_acceptance_manifest(
            source,
            "cloud",
            lifecycle_release={
                "kind": "private_deployment",
                "manifest_sha256": "sha256:" + "0" * 64,
            },
        )


def test_incomplete_or_failed_private_source_cannot_build_manifest() -> None:
    source = _derive()
    lifecycle = _verified_flow_lifecycle(source)
    mutations = []
    wrong_verdict = copy.deepcopy(source)
    wrong_verdict["verdict"] = "rejected"
    mutations.append(wrong_verdict)
    missing_binding = copy.deepcopy(source)
    del missing_binding["bindings"]["oracle_contract_sha256"]
    mutations.append(missing_binding)
    production_failure = copy.deepcopy(source)
    production_failure["derived_outcomes"]["verified"] -= 1
    production_failure["derived_outcomes"]["over_halt"] += 1
    production_failure["reliability"]["over_halt_count"] = 1
    mutations.append(production_failure)

    for mutation in mutations:
        with pytest.raises(MODULE.AcceptanceError):
            MODULE.build_production_acceptance_manifest(
                mutation,
                "flow",
                lifecycle_release=lifecycle,
            )


def test_private_source_reliability_schema_is_closed() -> None:
    source = _derive()
    lifecycle = _verified_flow_lifecycle(source)
    missing = copy.deepcopy(source)
    del missing["reliability"]["operator_intervention_count"]
    extra = copy.deepcopy(source)
    extra["reliability"]["duplicate_count"] = 0

    for mutation in (missing, extra):
        with pytest.raises(MODULE.AcceptanceError, match="reliability keys differ"):
            MODULE.build_production_acceptance_manifest(
                mutation,
                "flow",
                lifecycle_release=lifecycle,
            )


@pytest.mark.parametrize(
    "field,replacement,expected",
    [
        ("receipt_id", "retention:not-a-receipt", "receipt ID"),
        ("provenance_attestation", "unreviewed-v1", "provenance"),
        ("visibility_verified", False, "visibility_verified is false"),
        (
            "acceptance_verified_at",
            "2026-08-18T12:00:00Z",
            "canonical millisecond UTC form",
        ),
        ("retained_at", "2026-08-18T11:59:59.000Z", "chronology"),
    ],
)
def test_private_source_retention_mutation_refuses(
    field: str,
    replacement: object,
    expected: str,
) -> None:
    source = _derive()
    source["retention"][field] = replacement

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.build_production_acceptance_manifest(
            source,
            "flow",
            lifecycle_release=_verified_flow_lifecycle(),
        )


def test_mutation_helpers_do_not_modify_committed_fixtures() -> None:
    certificate = _certificate()
    campaign = _campaign()
    admission = _admission()
    original_certificate = copy.deepcopy(certificate)
    original_campaign = copy.deepcopy(campaign)

    _derive(certificate, campaign, admission)

    assert certificate == original_certificate
    assert campaign == original_campaign
