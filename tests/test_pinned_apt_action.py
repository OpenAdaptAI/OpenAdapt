"""Infrastructure action identity is independent of the checked-out subject."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_release_workflows_cache_free as release_contract
import yaml
from _apt_action_reference import APT_ACTION_REF, APT_ACTION_SOURCE

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "apt-install"
SUBJECT_REFS = [
    "refs/pull/17/head",
    "refs/tags/old-release",
    "refs/heads/gh-readonly-queue/main/17",
]


def test_all_production_callers_pin_the_reviewed_action():
    production = []
    local_selftests = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        data = yaml.safe_load(workflow.read_text())
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if uses == "./.github/actions/apt-install":
                    assert workflow.name == "apt-install-selftest.yml"
                    local_selftests.append(uses)
                elif uses.startswith(APT_ACTION_SOURCE):
                    assert uses == APT_ACTION_REF, (workflow.name, uses)
                    production.append(workflow.name)
    assert set(production) == {
        "apt-cache-seed.yml",
        "desktop-build.yml",
        "desktop-release.yml",
        "desktop-replay.yml",
        "packages.yml",
        "quickstart-lifecycle.yml",
    }
    # + the flow unit shards' tesseract, and the default-at-rest shards' (#2423)
    assert len(production) == 13
    assert len(local_selftests) == 5  # Exercise future helper edits, not the pin.


def _isolated_action(tmp_path: Path, subject_ref: str):
    action = tmp_path / "downloaded action"
    action.mkdir()
    for name in ("action.yml", "apt_install.py"):
        shutil.copy2(ACTION / name, action / name)
    subject = tmp_path / "unrelated subject checkout"
    subject.mkdir()
    # A subject-relative script must never replace the downloaded helper.
    (subject / "apt_install.py").write_text(
        "raise AssertionError('subject helper used')"
    )
    assert not (subject / ".github").exists()
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    dpkg = commands / "dpkg"
    dpkg.write_text("#!/bin/sh\nprintf 'amd64\\n'\n")
    dpkg.chmod(0o755)
    (tmp_path / "runner").mkdir()
    env = {
        "PATH": f"{commands}{os.pathsep}{os.defpath}",
        "GITHUB_ACTION_PATH": str(action),
        "GITHUB_REF": subject_ref,
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "ImageOS": "ubuntu24",
        "ImageVersion": "synthetic",
        "PACKAGES": "xvfb dbus xvfb",
        "PLAYWRIGHT_DEPS": "",
        "PLAYWRIGHT_PYTHON": "python3",
        "RECOMMENDS": "false",
        "KEY_SUFFIX": "",
    }
    steps = yaml.safe_load((action / "action.yml").read_text())["runs"]["steps"]
    return subject, env, {step["id"]: step for step in steps if "run" in step}


@pytest.mark.parametrize("subject_ref", SUBJECT_REFS)
def test_action_plan_runs_outside_the_subject_checkout(tmp_path, subject_ref):
    subject, env, steps = _isolated_action(tmp_path, subject_ref)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", steps["plan"]["run"]],
        cwd=subject,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = dict(
        line.split("=", 1)
        for line in Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    )
    assert output["packages"] == "dbus xvfb"
    assert output["key"].startswith("apt-install-v1-ubuntu24-amd64-")
    assert output["key"].endswith("-synthetic")
    assert str(tmp_path / "runner") in output["dir"]


@pytest.mark.parametrize("subject_ref", SUBJECT_REFS)
def test_action_reports_install_refusal_outside_subject_checkout(tmp_path, subject_ref):
    subject, env, steps = _isolated_action(tmp_path, subject_ref)
    env.update(
        RUNNER_OS="macOS",  # Refuse before any apt/root/network operation.
        CACHE_DIR=str(tmp_path / "cache"),
        CACHE_HIT="none",
        REFRESH="false",
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", steps["install"]["run"]],
        cwd=subject,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "::error title=apt-install::" in result.stdout
    record = json.loads((tmp_path / "runner" / "apt-install-result.json").read_text())
    assert record["reason"] == "not-apt"
    assert "reason=not-apt" in Path(env["GITHUB_OUTPUT"]).read_text()


@pytest.mark.parametrize("reference", ["./.github/actions/apt-install", APT_ACTION_REF])
@pytest.mark.parametrize("cache", [None, "true", "false"])
def test_release_cache_guard_covers_local_and_pinned_helpers(
    tmp_path, monkeypatch, reference, cache
):
    workflow = tmp_path / "release.yml"
    text = f"steps:\n  - uses: {reference}\n"
    if cache is not None:
        text += f'    with:\n      use-cache: "{cache}"\n'
    workflow.write_text(text)
    monkeypatch.setattr(release_contract, "GITHUB", tmp_path)
    if cache == "false":
        release_contract.test_releases_turn_off_the_cache_of_cache_optional_actions(
            "release.yml"
        )
    else:
        with pytest.raises(AssertionError):
            release_contract.test_releases_turn_off_the_cache_of_cache_optional_actions(
                "release.yml"
            )


@pytest.mark.parametrize("ref", ["main", "v1", "0" * 40])
def test_release_guard_rejects_unreviewed_helper_refs(ref):
    with pytest.raises(AssertionError, match="unreviewed infrastructure helper ref"):
        release_contract._cache_optional_action(f"{APT_ACTION_SOURCE}@{ref}")
