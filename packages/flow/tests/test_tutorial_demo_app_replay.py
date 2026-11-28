"""Regression tests for `flow replay --demo-app` on tutorial bundles (#2408)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.tutorial import (
    demo_app_entry_url,
    demo_app_is_tasks_bundle,
    start_demo_app_for_replay,
)


def test_demo_app_entry_url_uses_tasks_and_fault_backend_for_tutorial_bundle() -> None:
    wf = SimpleNamespace(
        name="local-quickstart",
        steps=[
            SimpleNamespace(
                id="step_000",
                intent="click 'Open'",
                effects=[],
                target=SimpleNamespace(text="Open", dom_selector=".open-btn"),
            )
        ],
    )
    assert demo_app_is_tasks_bundle(wf) is True
    assert (
        demo_app_entry_url("http://127.0.0.1:9999/", workflow=wf, drift=None)
        == "http://127.0.0.1:9999/?fault=ok&idempotency=demo#tasks"
    )
    assert (
        demo_app_entry_url("http://127.0.0.1:9999/", workflow=wf, drift="rename")
        == "http://127.0.0.1:9999/?fault=ok&idempotency=demo&drift=rename#tasks"
    )


def test_demo_app_entry_url_keeps_root_for_login_starting_bundle() -> None:
    wf = SimpleNamespace(
        name="login-demo",
        steps=[
            SimpleNamespace(
                id="step_000",
                intent="click 'Sign in'",
                effects=[],
                target=SimpleNamespace(text="Sign in", dom_selector="#login-btn"),
            )
        ],
    )
    assert demo_app_is_tasks_bundle(wf) is False
    assert (
        demo_app_entry_url("http://127.0.0.1:9999/", workflow=wf, drift=None)
        == "http://127.0.0.1:9999/"
    )
    assert (
        demo_app_entry_url("http://127.0.0.1:9999/", workflow=wf, drift="theme")
        == "http://127.0.0.1:9999/?drift=theme"
    )


def test_start_demo_app_for_replay_wires_verifier_for_tasks_bundle_only() -> None:
    tasks_wf = SimpleNamespace(
        name="local-quickstart",
        steps=[
            SimpleNamespace(
                id="step_000",
                intent="click 'Open'",
                effects=[],
                target=SimpleNamespace(text="Open", dom_selector=".open-btn"),
            )
        ],
    )
    url, stop, verifier = start_demo_app_for_replay(workflow=tasks_wf, drift=None)
    try:
        assert url.endswith("/?fault=ok&idempotency=demo#tasks")
        assert verifier is not None
        assert getattr(verifier, "records_path", None) == "/api/db"
        assert getattr(verifier, "records_key", None) == "records"
        assert getattr(verifier, "records_complete", None) is True
    finally:
        stop()

    login_wf = SimpleNamespace(
        name="login-demo",
        steps=[
            SimpleNamespace(
                id="step_000",
                intent="click 'Sign in'",
                effects=[],
                target=SimpleNamespace(text="Sign in", dom_selector="#login-btn"),
            )
        ],
    )
    url, stop, verifier = start_demo_app_for_replay(workflow=login_wf, drift=None)
    try:
        assert "#" not in url
        assert verifier is None
    finally:
        stop()


def test_tutorial_then_demo_app_replay_verifies_effects_in_empty_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`flow tutorial` -> `flow replay tut/bundle --demo-app` completes in empty HOME."""
    pytest.importorskip("playwright")
    from openadapt_flow.__main__ import main as cli_main
    from openadapt_flow.ir import RunReport

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))

    tut_dir = tmp_path / "tut"
    assert cli_main(["tutorial", "--out", str(tut_dir)]) == 0

    run_dir = tmp_path / "replay-run"
    assert (
        cli_main(
            [
                "replay",
                str(tut_dir / "bundle"),
                "--demo-app",
                "--run-dir",
                str(run_dir),
            ]
        )
        == 0
    )

    report = RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )
    assert report.success is True
    assert report.execution_profile == "demo"
    assert report.execution_outcome == "COMPLETED_UNVERIFIED"
    step_005 = next(r for r in report.results if r.step_id == "step_005")
    assert step_005.ok is True
    assert step_005.effect_verified is True
    assert len(step_005.effect_evidence) == 2
    assert all(ev.final_verdict == "confirmed" for ev in step_005.effect_evidence)
