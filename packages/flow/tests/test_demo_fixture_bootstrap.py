"""Closed transport/envelope refusal controls; no network, Docker or model."""

from __future__ import annotations

import asyncio
import json
import urllib.error
from collections.abc import Callable
from email.message import Message
from types import SimpleNamespace
from typing import cast

import pytest

from openadapt_flow.integrations.agents import demo_fixture as fixture

CANARY = "synthetic-private-body-auth-canary"


def _inputs(tmp_path):
    files = fixture.FixtureFiles(
        tmp_path,
        "oawrapdemo-unit",
        tmp_path / "compose.json",
        tmp_path / "db.sqlite",
        "http://127.0.0.1:19318",
    )
    spec = {
        "bootstrap": {
            "path": "/jsonrpc.php",
            "method": "createResource",
            "parameters": {"text": CANARY},
        }
    }
    return files, spec


def _install(monkeypatch, raw, *, failure=None, where="open"):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            calls.append(("read", limit))
            if where == "read" and failure is not None:
                raise failure
            return raw

    def open_response(request, *, timeout):
        calls.append(("open", timeout))
        assert request.full_url == "http://127.0.0.1:19318/jsonrpc.php"
        assert request.get_method() == "POST"
        assert json.loads(request.data) == {
            "jsonrpc": "2.0",
            "method": "createResource",
            "params": {"text": CANARY},
            "id": 1,
        }
        assert request.get_header("Authorization").startswith("Basic ")
        if where == "open" and failure is not None:
            raise failure
        return Response()

    def build(*handlers):
        assert len(handlers) == 2
        assert isinstance(handlers[0], fixture.urllib.request.ProxyHandler)
        assert vars(handlers[0])["proxies"] == {}
        assert isinstance(handlers[1], fixture._NoRedirect)
        return SimpleNamespace(open=open_response)

    monkeypatch.setattr(fixture.urllib.request, "build_opener", build)
    return calls


_RESPONSE_CASES = [
    (b'{"id":1,"result":17,"error":null}', None),
    (b"x" * 65537, "RESPONSE_TOO_LARGE"),
    (b"not-json-private-canary", "INVALID_JSON"),
    (b"[]", "ENVELOPE_NOT_OBJECT"),
    (b'{"result":17}', "ID_MISMATCH"),
    (b'{"id":true,"result":17}', "ID_MISMATCH"),
    (b'{"id":"1","result":17}', "ID_MISMATCH"),
    (b'{"id":2,"result":17}', "ID_MISMATCH"),
    (json.dumps({"id": 1, "error": {"message": CANARY}}).encode(), "RPC_ERROR"),
    (b'{"id":1,"result":false}', "BOOLEAN_RESULT"),
    (b'{"id":1,"result":true}', "BOOLEAN_RESULT"),
    (b'{"id":1,"result":0}', "NONPOSITIVE_ID"),
    (b'{"id":1,"result":-1}', "NONPOSITIVE_ID"),
    (b'{"id":1,"result":"17"}', "RESULT_NOT_INTEGER"),
    (b'{"id":1,"result":null}', "RESULT_NOT_INTEGER"),
    (b'{"id":1}', "RESULT_NOT_INTEGER"),
]


@pytest.mark.parametrize(
    "raw,reason",
    _RESPONSE_CASES,
    ids=[
        "positive",
        "oversize",
        "invalid-json",
        "array",
        "missing-id",
        "bool-id",
        "string-id",
        "wrong-id",
        "rpc-error",
        "false",
        "true",
        "zero",
        "negative",
        "string-result",
        "null-result",
        "missing-result",
    ],
)
def test_actual_bootstrap_classifies_closed_response(
    tmp_path, monkeypatch, capsys, raw, reason
):
    files, spec = _inputs(tmp_path)
    calls = _install(monkeypatch, raw)
    if reason is None:
        assert (
            cast(Callable[..., object], fixture.bootstrap)(
                files, spec, auth=("synthetic", CANARY)
            )
            is None
        )
    else:
        with pytest.raises(fixture.DemoBootstrapError) as caught:
            fixture.bootstrap(files, spec, auth=("synthetic", CANARY))
        assert caught.value.reason == reason
        assert str(caught.value) == "DEMO_BOOTSTRAP_UNAVAILABLE"
        assert CANARY not in repr(caught.value)
    assert calls == [("open", 5), ("read", 65537)]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "kind,where,reason",
    [
        ("http", "open", "HTTP_REFUSED"),
        ("url", "open", "URL_UNAVAILABLE"),
        ("timeout", "read", "TIMEOUT"),
        ("os", "read", "IO_UNAVAILABLE"),
        ("runtime", "open", "OTHER"),
    ],
)
def test_transport_failure_exposes_only_closed_metadata(
    tmp_path, monkeypatch, capsys, kind, where, reason
):
    errors = {
        "http": urllib.error.HTTPError(CANARY, 403, CANARY, Message(), None),
        "url": urllib.error.URLError(CANARY),
        "timeout": TimeoutError(CANARY),
        "os": OSError(CANARY),
        "runtime": RuntimeError(CANARY),
    }
    files, spec = _inputs(tmp_path)
    _install(monkeypatch, b"", failure=errors[kind], where=where)
    with pytest.raises(fixture.DemoBootstrapError) as caught:
        fixture.bootstrap(files, spec, auth=("synthetic", CANARY))
    assert caught.value.reason == reason
    assert str(caught.value) == "DEMO_BOOTSTRAP_UNAVAILABLE"
    assert caught.value.__suppress_context__ is True
    assert CANARY not in repr(caught.value)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("kind", ["cancel", "interrupt"])
def test_primary_cancellation_is_not_an_envelope_failure(tmp_path, monkeypatch, kind):
    error = (
        asyncio.CancelledError(CANARY)
        if kind == "cancel"
        else KeyboardInterrupt(CANARY)
    )
    files, spec = _inputs(tmp_path)
    _install(monkeypatch, b"", failure=error)
    with pytest.raises(type(error)) as caught:
        fixture.bootstrap(files, spec, auth=("synthetic", CANARY))
    assert caught.value is error


@pytest.mark.parametrize("mutation", ["query", "foreign", "unknown-action"])
def test_invalid_scope_refuses_before_opener(tmp_path, monkeypatch, mutation):
    files, spec = _inputs(tmp_path)
    if mutation == "query":
        spec["bootstrap"]["path"] += "?" + CANARY
    elif mutation == "foreign":
        files = fixture.FixtureFiles(
            files.root,
            files.project,
            files.compose,
            files.database,
            "http://example.invalid",
        )
    else:
        spec["bootstrap"][CANARY] = CANARY

    def forbidden(*_args):
        raise AssertionError("OPENER_MUST_NOT_RUN")

    monkeypatch.setattr(fixture.urllib.request, "build_opener", forbidden)
    with pytest.raises(ValueError) as caught:
        fixture.bootstrap(files, spec, auth=("synthetic", CANARY))
    assert CANARY not in str(caught.value)


def test_failure_reason_rejects_unknown_metadata():
    with pytest.raises(ValueError, match="^INVALID_DEMO_BOOTSTRAP_REASON$"):
        fixture.DemoBootstrapError(CANARY)
