"""WebApp.setup: unmeasured setup on the fresh page, before capture or run."""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.backends import playwright_backend
from openadapt_flow.jit import WebApp


class _Backend:
    def __init__(self, url: str) -> None:
        self.page = f"page-at:{url}"


def _fake_launch(calls: list[Any]):
    def launch(url: str, headless: bool = True, **kw: Any):
        calls.append(("launch", url, headless, kw.get("record_video_dir")))
        return _Backend(url), lambda: calls.append(("close",))

    return launch


def test_setup_runs_on_the_new_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend, "launch", _fake_launch(calls)
    )
    app = WebApp(
        url="http://app.test/form",
        setup=lambda page: calls.append(("setup", page)),
        record_video_dir="videos",
    )
    backend, close = app.open()
    assert calls == [
        ("launch", "http://app.test/form", True, "videos"),
        ("setup", "page-at:http://app.test/form"),
    ]
    close()
    assert calls[-1] == ("close",)


def test_a_failed_setup_closes_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend, "launch", _fake_launch(calls)
    )

    def setup(page: Any) -> None:
        raise RuntimeError("sign-in failed")

    with pytest.raises(RuntimeError, match="sign-in failed"):
        WebApp(url="http://app.test/form", setup=setup).open()
    assert calls[-1] == ("close",)


def test_no_setup_just_opens_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend, "launch", _fake_launch(calls)
    )
    WebApp(url="http://app.test/").open()
    assert calls == [("launch", "http://app.test/", True, None)]


class _NoModelAgent:
    dialect = "generic"
    requires_model_key = False

    def run(self, request: Any, computer: Any) -> None:  # pragma: no cover
        raise AssertionError("not reached")


@pytest.mark.parametrize("timeout", [None, 10.0])
def test_capture_settle_reaches_the_authoring_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, timeout: Any
) -> None:
    from openadapt_flow import authoring
    from openadapt_flow.jit import TaskSignature, compile_agent

    calls: list[Any] = []
    monkeypatch.setattr(
        playwright_backend.PlaywrightBackend, "launch", _fake_launch(calls)
    )
    seen: dict[str, Any] = {}

    class _Session:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)
            raise RuntimeError("stop after construction")

    monkeypatch.setattr(authoring, "AuthoringSession", _Session)
    jit = compile_agent(
        _NoModelAgent(),
        task=TaskSignature("t.x", "do {x}", {"x": str}),
        app=WebApp(url="http://app.test/", capture_settle_timeout_s=timeout),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="stop after construction"):
        jit.recording(x="1").__enter__()
    if timeout is None:
        assert "settle_timeout_s" not in seen
    else:
        assert seen["settle_timeout_s"] == timeout
    assert calls[-1] == ("close",)
