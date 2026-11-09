"""The release-health check flags README install commands that PyPI cannot satisfy."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_release_health.py"
README = ROOT / "README.md"


def _load():
    spec = importlib.util.spec_from_file_location("check_release_health", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(**override):
    state = {
        "now": "2026-10-09T20:00:00+00:00",
        "main_sha": "a" * 40,
        "main_test": {"status": "completed", "conclusion": "success"},
        "latest_tag": "v1.2.2",
        "latest_version": "1.2.2",
        "latest_tag_date": "2026-07-28T03:50:00+00:00",
        "release_tags": ["v1.2.2"],
        "pypi": {
            "reachable": True,
            "latest": "1.2.2",
            "versions": ["1.2.0", "1.2.1", "1.2.2"],
            "installable": ["1.2.0", "1.2.1", "1.2.2"],
        },
        "release_runs": [],
        "commits_since_tag": [],
        "readme_pins": [],
    }
    state.update(override)
    return state


def test_self_test_passes():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_readme_pin_with_no_published_version_alerts():
    health = _load()
    text = 'python -m pip install "openadapt-capture>=1.3.0,<1.4"\n'

    alerts, _ = health.evaluate(_state(readme_pins=health.readme_pins(text)))

    assert len(alerts) == 1
    assert "README ahead of PyPI" in alerts[0]
    assert "openadapt-capture>=1.3.0,<1.4" in alerts[0]
    assert "1.2.2" in alerts[0]
    assert not health.can_close_issue(_state(), alerts)


def test_readme_pin_on_a_published_version_is_quiet():
    health = _load()

    alerts, notes = health.evaluate(_state(readme_pins=[">=1.2.0,<1.3"]))

    assert alerts == []
    assert any("resolves on PyPI to 1.2.2" in note for note in notes)


def test_every_readme_pin_is_one_the_check_can_evaluate():
    health = _load()

    for pin in health.readme_pins(README.read_text(encoding="utf-8")):
        assert health.parse_specifier(pin) is not None, pin


def test_live_state_reads_the_repository_readme(monkeypatch):
    health = _load()
    responses = {
        f"/repos/{health.REPOSITORY}/commits/main": {"sha": "a" * 40},
        f"/repos/{health.REPOSITORY}/tags": [],
        f"/repos/{health.REPOSITORY}/releases": [],
        f"/repos/{health.REPOSITORY}/actions/workflows/test.yml/runs": {"workflow_runs": []},
        f"/repos/{health.REPOSITORY}/actions/workflows/release.yml/runs": {"workflow_runs": []},
    }
    monkeypatch.setattr(health, "github_get", lambda path, params=None: responses[path])
    monkeypatch.setattr(
        health, "fetch_pypi_versions", lambda: {"reachable": True, "versions": []}
    )

    state = health.collect_live_state()

    assert state["readme_pins"] == health.readme_pins(README.read_text(encoding="utf-8"))


def _failed_release_run(**override):
    run = {
        "id": 33195841524,
        "head_branch": "main",
        # An older main commit: the dispatch predates the current main head.
        "head_sha": "c" * 40,
        "status": "completed",
        "conclusion": "failure",
        "created_at": "2026-08-28T17:41:29Z",
        "failed_steps": [
            {"job": "create-release-tag", "step": "Create the exact release App token"}
        ],
    }
    run.update(override)
    return run


def _unreleased_commit():
    return {
        "sha": "b" * 40,
        "message": "feat: install FFmpeg with one command",
        "date": "2026-09-01T00:00:00+00:00",
    }


def test_release_app_token_failure_is_named_in_the_alert():
    health = _load()

    alerts, _ = health.evaluate(
        _state(
            release_runs=[_failed_release_run()],
            commits_since_tag=[_unreleased_commit()],
        )
    )

    blocked = [alert for alert in alerts if "Release blocked" in alert]
    assert len(blocked) == 1, alerts
    assert "33195841524" in blocked[0]
    assert "2026-08-28" in blocked[0]
    assert "Create the exact release App token" in blocked[0]
    assert "installed on OpenAdaptAI/openadapt-capture" in blocked[0]
    assert "dispatch release.yml again" in blocked[0]
    assert not health.can_close_issue(_state(), alerts)


def test_other_failed_release_step_is_named_without_app_advice():
    health = _load()
    run = _failed_release_run(
        failed_steps=[
            {
                "job": "create-release-tag",
                "step": "Require exact-main test, hosted, and live Linux evidence",
            }
        ]
    )

    alerts, _ = health.evaluate(
        _state(release_runs=[run], commits_since_tag=[_unreleased_commit()])
    )

    failed = [alert for alert in alerts if "Last release run failed" in alert]
    assert len(failed) == 1, alerts
    assert "Require exact-main test, hosted, and live Linux evidence" in failed[0]
    assert "create-release-tag" in failed[0]
    assert not any("GitHub App" in alert for alert in alerts)


def test_app_token_failure_in_the_publish_job_gets_no_release_identity_advice():
    health = _load()
    # publish-tag has a step with the same name, but it reads the pypi environment.
    run = _failed_release_run(
        failed_steps=[{"job": "publish-tag", "step": "Create the exact release App token"}]
    )

    alerts, _ = health.evaluate(
        _state(release_runs=[run], commits_since_tag=[_unreleased_commit()])
    )

    assert not any("release-identity" in alert for alert in alerts), alerts
    failed = [alert for alert in alerts if "Last release run failed" in alert]
    assert len(failed) == 1, alerts
    assert '"Create the exact release App token" in job publish-tag' in failed[0]


def test_failed_release_run_with_no_failed_step_is_still_named():
    health = _load()
    # A job can fail before any step runs, for example on an environment rule.
    run = _failed_release_run(failed_steps=[])

    alerts, _ = health.evaluate(
        _state(release_runs=[run], commits_since_tag=[_unreleased_commit()])
    )

    failed = [alert for alert in alerts if "Last release run failed" in alert]
    assert len(failed) == 1, alerts
    assert "33195841524" in failed[0]
    assert "no failed step" in failed[0]
    assert not any("Release blocked" in alert for alert in alerts), alerts


def test_release_failure_is_quiet_when_nothing_waits_for_release():
    health = _load()

    alerts, _ = health.evaluate(_state(release_runs=[_failed_release_run()]))

    assert alerts == []


def test_successful_release_after_the_failure_clears_the_blocker():
    health = _load()
    succeeded = _failed_release_run(
        id=33300000000, conclusion="success", failed_steps=[], head_sha="a" * 40
    )

    alerts, _ = health.evaluate(
        _state(
            release_runs=[succeeded, _failed_release_run()],
            commits_since_tag=[_unreleased_commit()],
        )
    )

    assert not any("Release blocked" in alert for alert in alerts), alerts


def test_cancelled_release_run_is_not_called_a_failure():
    health = _load()
    cancelled = _failed_release_run(conclusion="cancelled")

    alerts, _ = health.evaluate(
        _state(release_runs=[cancelled], commits_since_tag=[_unreleased_commit()])
    )

    assert not any("Release blocked" in a or "Last release run failed" in a for a in alerts)


def test_unreadable_release_steps_are_reported_not_guessed():
    health = _load()

    alerts, notes = health.evaluate(
        _state(
            release_runs=[_failed_release_run(failed_steps=None)],
            commits_since_tag=[_unreleased_commit()],
        )
    )

    assert not any("Release blocked" in alert for alert in alerts), alerts
    assert any("couldn't read the steps of release run 33195841524" in n for n in notes)


def _live_responses(health, release_runs, jobs):
    responses = {
        f"/repos/{health.REPOSITORY}/commits/main": {"sha": "a" * 40},
        f"/repos/{health.REPOSITORY}/tags": [],
        f"/repos/{health.REPOSITORY}/releases": [],
        f"/repos/{health.REPOSITORY}/actions/workflows/test.yml/runs": {"workflow_runs": []},
        f"/repos/{health.REPOSITORY}/actions/workflows/release.yml/runs": {
            "workflow_runs": release_runs
        },
    }
    for run_id, payload in jobs.items():
        responses[f"/repos/{health.REPOSITORY}/actions/runs/{run_id}/jobs"] = payload
    return responses


def test_live_state_reads_the_failed_steps_of_the_latest_release_run(monkeypatch):
    health = _load()
    run = _failed_release_run()
    del run["failed_steps"]
    jobs = {
        run["id"]: {
            "jobs": [
                {"name": "validate-dispatch", "conclusion": "success", "steps": []},
                {
                    "name": "create-release-tag",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Set up job", "conclusion": "success"},
                        {"name": "Create the exact release App token", "conclusion": "failure"},
                        {"name": "Set up Python", "conclusion": "skipped"},
                    ],
                },
            ]
        }
    }
    responses = _live_responses(health, [run], jobs)
    monkeypatch.setattr(health, "github_get", lambda path, params=None: responses[path])
    monkeypatch.setattr(health, "fetch_pypi_versions", lambda: {"reachable": True, "versions": []})

    state = health.collect_live_state()

    assert state["release_runs"][0]["failed_steps"] == [
        {"job": "create-release-tag", "step": "Create the exact release App token"}
    ]


def test_live_state_survives_unreadable_release_jobs(monkeypatch):
    import urllib.error

    health = _load()
    run = _failed_release_run()
    del run["failed_steps"]
    responses = _live_responses(health, [run], {})

    def github_get(path, params=None):
        if path.endswith("/jobs"):
            raise urllib.error.URLError("jobs endpoint unavailable")
        return responses[path]

    monkeypatch.setattr(health, "github_get", github_get)
    monkeypatch.setattr(health, "fetch_pypi_versions", lambda: {"reachable": True, "versions": []})

    state = health.collect_live_state()

    assert state["release_runs"][0]["failed_steps"] is None


def test_live_state_survives_malformed_release_jobs(monkeypatch):
    health = _load()
    run = _failed_release_run()
    del run["failed_steps"]
    responses = _live_responses(
        health, [run], {run["id"]: {"jobs": [{"name": "create-release-tag", "steps": 7}]}}
    )
    monkeypatch.setattr(health, "github_get", lambda path, params=None: responses[path])
    monkeypatch.setattr(health, "fetch_pypi_versions", lambda: {"reachable": True, "versions": []})

    state = health.collect_live_state()

    assert state["release_runs"][0]["failed_steps"] is None


def test_release_workflow_still_has_the_app_token_step_the_check_names():
    health = _load()
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    marker = f"\n  {health.APP_TOKEN_JOB}:\n"
    assert marker in workflow
    create_tag = workflow.split(marker, 1)[1].split("\n  validate-tag:\n")[0]
    job_keys = create_tag.split("\n    steps:\n", 1)[0]

    assert f"- name: {health.APP_TOKEN_STEP}\n" in create_tag
    # The Actions API reports a job by its id only while the job has no name.
    assert "\n    name:" not in "\n" + job_keys
    # The alert tells people to check the credentials in this environment.
    assert "\n    environment: release-identity" in "\n" + job_keys
