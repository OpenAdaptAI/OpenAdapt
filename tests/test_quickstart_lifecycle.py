from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quickstart_lifecycle.py"
WORKFLOW = ROOT / ".github" / "workflows" / "quickstart-lifecycle.yml"


def _module():
    spec = importlib.util.spec_from_file_location("launcher_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_verified_quickstart(root: Path) -> None:
    run = root / "run"
    run.mkdir(parents=True)
    (run / "report.json").write_text(
        json.dumps(
            {
                "execution_outcome": "VERIFIED",
                "execution_profile": "standard",
                "transaction_outcome": "VERIFIED",
                "model_calls": 0,
                "outcome_envelope": {
                    "required_contracts": {"effect": 2},
                    "passed_contracts": {"effect": 2},
                },
            }
        ),
        encoding="utf-8",
    )
    (run / "receipt.json").write_text(
        json.dumps({"outcome": "VERIFIED", "provenance": "synthetic-tutorial"}),
        encoding="utf-8",
    )
    (run / "REPORT.md").write_text("# VERIFIED\n", encoding="utf-8")
    (run / "receipt.md").write_text("# VERIFIED\n", encoding="utf-8")
    (run / "receipt.png").write_bytes(b"png")


def test_inspector_requires_a_verified_standard_effect(tmp_path):
    lifecycle = _module()
    _write_verified_quickstart(tmp_path)

    summary = lifecycle._inspect_quickstart(tmp_path)

    assert summary["outcome"] == "VERIFIED"
    assert summary["effect_contracts"] == 2

    report = tmp_path / "run" / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["transaction_outcome"] = "COMPLETED_UNVERIFIED"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AssertionError, match="transaction outcome"):
        lifecycle._inspect_quickstart(tmp_path)


def test_zero_exit_with_an_unhandled_async_error_fails(tmp_path, monkeypatch):
    lifecycle = _module()
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="VERIFIED\nTask was destroyed but it is pending!\n"
        ),
    )

    with pytest.raises(RuntimeError, match="unhandled runtime error"):
        lifecycle._run(
            ["openadapt", "quickstart"],
            cwd=tmp_path,
            env={},
            log=tmp_path / "quickstart.log",
        )


def test_browser_dependency_setup_does_not_download_chromium(tmp_path, monkeypatch):
    lifecycle = _module()
    calls = []

    def capture(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(lifecycle, "_run", capture)
    lifecycle._install_browser_system_dependencies(
        Path("/venv/bin/python"),
        cwd=tmp_path,
        env={},
        log=tmp_path / "browser-deps.log",
    )

    assert calls == [
        [
            "/venv/bin/python",
            "-m",
            "playwright",
            "install-deps",
            "chromium",
        ]
    ]


@pytest.mark.parametrize(
    ("output", "expected"), [("absent\n", False), ("present\n", True)]
)
def test_browser_probe_reports_exact_install_state(
    tmp_path, monkeypatch, output, expected
):
    lifecycle = _module()
    monkeypatch.setattr(
        lifecycle,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=output
        ),
    )

    assert (
        lifecycle._browser_present(
            Path("/venv/bin/python"),
            cwd=tmp_path,
            env={},
            log=tmp_path / "browser-probe.log",
        )
        is expected
    )


def test_lifecycle_summary_proves_preflight_and_lazy_install(tmp_path, monkeypatch):
    lifecycle = _module()
    launcher_wheel = tmp_path / "openadapt.whl"
    launcher_wheel.write_bytes(b"launcher")
    work_dir = tmp_path / "run"

    class FakeEnvironment:
        def create(self, root):
            lifecycle._venv_python(root).parent.mkdir(parents=True)
            lifecycle._venv_python(root).touch()
            lifecycle._console(root).touch()

    monkeypatch.setattr(
        lifecycle.venv,
        "EnvBuilder",
        lambda **_kwargs: FakeEnvironment(),
    )

    commands = []

    def successful_run(command, **_kwargs):
        commands.append(command)
        output = (
            lifecycle._LAZY_BROWSER_INSTALL_NOTICE if "quickstart" in command else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(lifecycle, "_run", successful_run)
    browser_states = iter([False, True])
    monkeypatch.setattr(
        lifecycle, "_browser_present", lambda *_a, **_k: next(browser_states)
    )
    monkeypatch.setattr(
        lifecycle,
        "_inspect_quickstart",
        lambda _root: {"outcome": "VERIFIED"},
    )

    summary = lifecycle.run_lifecycle(
        launcher_wheel,
        work_dir,
        flow_wheel=None,
        browser_system_deps=True,
        source_revision="abc123",
    )

    assert summary["browser_preflight"] == {
        "system_dependencies": "installed-via-playwright",
        "doctor": "passed-before-browser-download",
        "chromium_present_before_quickstart": False,
        "chromium_present_after_quickstart": True,
        "lazy_install_performed": True,
        "lazy_install_notice_seen": True,
    }
    written = json.loads((work_dir / "summary.json").read_text(encoding="utf-8"))
    assert written["browser_preflight"] == summary["browser_preflight"]
    assert any("install-deps" in command for command in commands)
    assert not any("--with-deps" in command for command in commands)


def test_workflow_runs_the_public_command_in_one_bounded_weekly_job():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = document[True]
    jobs = document["jobs"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert list(jobs) == ["quickstart"]
    steps = jobs["quickstart"]["steps"]
    run = next(step["run"] for step in steps if step.get("name", "").startswith("Run"))
    assert "scripts/quickstart_lifecycle.py" in run
    assert "--browser-system-deps" in run
    assert "--browser-with-deps" not in run
    assert jobs["quickstart"]["runs-on"] == "ubuntu-latest"


def test_workflow_pins_actions_to_full_commit_shas():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_refs = re.findall(r"(?m)^\s*uses:\s+\S+@([^\s#]+)", workflow)

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
