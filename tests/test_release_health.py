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
