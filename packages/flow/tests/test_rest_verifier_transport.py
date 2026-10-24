"""The REST verifier must be able to read a system of record on a clean install.

``requests`` is a development dependency; ``httpx`` is a core one. Before the
fallback added alongside these tests, ``RestRecordVerifier._get_session`` raised
``ModuleNotFoundError`` on a wheel-only install. ``_fetch_records`` swallows
every transport error, so the verifier read the system of record as
*unreadable* and HALTed. That failed safe -- it never invented a confirmation --
but it made `effects.kind: rest` unusable out of the box, and it is exactly the
kind of composed-path break that per-module tests do not see.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from openadapt_flow.runtime.effects.rest import RestRecordVerifier


def _hide(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    real_import = builtins.__import__

    def fake_import(module: str, *args: Any, **kwargs: Any) -> Any:
        if module == name:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(module, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_session_falls_back_to_httpx_without_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    _hide(monkeypatch, "requests")
    session = RestRecordVerifier("http://127.0.0.1:1")._get_session()
    assert isinstance(session, httpx.Client)


def test_injected_session_is_always_preferred() -> None:
    sentinel = object()
    verifier = RestRecordVerifier("http://127.0.0.1:1", session=sentinel)
    assert verifier._get_session() is sentinel


def test_records_are_read_over_the_fallback_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is wired end to end, not merely constructible."""

    from openadapt_flow.mockmed.fault_server import serve

    _hide(monkeypatch, "requests")
    base_url, db, stop = serve()
    try:
        db.add("p1", "Triage", "note", key="k1")
        state = RestRecordVerifier(base_url).capture_pre_state()
        assert state.reachable is True
        assert [record["patient_id"] for record in state.records] == ["p1"]
    finally:
        stop()


def test_rest_verifier_follows_body_pagination() -> None:
    class FakeResponse:
        def __init__(self, data, url="http://127.0.0.1/api/db"):
            self.data = data
            self.status_code = 200
            self.url = url
            self.links = {}

        def json(self):
            return self.data

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout=None, headers=None):
            self.calls.append(url)
            if url == "http://127.0.0.1/api/db":
                return FakeResponse(
                    {"records": [{"id": 1, "val": "a"}], "next": "/api/db?page=2"},
                    url=url,
                )
            elif url == "http://127.0.0.1/api/db?page=2":
                return FakeResponse(
                    {"records": [{"id": 2, "val": "b"}]},
                    url=url,
                )
            raise AssertionError(f"unexpected url: {url}")

    session = FakeSession()
    verifier = RestRecordVerifier("http://127.0.0.1", session=session)
    state = verifier.capture_pre_state()
    assert state.reachable is True
    assert len(state.records) == 2
    assert state.records[0]["val"] == "a"
    assert state.records[1]["val"] == "b"


def test_rest_verifier_follows_link_header_pagination() -> None:
    class FakeResponse:
        def __init__(self, data, next_link=None, url="http://127.0.0.1/api/db"):
            self.data = data
            self.status_code = 200
            self.url = url
            self.links = {"next": {"url": next_link}} if next_link else {}

        def json(self):
            return self.data

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout=None, headers=None):
            self.calls.append(url)
            if url == "http://127.0.0.1/api/db":
                return FakeResponse(
                    [{"id": 1, "val": "first"}],
                    next_link="http://127.0.0.1/api/db?page=2",
                    url=url,
                )
            elif url == "http://127.0.0.1/api/db?page=2":
                return FakeResponse(
                    [{"id": 2, "val": "second"}],
                    url=url,
                )
            raise AssertionError(f"unexpected url: {url}")

    session = FakeSession()
    verifier = RestRecordVerifier("http://127.0.0.1", records_key=None, session=session)
    state = verifier.capture_pre_state()
    assert state.reachable is True
    assert len(state.records) == 2
    assert [r["val"] for r in state.records] == ["first", "second"]


def test_rest_verifier_total_mismatch_is_unreachable() -> None:
    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.url = "http://127.0.0.1/api/db"
            self.links = {}

        def json(self):
            return {"records": [{"id": 1}], "total": 99}

    class FakeSession:
        def get(self, url, timeout=None, headers=None):
            return FakeResponse()

    session = FakeSession()
    verifier = RestRecordVerifier("http://127.0.0.1", session=session)
    state = verifier.capture_pre_state()
    assert state.reachable is False
    assert state.records == []

