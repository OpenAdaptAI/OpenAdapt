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


def _write_verified_quickstart(root: Path, name: str = "run-1") -> None:
    run = root / name
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

    _write_verified_quickstart(tmp_path, "run-2")

    summary = lifecycle._inspect_quickstart(tmp_path)

    assert summary["outcome"] == "VERIFIED"
    assert summary["effect_contracts"] == 2
    assert summary["verified_runs"] == 2

    report = tmp_path / "run-2" / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["transaction_outcome"] = "COMPLETED_UNVERIFIED"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AssertionError, match="transaction outcome"):
        lifecycle._inspect_quickstart(tmp_path)


def test_inspector_reads_the_engine_tutorial_layout(tmp_path):
    lifecycle = _module()
    _write_verified_quickstart(tmp_path, "run")
    assert lifecycle._inspect_quickstart(tmp_path)["verified_runs"] == 1


def test_inspector_requires_the_silent_failure_to_stop(tmp_path):
    lifecycle = _module()
    _write_verified_quickstart(tmp_path)
    broken = tmp_path / "run-broken"
    broken.mkdir()
    (broken / "report.json").write_text(
        json.dumps({"execution_outcome": "HALTED"}), encoding="utf-8"
    )
    assert lifecycle._inspect_quickstart(tmp_path)["silent_failure_stopped"] is True
    (broken / "report.json").write_text(
        json.dumps({"execution_outcome": "VERIFIED"}), encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="not caught"):
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


def _fake_lifecycle(
    tmp_path,
    monkeypatch,
    *,
    channel="chrome",
    auto_output="",
    bundled_after_auto=False,
):
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

    calls = []

    def successful_run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        output = ""
        if "quickstart" in command:
            auto = str(command[-1]).endswith("-auto")
            output = auto_output if auto else lifecycle._LAZY_BROWSER_INSTALL_NOTICE
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(lifecycle, "_run", successful_run)
    browser_states = iter([False, True, bundled_after_auto])
    monkeypatch.setattr(
        lifecycle, "_browser_present", lambda *_a, **_k: next(browser_states)
    )
    monkeypatch.setattr(
        lifecycle, "_installed_browser_channel", lambda *_a, **_k: channel
    )
    monkeypatch.setattr(
        lifecycle,
        "_inspect_quickstart",
        lambda _root: {"outcome": "VERIFIED"},
    )

    def run():
        return lifecycle.run_lifecycle(
            launcher_wheel,
            work_dir,
            flow_wheel=None,
            browser_system_deps=True,
            source_revision="abc123",
        )

    return lifecycle, run, calls, work_dir


def test_lifecycle_summary_proves_preflight_and_lazy_install(tmp_path, monkeypatch):
    _lifecycle, run, calls, work_dir = _fake_lifecycle(tmp_path, monkeypatch)

    summary = run()

    assert summary["browser_preflight"] == {
        "system_dependencies": "installed-via-playwright",
        "doctor": "passed-before-browser-download",
        "chromium_present_before_quickstart": False,
        "chromium_present_after_quickstart": True,
        "lazy_install_performed": True,
        "lazy_install_notice_seen": True,
    }
    assert summary["auto_browser_pass"] == {
        "browser_setting": "auto",
        "installed_browser_channel": "chrome",
        "outcome": "VERIFIED",
        "download_notice_seen": False,
        "bundled_present_after_quickstart": False,
        "no_download_checked": True,
    }
    written = json.loads((work_dir / "summary.json").read_text(encoding="utf-8"))
    assert written["browser_preflight"] == summary["browser_preflight"]
    assert written["auto_browser_pass"] == summary["auto_browser_pass"]
    commands = [command for command, _env in calls]
    assert any("install-deps" in command for command in commands)
    assert not any("--with-deps" in command for command in commands)

    quickstarts = [(command, env) for command, env in calls if "quickstart" in command]
    assert len(quickstarts) == 2
    (_lazy, lazy_env), (_auto, auto_env) = quickstarts
    assert lazy_env["OPENADAPT_FLOW_BROWSER"] == "bundled"
    assert "OPENADAPT_FLOW_BROWSER" not in auto_env
    assert auto_env["PLAYWRIGHT_BROWSERS_PATH"] != lazy_env["PLAYWRIGHT_BROWSERS_PATH"]


def test_lazy_install_notice_matches_the_headless_shell_download():
    lifecycle = _module()

    assert lifecycle._LAZY_BROWSER_INSTALL_NOTICE == (
        "openadapt-flow: no Chrome or Edge found, so downloading Playwright's "
        "headless Chromium once (about 90 MB). Later runs reuse it."
    )


@pytest.mark.parametrize(
    ("auto_output", "bundled_after_auto", "message"),
    [
        ("LAZY", False, "download notice"),
        ("", True, "downloaded Playwright's Chromium"),
    ],
)
def test_auto_pass_fails_when_it_downloads_despite_installed_chrome(
    tmp_path, monkeypatch, auto_output, bundled_after_auto, message
):
    lifecycle = _module()
    output = lifecycle._LAZY_BROWSER_INSTALL_NOTICE if auto_output else ""
    _lifecycle, run, _calls, _work_dir = _fake_lifecycle(
        tmp_path,
        monkeypatch,
        auto_output=output,
        bundled_after_auto=bundled_after_auto,
    )

    with pytest.raises(AssertionError, match=message):
        run()


def test_auto_pass_skips_the_no_download_check_without_chrome(
    tmp_path, monkeypatch, capsys
):
    lifecycle = _module()
    _lifecycle, run, _calls, _work_dir = _fake_lifecycle(
        tmp_path,
        monkeypatch,
        channel=None,
        auto_output=lifecycle._LAZY_BROWSER_INSTALL_NOTICE,
        bundled_after_auto=True,
    )

    summary = run()

    assert summary["auto_browser_pass"]["no_download_checked"] is False
    assert "SKIP no-download check" in capsys.readouterr().out


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
