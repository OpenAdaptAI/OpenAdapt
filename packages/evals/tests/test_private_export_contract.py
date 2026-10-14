from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_production_acceptance.py"
SPEC = importlib.util.spec_from_file_location("production_acceptance_importer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

WORKFLOW_REF = (
    "OpenAdaptAI/openadapt-evals/.github/workflows/import-production-acceptance.yml"
    "@refs/heads/main"
)


def _contract() -> dict[str, Any]:
    return {
        "schema_version": MODULE.PRIVATE_EXPORT_CONTRACT_SCHEMA,
        "destination": {
            "repository": "OpenAdaptAI/openadapt-evidence",
            "ref": "refs/heads/main",
            "path_prefix": "production-acceptance",
            "encryption_recipient": "age19gxzh0n7d6r8hdwpdfvuwxgc46gtcq0fd298nq2v8puqprtphetsg7sdh2",
            "retention_commitment_days": 2555,
        },
        "uploader_identity": "openadapt-release[bot]",
        "importer_workflow_ref": WORKFLOW_REF,
        "approval_authority": "OpenAdapt",
        "approved_at": "2026-08-26T12:00:00.000Z",
    }


def test_every_digest_matches_the_cloud_retention_writer() -> None:
    """The Cloud writer emits these digests. Ours must be byte-identical.

    `opaqueDigest` in openadapt-cloud's scripts/retain-execute-private-evidence.mjs
    is sha256(`OpenAdapt ${domain} v1\\0` + value) over a single string. This
    recomputes that independently of the implementation.
    """

    contract = _contract()
    facts = MODULE.validate_private_export_contract(contract)
    destination = contract["destination"]

    def cloud(domain: str, value: str) -> str:
        payload = f"OpenAdapt {domain} v1\0".encode("utf-8") + value.encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    assert facts["storage_identity_sha256"] == cloud(
        "retention store", destination["repository"]
    )
    assert facts["encryption_recipient_sha256"] == cloud(
        "retention encryption recipient", destination["encryption_recipient"]
    )
    assert facts["uploader_identity_sha256"] == cloud(
        "retention uploader", contract["uploader_identity"]
    )
    assert facts["destination_approval_sha256"] == cloud(
        "Execute acceptance retention destination",
        MODULE.canonical_json({k: destination[k] for k in sorted(destination)}),
    )


def test_the_retention_separator_is_not_the_acceptance_separator() -> None:
    """These two helpers differ by one word and produce different digests.

    `opaque_binding_sha256` inserts "acceptance" into the separator. Using it
    for a retention binding would refuse every genuine certificate, which is
    exactly the defect this file exists to prevent recurring.
    """

    repository = _contract()["destination"]["repository"]

    assert MODULE.retention_binding_sha256("retention store", repository) != (
        MODULE.opaque_binding_sha256("retention store", repository)
    )


def test_the_contract_carries_values_not_digests() -> None:
    contract = _contract()

    # An approval that asserted a digest would be unverifiable. Every digest the
    # importer compares against has to be derived from a visible value.
    assert not any(key.endswith("_sha256") for key in contract)
    assert not any(key.endswith("_sha256") for key in contract["destination"])
    facts = MODULE.validate_private_export_contract(contract)
    assert facts["storage_identity_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda c: c.__setitem__("schema_version", "other/v1"), "schema is not supported"),
        (lambda c: c.pop("approval_authority"), "keys differ"),
        (lambda c: c.__setitem__("unreviewed", True), "keys differ"),
        (lambda c: c.__setitem__("approval_authority", ""), "approval authority"),
        (lambda c: c.__setitem__("importer_workflow_ref", "main"), "workflow ref is invalid"),
        (
            lambda c: c.__setitem__(
                "importer_workflow_ref",
                "OpenAdaptAI/openadapt-evals/.github/workflows/x.yml@refs/tags/v1",
            ),
            "workflow ref is invalid",
        ),
        (lambda c: c["destination"].__setitem__("repository", "nope"), "repository is invalid"),
        (lambda c: c["destination"].__setitem__("ref", "refs/tags/v1"), "ref is invalid"),
        (lambda c: c["destination"].__setitem__("path_prefix", "/x"), "path prefix"),
        (lambda c: c["destination"].__setitem__("path_prefix", "a//b"), "path prefix"),
        (lambda c: c["destination"].__setitem__("path_prefix", "a/../b"), "path prefix"),
        (lambda c: c["destination"].__setitem__("retention_commitment_days", 30), "outside policy"),
        (lambda c: c["destination"].__setitem__("retention_commitment_days", 4000), "outside policy"),
        (lambda c: c["destination"].__setitem__("retention_commitment_days", "2555"), "must be an integer"),
        (lambda c: c.__setitem__("uploader_identity", "bad identity!"), "uploader identity is invalid"),
        (lambda c: c["destination"].__setitem__("encryption_recipient", "age1short"), "encryption recipient is invalid"),
        (lambda c: c["destination"].pop("ref"), "retention destination keys differ"),
        (lambda c: c.__setitem__("approved_at", "2026-08-26T12:00:00Z"), "canonical"),
    ],
)
def test_contract_refuses_anything_inexact(mutation: object, expected: str) -> None:
    contract = _contract()
    mutation(contract)

    with pytest.raises(MODULE.AcceptanceError, match=expected):
        MODULE.validate_private_export_contract(contract)


def test_retention_days_must_sit_inside_the_fixed_policy() -> None:
    policy = MODULE.production_acceptance_policy()
    facts = MODULE.validate_private_export_contract(_contract())

    assert policy["minimum_retention_days"] <= facts["retention_commitment_days"]
    assert facts["retention_commitment_days"] <= policy["maximum_retention_days"]


