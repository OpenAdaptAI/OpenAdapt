from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
LEGACY_REFUSAL = WORKFLOWS / "import-production-acceptance.yml"
ACCEPTANCE_ISSUER = WORKFLOWS / "issue-production-acceptance.yml"


def test_no_evals_workflow_can_accept_a_private_payload() -> None:
    """Keep the legacy importer unreachable until the public cutover removes it."""

    forbidden_public_inputs = {
        "private_export_contract:",
        "--private-export-contract",
        "--campaign /",
        "--qualification-admission /",
    }
    for workflow in WORKFLOWS.glob("*.yml"):
        if workflow == LEGACY_REFUSAL:
            continue
        text = workflow.read_text(encoding="utf-8")
        for token in forbidden_public_inputs:
            assert token not in text, f"{workflow.name} accepts private input via {token!r}"

    legacy = LEGACY_REFUSAL.read_text(encoding="utf-8")
    assert "Staging private evidence is not implemented, and the import gate is closed." in legacy
    assert "if python scripts/import_production_acceptance.py" in legacy
    assert "--certificate /nonexistent/certificate.json" in legacy
    assert "--campaign /nonexistent/campaign.json" in legacy
    assert "--qualification-admission /nonexistent/admission.json" in legacy
    assert "--attestation-bundle /nonexistent/bundle.jsonl" in legacy
    assert "the importer produced a result while the gate is closed" in legacy
    assert 'test ! -e "${RUNNER_TEMP}/derived.json"' in legacy
    assert "actions/upload-artifact" not in legacy


def test_production_acceptance_issuer_exists_and_refuses() -> None:
    """The named issuer is installed. Every run exits without writing evidence."""

    assert ACCEPTANCE_ISSUER.is_file()
    text = ACCEPTANCE_ISSUER.read_text(encoding="utf-8")
    assert text.startswith("name: Production acceptance issuer\n")
    assert "on:\n  workflow_dispatch:\n" in text
    assert "permissions: {}" in text
    assert "reject-lifecycle-app:" in text
    assert "refuse-inactive-issuer:" in text
    assert "environment: production-acceptance" not in text
    assert "id-token: write" not in text
    assert "actions/upload-artifact" not in text
    assert "gh attestation" not in text
    assert "gh release" not in text
    assert "verdict: accepted" not in text
    assert "persist-credentials: false" in text
    assert "The production acceptance issuer is installed but inactive." in text
    assert "exit 1" in text
    assert (
        "The central signer registry, authority state, revocation state, "
        "and protected vectors must exist before activation."
    ) in text
    assert "github.actor != 'openadapt-lifecycle[bot]'" not in text
    assert "test \"$ACTOR\" != 'openadapt-lifecycle[bot]'" in text
    assert "test \"$TRIGGERING_ACTOR\" != 'openadapt-lifecycle[bot]'" in text
    assert "test \"$REPOSITORY\" = 'OpenAdaptAI/openadapt-evals'" in text
    assert "test \"$REF\" = 'refs/heads/main'" in text
    assert "test \"$EVENT_NAME\" = 'workflow_dispatch'" in text
    assert 'test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"' in text
    assert "secrets." not in text
    assert "OPENADAPT_RELEASE_APP_PRIVATE_KEY" not in text
