"""Fail-safe tests for the runtime VLM clients.

The single invariant under test: when the appliance misbehaves in ANY way
(unreachable, timeout, auth error, 5xx, malformed body), every client returns
the SAFE outcome so the runtime halts -- never a wrong action.
"""

from __future__ import annotations

import httpx
import pytest

from openadapt_flow.runtime.grounder import Grounder, GrounderMatch
from openadapt_flow.runtime.remote_vlm import (
    IdentityVerdict,
    RemoteGrounder,
    RemoteIdentityVLM,
    RemoteStateVerifier,
    RemoteVLMClient,
)

PNG = b"\x89PNG\r\n\x1a\n"  # bytes need not be a real PNG for the client layer


# --- happy path (baseline: clients pass through good answers) ---------------


def test_identity_pass_through_verdicts(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"verdict": "same", "latency_ms": 1.0})

    monkeypatch.setattr(httpx, "post", lambda url, **kw: handler(None))
    idv = RemoteIdentityVLM(RemoteVLMClient("http://x", token="t"))
    assert idv.compare(PNG, PNG) is IdentityVerdict.VERIFY


def test_identity_different_is_mismatch(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(200, json={"verdict": "different"}),
    )
    idv = RemoteIdentityVLM(RemoteVLMClient("http://x", token="t"))
    assert idv.compare(PNG, PNG) is IdentityVerdict.MISMATCH
    assert idv.is_veto(PNG, PNG) is True


def test_grounder_pass_through_point(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200, json={"point": [120, 45], "confidence": 0.9}
        ),
    )
    g = RemoteGrounder(RemoteVLMClient("http://x", token="t"))
    m = g.locate(PNG, "Save button")
    assert isinstance(m, GrounderMatch)
    assert m.point == (120, 45)


def test_remote_grounder_satisfies_protocol():
    g = RemoteGrounder(RemoteVLMClient("http://x"))
    assert isinstance(g, Grounder)


# --- FAIL-SAFE: every failure mode -> SAFE outcome (halt) -------------------


def _raise_connect(url, **kw):
    raise httpx.ConnectError("appliance unreachable")


def _raise_timeout(url, **kw):
    raise httpx.ReadTimeout("timed out")


def _resp_401(url, **kw):
    return httpx.Response(401, json={"detail": "unauthorized"})


def _resp_500(url, **kw):
    return httpx.Response(500, text="boom")


def _resp_malformed(url, **kw):
    return httpx.Response(200, text="<html>not json</html>")


def _resp_missing_field(url, **kw):
    return httpx.Response(200, json={"unexpected": "shape"})


FAILURES = [
    ("unreachable", _raise_connect),
    ("timeout", _raise_timeout),
    ("auth_401", _resp_401),
    ("server_500", _resp_500),
    ("malformed", _resp_malformed),
    ("missing_field", _resp_missing_field),
]


@pytest.mark.parametrize("name,post", FAILURES)
def test_identity_fails_safe_to_abstain(name, post, monkeypatch):
    monkeypatch.setattr(httpx, "post", post)
    idv = RemoteIdentityVLM(RemoteVLMClient("http://x", token="t"))
    # SAFE direction: never VERIFY on failure -> the tier abstains -> halt.
    assert idv.compare(PNG, PNG) is IdentityVerdict.ABSTAIN, name
    assert idv.is_veto(PNG, PNG) is True, name


@pytest.mark.parametrize("name,post", FAILURES)
def test_grounder_fails_safe_to_none(name, post, monkeypatch):
    monkeypatch.setattr(httpx, "post", post)
    g = RemoteGrounder(RemoteVLMClient("http://x", token="t"))
    # SAFE direction: no proposal -> the resolution ladder halts.
    assert g.locate(PNG, "Save button") is None, name


@pytest.mark.parametrize("name,post", FAILURES)
def test_state_fails_safe_to_uncertain(name, post, monkeypatch):
    monkeypatch.setattr(httpx, "post", post)
    sv = RemoteStateVerifier(RemoteVLMClient("http://x", token="t"))
    # SAFE direction: postcondition unproven -> halt.
    assert sv.verify(PNG, "saved dialog visible") == "uncertain", name
    assert sv.holds(PNG, "saved dialog visible") is False, name


def test_identity_uncertain_verdict_is_abstain(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(200, json={"verdict": "uncertain"}),
    )
    idv = RemoteIdentityVLM(RemoteVLMClient("http://x", token="t"))
    assert idv.compare(PNG, PNG) is IdentityVerdict.ABSTAIN


def test_grounder_null_point_is_none(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(200, json={"point": None}),
    )
    g = RemoteGrounder(RemoteVLMClient("http://x", token="t"))
    assert g.locate(PNG, "Save") is None
