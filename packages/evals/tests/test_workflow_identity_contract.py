from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
LIFECYCLE_ACTOR = "openadapt-lifecycle[bot]"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_production_lifecycle_evidence_is_app_only() -> None:
    workflow = _workflow("production-lifecycle-evidence.yml")
    assert re.search(r"(?m)^  workflow_dispatch:\s*$", workflow)
    assert not re.search(
        r"(?m)^  (pull_request|pull_request_target|push|release|schedule|repository_dispatch|workflow_call):\s*$",
        workflow,
    )
    assert "environment: production-lifecycle-evidence" in workflow
    assert "github.repository == 'OpenAdaptAI/openadapt-evals'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert f"github.actor == '{LIFECYCLE_ACTOR}'" in workflow
    assert f"github.triggering_actor == '{LIFECYCLE_ACTOR}'" in workflow
    assert "github.actor_id == vars.OPENADAPT_LIFECYCLE_ACTOR_ID" in workflow
    assert "vars.OPENADAPT_LIFECYCLE_APP_ID" in workflow
    assert "vars.OPENADAPT_LIFECYCLE_INSTALLATION_ID" in workflow
    assert "secrets.OPENADAPT_LIFECYCLE_APP_PRIVATE_KEY" in workflow
    assert "permission-pull-requests: write" in workflow
    assert "contents: write" in workflow
    assert "token: ${{ github.token }}" in workflow
    assert "gh pr create" in workflow
    assert not re.search(r"git\s+push[^\n]*(?:refs/heads/)?main", workflow)
    assert "permission-contents: write" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "github.workflow" in workflow
    assert "github.event_name" in workflow


def test_manual_non_lifecycle_workflows_reject_the_lifecycle_app() -> None:
    for name, job in (
        ("complex-visual.yml", "headed-pixel-campaign"),
        ("evidence-freshness.yml", "freshness"),
        ("propose-release.yml", "propose-release"),
        ("import-production-acceptance.yml", "import-production-acceptance"),
    ):
        workflow = _workflow(name)
        assert "reject-lifecycle-app:" in workflow
        assert "permissions: {}" in workflow
        assert workflow.count(LIFECYCLE_ACTOR) >= 4
        assert re.search(rf"(?ms)^  {job}:\n.*?    needs: reject-lifecycle-app", workflow)
        assert "github.actor != 'openadapt-lifecycle[bot]'" in workflow
        assert "github.triggering_actor != 'openadapt-lifecycle[bot]'" in workflow
        assert "cancel-in-progress: false" in workflow
        group = re.search(r"(?m)^  group: (.+)$", workflow)
        assert group is not None
        assert "github.workflow" in group.group(1)
        assert "github.event_name" in group.group(1)


def test_release_proposal_can_only_propose() -> None:
    workflow = _workflow("propose-release.yml")

    assert "scripts/plan_release.py" in workflow
    assert "scripts/verify_release_lock.py --write" in workflow
    assert "gh pr create" in workflow
    assert "vars.OPENADAPT_LIFECYCLE_APP_ID" in workflow
    assert "vars.OPENADAPT_LIFECYCLE_INSTALLATION_ID" in workflow
    assert "secrets.OPENADAPT_LIFECYCLE_APP_PRIVATE_KEY" in workflow
    assert "permission-pull-requests: write" in workflow

    # It proposes. It must not be able to land, tag, or publish what it wrote.
    assert "permission-contents: write" not in workflow
    assert not re.search(r"git\s+push[^\n]*(?:refs/heads/)?main", workflow)
    assert "gh pr merge" not in workflow
    assert "--auto" not in workflow
    assert "git tag" not in workflow
    assert "gh release" not in workflow
    assert "pypa/gh-action-pypi-publish" not in workflow
    assert "OPENADAPT_RELEASE_APP_ID" not in workflow
    assert "OPENADAPT_RELEASE_APP_PRIVATE_KEY" not in workflow

    # It writes only the three release-metadata files.
    assert "git add pyproject.toml uv.lock CHANGELOG.md" in workflow


def test_the_release_proposal_survives_the_built_in_token_suppression() -> None:
    """A pushed update must end with checks running on the head being merged.

    A force-push made with the built-in token emits a synchronize event GitHub
    ignores, so without both of these an updated proposal keeps the checks from
    its previous head and can never be merged.
    """

    workflow = _workflow("propose-release.yml")

    # An unchanged proposal is not pushed, so a head that already passed is
    # never disturbed.
    assert "git diff --quiet \\\n              FETCH_HEAD" in workflow
    assert "changed=false" in workflow
    assert "changed=true" in workflow
    # A changed proposal is reopened, which does start a run.
    assert "gh pr close \"$existing\"" in workflow
    assert "gh pr reopen \"$existing\"" in workflow
    assert 'if [ "${CHANGED}" = \'true\' ]; then' in workflow


def test_the_importer_workflow_is_the_one_the_contract_can_name() -> None:
    workflow = _workflow("import-production-acceptance.yml")
    template = json.loads(
        (ROOT / "docs/eval_results/private-export-contract.template.json").read_text(
            encoding="utf-8"
        )
    )

    # The contract template must name this exact file and ref, or the identity
    # check refuses every run of it.
    assert template["importer_workflow_ref"] == (
        "OpenAdaptAI/openadapt-evals/.github/workflows/"
        "import-production-acceptance.yml@refs/heads/main"
    )

    assert re.search(r"(?m)^  workflow_dispatch:\s*$", workflow)
    assert not re.search(
        r"(?m)^  (pull_request|pull_request_target|push|release|schedule|repository_dispatch|workflow_call):\s*$",
        workflow,
    )
    assert "environment: production-acceptance-import" in workflow
    assert "github.repository == 'OpenAdaptAI/openadapt-evals'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "validate_private_export_contract" in workflow
    assert "verify_importer_identity" in workflow

    # It reads evidence. It must never write, tag, publish, or push.
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "git push" not in workflow
    assert "gh pr merge" not in workflow
    assert "gh release" not in workflow
    assert "pypa/gh-action-pypi-publish" not in workflow

    # A run must fail loudly if the closed gate ever produces a result.
    assert "the importer produced a result while the gate is closed" in workflow


def test_legacy_docs_pat_dispatch_is_removed() -> None:
    assert not (WORKFLOWS / "notify-docs.yml").exists()
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml")
    )
    assert "DOCS_DISPATCH_TOKEN" not in combined
    assert "peter-evans/repository-dispatch" not in combined
