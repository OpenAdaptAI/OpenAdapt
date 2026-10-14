"""WAA readiness must include a verified unlocked desktop."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "run_dc_eval", Path(__file__).parents[1] / "scripts" / "run_dc_eval.py"
)
assert _SPEC and _SPEC.loader
run_dc_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_dc_eval)


def test_lock_state_query_failure_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        run_dc_eval.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(ok=False, status_code=503, text="unavailable"),
    )

    assert run_dc_eval._dismiss_lock_screen("http://waa") is False


def test_unreadable_lock_state_is_not_ready(monkeypatch):
    monkeypatch.setattr(
        run_dc_eval.requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {}),
    )

    assert run_dc_eval._dismiss_lock_screen("http://waa") is False


def test_unlock_must_be_confirmed(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {"output": "True"}),
            SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {}),
            SimpleNamespace(ok=True, status_code=200, text="", json=lambda: {"output": "True"}),
        ]
    )
    monkeypatch.setattr(run_dc_eval.requests, "post", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(run_dc_eval.time, "sleep", lambda seconds: None)

    assert run_dc_eval._dismiss_lock_screen("http://waa") is False


def test_probe_success_still_requires_desktop_readiness(monkeypatch):
    monkeypatch.setattr(run_dc_eval, "_probe", lambda *args, **kwargs: True)
    monkeypatch.setattr(run_dc_eval, "_dismiss_lock_screen", lambda server: False)

    assert (
        run_dc_eval.ensure_waa_ready("http://waa", "user", "10.0.0.1", evaluate_url="http://eval")
        is False
    )
