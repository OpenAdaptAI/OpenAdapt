"""A Desktop release that reached PyPI but not its published GitHub release.

Desktop 0.22.0 run 39374653331 uploaded the wheel and sdist to PyPI, then
refused them: the PyPI publish action had written ``*.publish.attestation``
files into the dist directory, and the byte check called them unexpected. The
draft release, with every installer and the tag, was never published. A rerun
would replay the same workflow, and a new run refused the partly published
version. A dispatch now resumes that exact state without building anything.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/desktop-release.yml"


def _jobs() -> dict:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return payload["jobs"]


def _steps(job: str) -> dict[str, dict]:
    return {
        str(step.get("name") or step.get("uses")): step
        for step in _jobs()[job]["steps"]
    }


STATE = "Read the version and the state of its release"
RESUME = "Resume from the staged draft, reverified, and the PyPI files"
PUBLISH = "Publish the verified draft once, then verify the immutable release"
DOWNLOAD = "Download the exact attested release set"


def _resume_branch() -> str:
    run = _steps("gate")[STATE]["run"]
    start = run.index('elif [ "${pypi}" = 200 ] && [ "${release}" != false ]')
    end = run.index('elif [ "${pypi}" = 200 ] || [ "${pypi}" = 404 ]; then')
    return run[start:end]


def test_only_a_dispatch_resumes_a_version_on_pypi_with_its_tag_and_no_release() -> (
    None
):
    branch = _resume_branch()
    condition = branch.split("; then", 1)[0]
    assert 'git ls-remote --exit-code --tags origin "refs/tags/${tag}"' in condition
    # The tags endpoint never returns a draft to this token, so a staged draft
    # reads as "none"; anything but a published release may resume.
    assert '[ "${release}" != false ]' in condition
    assert _jobs()["gate"]["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    # A workflow_run (or push) in this state builds nothing and warns; only a
    # dispatch goes on to resume.
    quiet, resume = branch.split("# Resume with the bytes already attested", 1)
    assert 'if [ "${EVENT_NAME}" != workflow_dispatch ]; then' in quiet
    assert "::warning::" in quiet
    assert 'echo "release=false" >> "${GITHUB_OUTPUT}"' in quiet
    assert "exit 0" in quiet
    assert "resume=true" not in quiet
    assert 'echo "resume=true"' in resume


def test_a_dispatch_always_reaches_the_release_state_check() -> None:
    # With the workflow_run gate, the state step runs only for a candidate; a
    # dispatch is always one, so a resume never depends on the subject.
    run = _steps("gate")["Choose the commit whose checks decide the release"]["run"]
    dispatch = run.split("\nelse\n", 1)[1]
    assert "candidate=false" not in dispatch
    assert run.rstrip().endswith('echo "candidate=true" >> "${GITHUB_OUTPUT}"')
    state = _steps("gate")[STATE]
    assert state["if"] == "steps.subject.outputs.candidate == 'true'"


def test_a_resume_builds_nothing_and_names_the_tags_commit() -> None:
    branch = _resume_branch()
    assert 'echo "release=false"' in branch
    assert 'echo "resume=true"' in branch
    assert 'commit="$(git rev-list -n 1 "refs/tags/${tag}")"' in branch
    assert 'git merge-base --is-ancestor "${commit}" refs/remotes/origin/main' in branch
    assert 'echo "resume-commit=${commit}"' in branch
    outputs = _jobs()["gate"]["outputs"]
    assert outputs["resume"] == "${{ steps.state.outputs.resume }}"
    assert outputs["resume-commit"] == "${{ steps.state.outputs.resume-commit }}"
    jobs = _jobs()
    for name in ("build-python", "build-macos", "build-windows", "build-linux"):
        assert jobs[name]["if"] == "needs.gate.outputs.release == 'true'"
    # attest needs the builds, stage needs attest, and publish-pypi needs
    # stage; none of them overrides the default success() condition.
    for name in ("attest", "stage", "publish-pypi"):
        assert "always()" not in str(jobs[name].get("if", ""))
        assert "cancelled()" not in str(jobs[name].get("if", ""))


def test_every_other_partly_published_state_still_refuses() -> None:
    run = _steps("gate")[STATE]["run"]
    refusal = run[run.index('elif [ "${pypi}" = 200 ] || [ "${pypi}" = 404 ]; then') :]
    assert "is partly published" in refusal
    assert "do not rebuild" in refusal
    assert "exit 1" in refusal


def test_publish_runs_after_pypi_or_on_a_resume() -> None:
    publish = _jobs()["publish"]
    assert publish["needs"] == ["gate", "stage", "publish-pypi"]
    condition = " ".join(publish["if"].split())
    assert condition.startswith("!cancelled() && needs.gate.result == 'success' && (")
    assert (
        "(needs.gate.outputs.release == 'true' && needs.publish-pypi.result == 'success')"
        in condition
    )
    assert "|| needs.gate.outputs.resume == 'true'" in condition
    assert publish["permissions"] == {"attestations": "read", "contents": "read"}


def test_publish_requires_the_release_app_identity() -> None:
    steps = list(_steps("publish"))
    identity = "Require the exact release App identity"
    assert steps.index(identity) < steps.index(PUBLISH)
    assert "openadapt-release" in _steps("publish")[identity]["run"]


def test_a_resume_reverifies_the_draft_and_pypi_before_publishing() -> None:
    steps = _steps("publish")
    order = list(steps)
    assert order.index(RESUME) < order.index(PUBLISH)
    assert steps[DOWNLOAD]["if"] == "needs.gate.outputs.resume != 'true'"
    resume = steps[RESUME]
    assert resume["if"] == "needs.gate.outputs.resume == 'true'"
    assert resume["env"]["RESUME_COMMIT"] == "${{ needs.gate.outputs.resume-commit }}"
    run = resume["run"]
    assert 'if [ "${state}" != draft ]; then' in run
    assert "release_publication.py reverify" in run
    assert '--source-commit "${RESUME_COMMIT}"' in run
    assert "--upload upload" in run
    assert "python-dist" in run
    assert "--directory pypi-dist --checksums upload/SHA256SUMS" in run
    # The publish step that follows verifies the draft against `upload` again.
    assert "--expected-dir upload" in steps[PUBLISH]["run"]


def test_the_pypi_check_compares_against_the_attested_sha256sums() -> None:
    run = _steps("publish-pypi")["Require PyPI to serve exactly these bytes"]["run"]
    assert "--directory pypi-dist --checksums release-assets/SHA256SUMS" in run


def test_a_resume_is_bound_to_the_tag_not_to_mains_current_tree() -> None:
    # Desktop changes merged after 0.22.0 must not block its resume: the
    # resume publishes bytes already built and attested for the tag's commit.
    # Only a new release requires main's Desktop tree to be unchanged.
    gate = _steps("gate")
    for name in (
        "Require one version everywhere, its changelog, and a new tag in order",
        "Require that commit's Packages run and its desktop jobs to have passed",
        "Require that main has not changed Desktop since that commit",
    ):
        assert gate[name]["if"] == "steps.state.outputs.release == 'true'", name
    # A resume leaves the release output empty, so nothing builds.
    assert _jobs()["gate"]["outputs"]["release"] == "${{ steps.fresh.outputs.release }}"
    branch = _resume_branch()
    assert "git diff" not in branch
    assert "GITHUB_SHA" not in branch
    run = _steps("publish")[RESUME]["run"]
    assert "GITHUB_SHA" not in run
    assert "git diff" not in run
