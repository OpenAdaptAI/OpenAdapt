"""How a Desktop release chooses its commit and the checks that decide it.

A release starts when Packages finishes successfully for a push to main
(workflow_run), on a schedule, or on a dispatch. It used to start on the push
and poll for that commit's Packages run for 60 minutes; under CI congestion the
run sat queued longer than that and release run 39368396653 refused 0.22.0
(2026-10-20).

Every event takes the same subject: the newest first-parent commit that
changed Desktop, up to the tip the run builds. Its latest successful Packages
push run decides the release, whichever run started this one. The subject used
to be the commit of the run that started the release, so when a newer push
cancelled the Desktop commit's run, no later run released it (0.23.0 and
0.24.1, 2026-10-25). Only commits on main's first-parent history get a Packages
push run; choosing one from inside a merged branch made the gate wait for a run
that could not exist (2026-10-20, 0.21.1).
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/desktop-release.yml"


def _workflow() -> dict:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _gate() -> dict:
    return _workflow()["jobs"]["gate"]


def _gate_steps() -> dict[str, dict]:
    return {
        str(step.get("name") or step.get("uses")): step for step in _gate()["steps"]
    }


SUBJECT_STEP = "Choose the commit whose checks decide the release"
STATE_STEP = "Read the version and the state of its release"
ORDER_STEP = "Require one version everywhere, its changelog, and a new tag in order"
PACKAGES_STEP = "Require that commit's Packages run and its desktop jobs to have passed"
FRESH_STEP = "Require that main has not changed Desktop since that commit"


def test_a_release_starts_when_packages_completes_on_main() -> None:
    trigger = _workflow()[True]
    # No push trigger: a push-triggered run had to wait for Packages, and its
    # queue time could fail the release. The schedule picks up a Desktop commit
    # whose Packages run passed only on a re-run.
    assert set(trigger) == {"workflow_run", "schedule", "workflow_dispatch"}
    assert trigger["schedule"] == [{"cron": "41 */3 * * *"}]
    assert trigger["workflow_run"] == {
        "workflows": ["Packages"],
        "types": ["completed"],
        "branches": ["main"],
    }


def test_only_a_successful_push_run_on_this_repositorys_main_can_release() -> None:
    condition = " ".join(_gate()["if"].split())
    assert condition.startswith("github.ref == 'refs/heads/main' && (")
    assert "github.event_name == 'workflow_dispatch' ||" in condition
    assert "github.event_name == 'schedule' ||" in condition
    for clause in (
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert f"{clause} &&" in condition or f"{clause} )" in condition, clause


def test_a_pending_release_is_queued_not_cancelled() -> None:
    # Every Packages run on main starts a run here. Without a queue, a newer
    # pending run cancels the release waiting before it.
    assert _workflow()["concurrency"] == {
        "group": "desktop-release",
        "cancel-in-progress": False,
        "queue": "max",
    }


def test_every_event_takes_the_newest_first_parent_desktop_commit() -> None:
    gate = _gate()
    assert (
        gate["env"]["PACKAGES_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    )
    assert "PACKAGES_RUN_ID" not in gate["env"]
    run = _gate_steps()[SUBJECT_STEP]["run"]
    # One subject rule for every event, never the run that started this one.
    assert run.count('subject="$(git log') == 1
    assert 'subject="$(git log -1 --first-parent --format=%H "${GITHUB_SHA}" -- .)"' in run
    assert 'subject="${PACKAGES_HEAD_SHA}"' not in run
    assert 'subject="${GITHUB_SHA}"' not in run
    # The run that started this one must still be on the history that is built.
    assert 'git merge-base --is-ancestor "${PACKAGES_HEAD_SHA}" "${GITHUB_SHA}"' in run
    assert "head_branch" not in run


def test_dispatch_subject_follows_first_parent_history() -> None:
    lines = [
        line.strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('subject="$(git log')
    ]
    assert lines, "the release gate no longer chooses a subject commit with git log"
    for line in lines:
        assert "--first-parent" in line, line


def test_only_a_history_without_desktop_changes_is_not_a_candidate() -> None:
    steps = list(_gate_steps())
    # Classify first, so a run without a subject never reaches the release
    # state check (which refuses a partly published version loudly).
    assert steps.index(SUBJECT_STEP) < steps.index(STATE_STEP)
    run = _gate_steps()[SUBJECT_STEP]["run"]
    assert run.count('echo "candidate=false" >> "${GITHUB_OUTPUT}"') == 1
    assert "No release:" in run
    assert (
        _gate_steps()[STATE_STEP]["if"] == "steps.subject.outputs.candidate == 'true'"
    )
    for name in (ORDER_STEP, PACKAGES_STEP):
        assert _gate_steps()[name]["if"] == "steps.state.outputs.release == 'true'", (
            name
        )
    assert _gate_steps()[FRESH_STEP]["if"] == (
        "steps.state.outputs.release == 'true' && steps.packages.outputs.ready == 'true'"
    )


def test_a_partly_published_version_warns_on_unrelated_runs() -> None:
    run = _gate_steps()[STATE_STEP]["run"]
    assert _gate_steps()[STATE_STEP]["env"]["SUBJECT"] == "${{ steps.subject.outputs.sha }}"
    # Warns on another commit's Packages run or a schedule; still fails a
    # dispatch and the Desktop commit's own run.
    quiet = (
        'if [ "${EVENT_NAME}" != workflow_dispatch ] && '
        '[ "${PACKAGES_HEAD_SHA:-}" != "${SUBJECT}" ]; then'
    )
    warned, loud = run.split(quiet, 1)[1].split("\n  fi\n", 1)
    assert "::warning::" in warned and 'echo "release=false"' in warned
    assert "exit 1" in loud


def test_an_already_released_version_is_quietly_no_release() -> None:
    run = _gate_steps()[STATE_STEP]["run"]
    assert 'echo "release=false" >> "${GITHUB_OUTPUT}"' in run
    # The gate's release output comes from the last check, which runs only
    # after the state step said release=true.
    assert _gate()["outputs"]["release"] == "${{ steps.fresh.outputs.release }}"
    assert _gate_steps()[FRESH_STEP]["id"] == "fresh"


def test_a_partly_published_version_refuses_a_rebuild() -> None:
    run = _gate_steps()[STATE_STEP]["run"]
    assert "is partly published" in run
    assert "do not rebuild" in run


def test_version_changelog_and_tag_order_still_gate_the_release() -> None:
    run = _gate_steps()[ORDER_STEP]["run"]
    for check in (
        "check_release_consistency.py",
        "validate-tag",
        "CHANGELOG.md",
        "validate-release-order",
    ):
        assert check in run, check


def test_the_subjects_latest_successful_push_run_decides() -> None:
    step = _gate_steps()[PACKAGES_STEP]
    assert step["id"] == "packages"
    run = step["run"]
    lookup = run.split('passed="$(gh api', 1)[1].split(")\"", 1)[0]
    for clause in (
        'head_sha="${SUBJECT}"',
        "event=push",
        "status=success",
        ".head_sha == env.SUBJECT",
        '.event == "push"',
        '.head_branch == "main"',
        '.conclusion == "success"',
        ".head_repository.full_name == env.GITHUB_REPOSITORY",
    ):
        assert clause in lookup, clause
    assert 'require_desktop_jobs "${passed}"' in run
    assert run.count('echo "ready=true" >> "${GITHUB_OUTPUT}"') == 2
    # Without a successful run, a workflow_run or schedule stops quietly and a
    # later one releases; it never waits.
    quiet = run.split('if [ "${EVENT_NAME}" != workflow_dispatch ]; then', 1)[1]
    quiet = quiet.split("\nfi\n", 1)[0]
    assert 'echo "ready=false" >> "${GITHUB_OUTPUT}"' in quiet
    assert "::warning::" in quiet
    assert "sleep" not in quiet


def test_the_desktop_test_and_build_jobs_must_have_passed() -> None:
    run = _gate_steps()[PACKAGES_STEP]["run"]
    assert 'for prefix in "desktop test / " "desktop build / "; do' in run
    assert "^(failure|cancelled|timed_out)\\tdesktop (test|build) / " in run
    assert run.count("require_desktop_jobs ") == 2


def test_a_dispatch_waits_longer_while_packages_is_queued() -> None:
    run = _gate_steps()[PACKAGES_STEP]["run"]
    assert "queued_deadline=$((SECONDS + 4 * 3600))" in run
    assert 'running_deadline="${running_deadline:-$((SECONDS + 3600))}"' in run
    # The job outlives both bounds.
    assert _gate()["timeout-minutes"] > 4 * 60 + 60


def test_a_newer_desktop_change_on_main_stops_the_older_release() -> None:
    run = _gate_steps()[FRESH_STEP]["run"]
    assert "git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main" in run
    assert 'for later in refs/remotes/origin/main "${GITHUB_SHA}"; do' in run
    # Quiet for workflow_run and schedule, whose newer commit releases itself;
    # loud for a dispatch, which a person is watching.
    quiet, loud = run.split('echo "release=false" >> "${GITHUB_OUTPUT}"', 1)
    assert 'if [ "${EVENT_NAME}" != workflow_dispatch ]; then' in quiet
    assert "exit 1" in loud
    assert run.rstrip().endswith('echo "release=true" >> "${GITHUB_OUTPUT}"')


def test_tagging_still_requires_the_release_app_identity() -> None:
    stage = {
        str(step.get("name") or step.get("uses")): step
        for step in _workflow()["jobs"]["stage"]["steps"]
    }
    assert "openadapt-release" in stage["Require the exact release App identity"]["run"]
