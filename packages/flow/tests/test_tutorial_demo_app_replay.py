"""Regression tests for `flow replay --demo-app` on tutorial bundles (#2408)."""

from __future__ import annotations

from types import SimpleNamespace

from openadapt_flow.tutorial import (
    demo_app_entry_url,
    demo_app_is_tasks_bundle,
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
