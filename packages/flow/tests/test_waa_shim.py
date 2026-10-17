"""Contract tests for the in-guest benchmark shim (``scripts/desktop/waa_shim.py``).

The shim used to ``exec()`` client Python on ``/execute_windows``. These tests
pin the replacement: that route is gone (410), the two benchmark tasks take
bounded data only, the token guards every route but ``/health``, the default
bind is loopback, and errors never carry a traceback.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")

_SHIM_PATH = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "waa_shim.py"


@pytest.fixture()
def shim(monkeypatch):
    spec = importlib.util.spec_from_file_location("waa_shim_under_test", _SHIM_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config["SHIM_TOKEN"] = None
    module.app.testing = True
    return module


def _auth(token: str = "s3cret") -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_execute_windows_is_gone_and_never_runs_code(shim, tmp_path) -> None:
    marker = tmp_path / "pwned"
    client = shim.app.test_client()
    r = client.post(
        "/execute_windows",
        json={"command": f"open({str(marker)!r}, 'w').write('x')"},
    )
    assert r.status_code == 410
    assert "trace" not in r.get_json()
    assert not marker.exists()


def test_token_guards_every_route_but_health(shim) -> None:
    shim.app.config["SHIM_TOKEN"] = "s3cret"
    client = shim.app.test_client()
    assert client.get("/health").status_code == 200
    for method, path in [
        ("get", "/screenshot"),
        ("get", "/uia"),
        ("post", "/tasks/quiet-desktop"),
        ("post", "/tasks/uia-arm"),
        ("post", "/execute_windows"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
        wrong = getattr(client, method)(path, headers=_auth("nope"))
        assert wrong.status_code == 401, path


def test_quiet_desktop_runs_only_the_fixed_registry_writes(shim, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(shim.subprocess, "run", fake_run)
    r = shim.app.test_client().post("/tasks/quiet-desktop", json={"command": "x"})
    assert r.status_code == 200
    assert len(calls) == 2
    assert all(c[:2] == ["reg", "add"] and c[-1] == "/f" for c in calls)
    assert {c[4] for c in calls} == {
        "ToastEnabled",
        "NOC_GLOBAL_SETTING_TOASTS_ENABLED",
    }


def _arm_body(**overrides) -> dict:
    body = {
        "search": "Smith",
        "first": "Ann",
        "last": "Smith",
        "note": "n",
        "mode": "identity",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "body",
    [
        {},
        _arm_body(mode="__import__('os')"),
        _arm_body(note=["not", "a", "string"]),
        _arm_body(note="x" * 10_000),
        {**_arm_body(), "path": "C:/evil.py"},
        "not an object",
    ],
)
def test_uia_arm_rejects_anything_but_five_bounded_strings(shim, body) -> None:
    r = shim.app.test_client().post("/tasks/uia-arm", json=body)
    assert r.status_code == 400


def test_uia_arm_runs_the_module_next_to_the_shim(shim, tmp_path, monkeypatch) -> None:
    (tmp_path / "uia_arm.py").write_text(
        "def run(search, first, last, note, mode):\n"
        "    return {'status': 'ok', 'args': [search, first, last, note, mode]}\n"
    )
    monkeypatch.setattr(shim, "_SHIM_DIR", tmp_path)
    r = shim.app.test_client().post("/tasks/uia-arm", json=_arm_body())
    assert r.status_code == 200
    assert r.get_json() == {
        "status": "ok",
        "args": ["Smith", "Ann", "Smith", "n", "identity"],
    }


def test_errors_never_carry_a_traceback(shim, tmp_path, monkeypatch) -> None:
    def boom():
        raise RuntimeError("secret detail")

    monkeypatch.setattr(shim, "_grab_png", boom)
    r = shim.app.test_client().get("/screenshot")
    assert r.status_code == 500
    assert r.get_json() == {"status": "error", "error": "screenshot failed"}

    (tmp_path / "uia_arm.py").write_text(
        "def run(*a):\n    raise RuntimeError('secret detail')\n"
    )
    monkeypatch.setattr(shim, "_SHIM_DIR", tmp_path)
    r = shim.app.test_client().post("/tasks/uia-arm", json=_arm_body())
    assert r.status_code == 500
    assert "secret detail" not in r.get_data(as_text=True)


def test_default_bind_is_loopback_and_token_comes_from_env(shim, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(shim.app, "run", lambda **kw: seen.update(kw))
    monkeypatch.setenv("OAFLOW_AGENT_TOKEN", "from-env")
    monkeypatch.setattr(sys, "argv", ["waa_shim.py"])
    shim.main()
    assert seen["host"] == "127.0.0.1"
    assert shim.app.config["SHIM_TOKEN"] == "from-env"
