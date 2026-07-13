"""Behavior + seam-contract tests for openadapt-panel.

Two layers, mirroring the meta-package's test philosophy:

1. Behavior tests that need no sibling package — the app boots headless, auth
   gates the API, and the read-only / .env-backed routes work. These run
   everywhere the panel is installed.
2. Seam contracts against sibling signatures (openadapt-capture / -evals) —
   they assert the exact symbols and kwargs the panel routes use actually exist
   in the installed sibling, catching the #999 "phantom import / phantom kwarg"
   class. They skip when the sibling isn't installed; CI installs them so they
   run there. Long-running capture/train reuse the CLI subprocess seam, which
   tests/test_cli_smoke.py already covers.
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("openadapt_panel", reason="openadapt-panel not installed")
pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient  # noqa: E402
from openadapt_panel.app import create_app  # noqa: E402

TOKEN = "test-token"


def _client() -> TestClient:
    return TestClient(create_app(token=TOKEN, enable_auth=True))


def _auth() -> dict:
    return {"X-Panel-Token": TOKEN}


# --- behavior (no siblings needed) -----------------------------------------


def test_health_is_unauthenticated():
    r = _client().get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_requires_token():
    c = _client()
    assert c.get("/api/system").status_code == 401
    assert c.get("/api/system", headers=_auth()).status_code == 200


def test_system_endpoint_shape():
    r = _client().get("/api/system", headers=_auth())
    body = r.json()
    assert "python" in body and "packages" in body and "api_keys" in body


def test_index_served():
    r = _client().get("/")
    assert r.status_code == 200
    assert "OpenAdapt" in r.text


def test_train_status_and_stop(tmp_path):
    c = _client()
    out = tmp_path / "run"
    r = c.get(f"/api/train/status?output={out}", headers=_auth())
    assert r.json() == {"active": False}

    r = c.request("POST", "/api/train/stop", headers=_auth(), json={"output": str(out)})
    assert r.status_code == 200
    assert (out / "STOP_TRAINING").exists()


def test_models_empty(tmp_path):
    r = _client().get(f"/api/models?path={tmp_path}", headers=_auth())
    assert r.json()["models"] == []


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # settings writes ./.env
    c = _client()
    c.request(
        "PUT",
        "/api/settings",
        headers=_auth(),
        json={
            "values": {"OPENADAPT_SERVER_PORT": "9999", "ANTHROPIC_API_KEY": "sk-xyz"}
        },
    )
    assert (tmp_path / ".env").exists()
    body = c.get("/api/settings", headers=_auth()).json()
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["OPENADAPT_SERVER_PORT"]["value"] == "9999"
    # Secret value is never echoed back, only its set-state.
    assert fields["ANTHROPIC_API_KEY"]["value"] is None
    assert fields["ANTHROPIC_API_KEY"]["is_set"] is True


def test_blank_secret_does_not_wipe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = _client()
    c.request(
        "PUT",
        "/api/settings",
        headers=_auth(),
        json={"values": {"ANTHROPIC_API_KEY": "sk-keep"}},
    )
    # Re-save with a blank secret; the stored key must survive.
    c.request(
        "PUT",
        "/api/settings",
        headers=_auth(),
        json={"values": {"ANTHROPIC_API_KEY": ""}},
    )
    env = (tmp_path / ".env").read_text()
    assert "sk-keep" in env


def test_missing_sibling_returns_503(tmp_path):
    """When a sibling isn't installed, its route returns 503 (not 500/import
    crash). Only meaningful when the sibling is actually absent."""
    from importlib.util import find_spec

    if find_spec("openadapt_capture") is not None:
        pytest.skip("openadapt-capture is installed; 503 path not exercised")
    r = _client().get("/api/captures", headers=_auth())
    assert r.status_code == 503
    assert "openadapt-capture" in r.json()["detail"]


# --- seam contracts against sibling signatures -----------------------------


def test_eval_seam_symbols_and_kwargs():
    """The openadapt_evals symbols + kwargs the eval routes use must exist."""
    evals = pytest.importorskip(
        "openadapt_evals", reason="openadapt-evals not installed"
    )

    for name in [
        "SmartMockAgent",
        "ApiAgent",
        "PolicyAgent",
        "WAAMockAdapter",
        "WAALiveAdapter",
        "WAALiveConfig",
        "compute_metrics",
        "evaluate_agent_on_benchmark",
    ]:
        assert hasattr(evals, name), f"openadapt_evals is missing {name}"

    def accepts(callable_obj, kwarg):
        params = inspect.signature(callable_obj).parameters
        return kwarg in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    assert accepts(evals.evaluate_agent_on_benchmark, "max_steps")
    assert accepts(evals.WAAMockAdapter, "num_tasks")
    # Live adapter takes a config object; the server URL lives on the config.
    assert accepts(evals.WAALiveAdapter, "config")
    assert accepts(evals.WAALiveConfig, "server_url")
    assert accepts(evals.ApiAgent, "provider")
    assert accepts(evals.ApiAgent, "demo")
    assert accepts(evals.PolicyAgent, "checkpoint_path")


def test_eval_metrics_keys_the_ui_reads():
    """compute_metrics must return the keys the panel UI (and CLI) display.

    Runs a tiny real mock eval — the same seam /api/eval/mock uses — and checks
    the metric keys read downstream. Catches drift like `total_tasks` vs
    `num_tasks`.
    """
    evals = pytest.importorskip(
        "openadapt_evals", reason="openadapt-evals not installed"
    )
    results = evals.evaluate_agent_on_benchmark(
        evals.SmartMockAgent(), evals.WAAMockAdapter(num_tasks=2), max_steps=15
    )
    metrics = evals.compute_metrics(results)
    assert "success_rate" in metrics
    assert "avg_steps" in metrics
    # The UI shows a task count under one of these names.
    assert "num_tasks" in metrics or "total_tasks" in metrics


def test_capture_seam_symbols():
    """The openadapt_capture symbols the captures routes use must exist."""
    cap = pytest.importorskip(
        "openadapt_capture", reason="openadapt-capture not installed"
    )
    assert hasattr(cap, "Capture")
    assert hasattr(cap, "create_html")
    assert hasattr(cap.Capture, "load")


def test_settings_env_prefix_matches_config():
    """The panel's curated OPENADAPT_-prefixed settings keys must correspond to
    real fields on openadapt.config (guards against silent typos)."""
    pytest.importorskip("pydantic_settings", reason="pydantic-settings not installed")
    from openadapt_panel.routes.settings import SCHEMA

    from openadapt.config import OpenAdaptSettings

    fields = set(OpenAdaptSettings.model_fields)
    bare_keys = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "LAMBDA_API_KEY",
    }
    for entry in SCHEMA:
        key = entry["key"]
        if key in bare_keys:
            field = key.lower()
        else:
            assert key.startswith("OPENADAPT_"), key
            field = key[len("OPENADAPT_") :].lower()
        assert field in fields, (
            f"settings key {key} -> {field} not in OpenAdaptSettings"
        )
