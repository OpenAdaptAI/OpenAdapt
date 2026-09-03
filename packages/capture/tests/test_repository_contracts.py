"""Static contracts for repository integration workflows."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docs_dispatch_targets_canonical_repo_with_pinned_action() -> None:
    """Avoid redirecting a signed dispatch body and pin the secret-using action."""
    workflow = (ROOT / ".github/workflows/notify-docs.yml").read_text(encoding="utf-8")

    assert "repository: OpenAdaptAI/openadapt-ops" in workflow
    assert "OpenAdaptAI/openadapt-maintenance" not in workflow
    assert re.search(
        r"uses: peter-evans/repository-dispatch@[0-9a-f]{40}(?:\s+#.*)?$",
        workflow,
        flags=re.MULTILINE,
    )


def test_release_workflow_uses_only_pinned_actions() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    revisions = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", workflow, re.MULTILINE)

    assert revisions
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions)


def test_release_tag_requires_reviewed_exact_main_and_release_app() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    triggers = workflow[workflow.index("\non:\n") : workflow.index("\njobs:\n")]
    release = workflow[
        workflow.index("\n  create-release-tag:") : workflow.index("\n  validate-tag:")
    ]
    current_main_index = release.index(
        "- name: Require the reviewed release candidate on current main"
    )
    qualification_index = release.index(
        "- name: Require exact-main test, hosted, and live Linux evidence"
    )
    artifact_index = release.index("- name: Build and verify the exact release artifacts")
    tag_index = release.index("- name: Create and push one annotated release tag")
    tag_block = release[tag_index:]

    assert "  workflow_dispatch:" in triggers
    assert "      version:" in triggers
    assert "  push:" in triggers
    assert 'tags: ["v*"]' in triggers
    assert "environment: release-identity" in release
    assert "actions/create-github-app-token@" in release
    assert "vars.OPENADAPT_RELEASE_APP_ID" in release
    assert "secrets.OPENADAPT_RELEASE_APP_PRIVATE_KEY" in release
    assert "permission-contents: write" in release
    assert "token: ${{ steps.release-app.outputs.token }}" in release
    assert current_main_index < qualification_index < artifact_index < tag_index
    assert "python scripts/check_release_ci.py" in release
    assert "--dispatch-missing" in release
    assert '--ref "${GITHUB_REF_NAME}"' in release
    assert "actions: write" in release
    assert "python scripts/check_changelog.py" in release
    assert "python scripts/verify_distribution.py dist/*" in release
    assert "python scripts/check_source_boundary.py --require-dist" in release
    assert "git tag --annotate" in release
    assert 'current_main="$(git rev-parse refs/remotes/origin/main)"' in tag_block
    assert 'if [ "${current_main}" != "${GITHUB_SHA}" ]' in tag_block
    assert 'git push origin "refs/tags/${release_tag}"' in release
    assert "python-semantic-release/python-semantic-release" not in workflow
    assert "ADMIN_TOKEN" not in workflow


def test_exact_test_workflow_can_be_started_by_the_release_orchestrator() -> None:
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    triggers = workflow[workflow.index("\non:\n") : workflow.index("\njobs:\n")]

    assert "  workflow_dispatch:" in triggers
    assert "  push:" in triggers
    assert "  pull_request:" in triggers


def test_tag_publication_requires_exact_tag_oidc_and_digest_verification() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    validate = workflow[workflow.index("\n  validate-tag:") : workflow.index("\n  publish-tag:")]
    publish = workflow[workflow.index("\n  publish-tag:") :]

    assert "GITHUB_EVENT_NAME" in validate
    assert "GITHUB_REF_TYPE" in validate
    assert '${GITHUB_ACTOR}" != "openadapt-release[bot]' in validate
    assert "GITHUB_TRIGGERING_ACTOR" not in validate
    assert "^refs/tags/v([0-9]+\\.[0-9]+\\.[0-9]+)$" in validate
    assert "environment: pypi" in publish
    assert "id-token: write" in publish
    assert "persist-credentials: false" in publish
    assert "git cat-file -t" in publish
    assert "git merge-base --is-ancestor" in publish
    assert "python scripts/check_release_ci.py" in publish
    assert "python scripts/verify_distribution.py dist/*" in publish
    assert "python scripts/check_source_boundary.py --require-dist" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "python-semantic-release/publish-action@" in publish
    assert "scripts/verify_release_publication.py" in publish
    assert "--allow-missing" in publish
    assert "--wait-seconds 180" in publish


def test_native_observers_are_part_of_each_interactive_qualification() -> None:
    workflow = (ROOT / ".github/workflows/live-qualification.yml").read_text(encoding="utf-8")
    linux = workflow[
        workflow.index("\n  interactive-linux:") : workflow.index("\n  interactive-macos:")
    ]
    macos = workflow[
        workflow.index("\n  interactive-macos:") : workflow.index("\n  interactive-windows:")
    ]
    windows = workflow[workflow.index("\n  interactive-windows:") :]

    assert "      candidate_sha:" in workflow
    assert "      platform:" in workflow
    assert "CAPTURE_SELF_HOSTED_QUALIFIED_LINUX_RUNNERS" in workflow
    assert "CAPTURE_SELF_HOSTED_QUALIFIED_MACOS_RUNNERS" in workflow
    assert "CAPTURE_SELF_HOSTED_QUALIFIED_WINDOWS_RUNNERS" in workflow
    assert "CAPTURE_SELF_HOSTED_QUALIFIED_RUNNERS" not in workflow
    assert "ref: ${{ needs.runner-availability.outputs.candidate_sha }}" in workflow
    assert '"${candidate_wheels[0]}[linux]"' in linux
    for job in (linux, macos, windows):
        assert 'tests/test_structural_observation.py"' in job
        assert 'OPENADAPT_CAPTURE_PRODUCTION_QUALIFICATION: "1"' in job


def test_the_release_gate_runs_only_on_github_hosted_runners() -> None:
    """The release gate must never wait on a runner nobody has registered.

    Three self-hosted lanes stood in this workflow from 2026-08-20 to
    2026-08-28. No runner carrying their label existed, so every dispatch
    cancelled them and the gate never passed once. They live in
    live-qualification.yml now.
    """
    workflow = (ROOT / ".github/workflows/production-qualification.yml").read_text(encoding="utf-8")
    targets = re.findall(r"^\s*runs-on: (.+)$", workflow, flags=re.M)
    assert targets
    assert not [target for target in targets if "self-hosted" in target]


def test_the_release_gate_job_set_matches_the_qualification_workflow() -> None:
    """check_release_ci.py and the workflow must name the same jobs.

    A disagreement here is a deadlock: the gate waits for a job that the
    workflow never produces.
    """
    from scripts.check_release_ci import EXPECTED_QUALIFICATION_JOBS

    workflow = (ROOT / ".github/workflows/production-qualification.yml").read_text(encoding="utf-8")
    blocks = re.split(r"^  (?=\S)", workflow[workflow.index("\njobs:\n") :], flags=re.M)
    produced: set[str] = set()
    for block in blocks:
        found = re.search(r"^    name: (.+)$", block, flags=re.M)
        if found is None:
            continue
        name = found.group(1).strip()
        operating_systems = re.search(r"^        os: \[(.+)\]$", block, flags=re.M)
        if operating_systems is None:
            produced.add(name)
            continue
        for value in operating_systems.group(1).split(","):
            produced.add(name.replace("${{ matrix.os }}", value.strip()))
    assert produced == set(EXPECTED_QUALIFICATION_JOBS)


def test_live_release_gate_job_set_matches_the_live_workflow() -> None:
    """The exact-SHA Linux release check must name every live workflow job."""
    from scripts.check_release_ci import EXPECTED_LIVE_QUALIFICATION_JOBS

    workflow = (ROOT / ".github/workflows/live-qualification.yml").read_text(encoding="utf-8")
    produced = set(re.findall(r"^    name: (.+)$", workflow, flags=re.M))

    assert produced == set(EXPECTED_LIVE_QUALIFICATION_JOBS)