def test_importer_identity_must_be_the_approved_workflow_and_ref() -> None:
    facts = MODULE.validate_private_export_contract(_contract())

    MODULE.verify_importer_identity(facts, {"GITHUB_WORKFLOW_REF": WORKFLOW_REF})

    for environ, expected in (
        ({}, "importer workflow ref is absent"),
        ({"GITHUB_WORKFLOW_REF": ""}, "importer workflow ref is absent"),
        (
            {
                "GITHUB_WORKFLOW_REF": (
                    "OpenAdaptAI/openadapt-evals/.github/workflows/other.yml@refs/heads/main"
                )
            },
            "not the approved workflow",
        ),
        (
            {"GITHUB_WORKFLOW_REF": WORKFLOW_REF.replace("refs/heads/main", "refs/heads/dev")},
            "not the approved workflow",
        ),
        (
            {"GITHUB_WORKFLOW_REF": WORKFLOW_REF.replace("OpenAdaptAI", "attacker")},
            "not the approved workflow",
        ),
    ):
        with pytest.raises(MODULE.AcceptanceError, match=expected):
            MODULE.verify_importer_identity(facts, environ)


def _retention(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "storage_identity_sha256": facts["storage_identity_sha256"],
        "encryption_recipient_sha256": facts["encryption_recipient_sha256"],
        "uploader_identity_sha256": facts["uploader_identity_sha256"],
        "retained_at": "2026-08-18T12:00:00.000Z",
        "retention_until": "2034-08-18T12:00:00.000Z",
    }


def test_retention_must_match_the_approved_destination_key_and_uploader() -> None:
    facts = MODULE.validate_private_export_contract(_contract())

    MODULE.verify_retention_against_contract(_retention(facts), facts)

    for key in (
        "storage_identity_sha256",
        "encryption_recipient_sha256",
        "uploader_identity_sha256",
    ):
        retention = _retention(facts)
        retention[key] = "sha256:" + "0" * 64
        with pytest.raises(MODULE.AcceptanceError, match=f"{key} is not the approved"):
            MODULE.verify_retention_against_contract(retention, facts)

        retention = _retention(facts)
        retention.pop(key)
        with pytest.raises(MODULE.AcceptanceError, match=f"{key} is not the approved"):
            MODULE.verify_retention_against_contract(retention, facts)


def test_a_certificate_cannot_choose_its_own_destination() -> None:
    facts = MODULE.validate_private_export_contract(_contract())
    elsewhere = copy.deepcopy(_contract())
    elsewhere["destination"]["repository"] = "attacker/evidence"
    forged = MODULE.validate_private_export_contract(elsewhere)

    retention = _retention(facts)
    retention["storage_identity_sha256"] = forged["storage_identity_sha256"]

    with pytest.raises(MODULE.AcceptanceError, match="not the approved identity"):
        MODULE.verify_retention_against_contract(retention, facts)


def test_the_commitment_is_recorded_and_not_enforced() -> None:
    """A git commit has no expiry, so the period is a commitment, not a lock.

    Enforcing a number nothing can hold would be theatre. The contract records
    retention_commitment_days and the verifier does not police it; what it does
    police is that the evidence went to the approved repository, key and
    uploader.
    """

    facts = MODULE.validate_private_export_contract(_contract())

    assert facts["retention_commitment_days"] == 2555
    # No period is compared, so no retained_at or retention_until is needed.
    MODULE.verify_retention_against_contract(
        {
            "storage_identity_sha256": facts["storage_identity_sha256"],
            "encryption_recipient_sha256": facts["encryption_recipient_sha256"],
            "uploader_identity_sha256": facts["uploader_identity_sha256"],
        },
        facts,
    )


def test_cli_refuses_a_contract_it_is_not_authorised_to_use(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", "attacker/repo/.github/workflows/x.yml@refs/heads/main")

    result = MODULE.main(
        [
            "--certificate",
            str(tmp_path / "certificate.json"),
            "--campaign",
            str(tmp_path / "campaign.json"),
            "--qualification-admission",
            str(tmp_path / "admission.json"),
            "--attestation-bundle",
            str(tmp_path / "bundle.jsonl"),
            "--expected-cloud-source-commit",
            "f" * 40,
            "--trusted-admission-signers",
            str(ROOT / "tests/fixtures/production_acceptance/qualification-admission-trust.json"),
            "--private-export-contract",
            str(contract_path),
            "--output",
            str(tmp_path / "derived.json"),
        ]
    )

    assert result == 1
    assert "not the approved workflow" in capsys.readouterr().err
    assert not (tmp_path / "derived.json").exists()


def test_cli_still_refuses_even_with_an_authorised_importer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract mechanism is implemented. The import gate stays closed."""

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(_contract()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKFLOW_REF", WORKFLOW_REF)

    result = MODULE.main(
        [
            "--certificate",
            str(tmp_path / "certificate.json"),
            "--campaign",
            str(tmp_path / "campaign.json"),
            "--qualification-admission",
            str(tmp_path / "admission.json"),
            "--attestation-bundle",
            str(tmp_path / "bundle.jsonl"),
            "--expected-cloud-source-commit",
            "f" * 40,
            "--trusted-admission-signers",
            str(ROOT / "tests/fixtures/production_acceptance/qualification-admission-trust.json"),
            "--private-export-contract",
            str(contract_path),
            "--output",
            str(tmp_path / "derived.json"),
        ]
    )

    assert result == 1
    assert "pending an approved private-export contract" in capsys.readouterr().err
    assert not (tmp_path / "derived.json").exists()
