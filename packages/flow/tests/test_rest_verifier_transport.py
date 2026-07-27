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
