"""Actual registered callback models; no Playwright/native/capture qualification."""

from __future__ import annotations

import itertools
import json
from asyncio import CancelledError
from types import SimpleNamespace

import pytest

from openadapt_flow.compile_agent.network_metadata import NetworkMetadata


class Page:
    def __init__(self):
        self.callbacks = {}
        self.detached = []

    def on(self, name, callback):
        self.callbacks[name] = callback

    def remove_listener(self, name, callback):
        assert self.callbacks[name] is callback
        self.detached.append(name)
        del self.callbacks[name]


class Request:
    method = "POST"
    url = "http://localhost:8080/tasks?token=SECRET"

    def __getattr__(self, name):
        raise AssertionError("FORBIDDEN_PAYLOAD_ACCESS")


def test_actual_callbacks_metadata_only_detached_and_deep_copy():
    page = Page()
    with NetworkMetadata(page, target_url="http://localhost:8080/tasks") as capture:
        page.callbacks["request"](Request())
        page.callbacks["response"](SimpleNamespace(request=Request(), status=201))
        page.callbacks["requestfailed"](Request())
    summary = capture.summary()
    assert len(summary["events"]) == 3
    assert summary["events"][1] == {
        "phase": "response",
        "method": "POST",
        "status": 201,
        "origin": "http://localhost:8080",
        "path": "/tasks",
    }
    assert summary["records_complete"] is None
    assert summary["api_binding"] == summary["causal_attribution"] == "UNAVAILABLE"
    assert "SECRET" not in json.dumps(summary)
    assert not page.callbacks
    summary["events"][0]["method"] = "CHANGED"
    assert capture.summary()["events"][0]["method"] == "POST"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/task/SECRET",
        "http://SECRET@localhost:8080/tasks",
        "https://SECRET.invalid/tasks",
        "invalid:SECRET",
        "http://localhost:wrong/tasks",
    ],
)
def test_unknown_or_credential_endpoint_not_retained(url):
    page = Page()
    with NetworkMetadata(page, target_url="http://localhost:8080/tasks") as capture:
        page.callbacks["request"](SimpleNamespace(method="POST", url=url))
    item = capture.summary()["events"][0]
    assert item["origin"] is item["path"] is None
    assert "SECRET" not in json.dumps(capture.summary())


def test_finite_prefixes_never_upgrade_authority_or_exceed_bound():
    for sequence in itertools.product(
        ("request", "response", "requestfailed"), repeat=4
    ):
        page = Page()
        with NetworkMetadata(
            page, target_url="http://localhost:8080/tasks", limit=2
        ) as capture:
            for event in sequence:
                page.callbacks[event](
                    SimpleNamespace(request=Request(), status=206)
                    if event == "response"
                    else Request()
                )
        result = capture.summary()
        assert len(result["events"]) == 2
        assert result["losses"] == ["EVENT_LIMIT"]
        assert result["records_complete"] is None
        assert result["api_binding"] == "UNAVAILABLE"


def test_partial_attach_detaches_every_attempt_without_exporting_error():
    page = Page()
    original = page.on

    def attach(name, callback):
        original(name, callback)
        if name == "response":
            raise RuntimeError("SECRET")

    page.on = attach
    with NetworkMetadata(page, target_url=None) as capture:
        pass
    assert not page.callbacks
    assert capture.summary()["losses"] == ["ATTACH_UNAVAILABLE"]
    assert "SECRET" not in json.dumps(capture.summary())


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt("ORIGINAL"), CancelledError("ORIGINAL")]
)
@pytest.mark.parametrize("phase", ["request", "response", "requestfailed"])
def test_registration_cancellation_after_attach_detaches_and_preserves_original(
    failure, phase
):
    page = Page()
    original = page.on

    def attach(name, callback):
        original(name, callback)
        if name == phase:
            raise failure

    page.on = attach
    capture = NetworkMetadata(page, target_url=None)
    with pytest.raises(type(failure)) as raised:
        capture.__enter__()
    assert raised.value is failure
    assert not page.callbacks
    assert capture.closed
    assert capture.summary()["losses"] == ["ATTACH_UNAVAILABLE"]


@pytest.mark.parametrize(
    "body_error", [ValueError("ORIGINAL"), KeyboardInterrupt("ORIGINAL")]
)
def test_detach_cancellation_preserves_original_action(body_error):
    page = Page()

    def fail(name, callback):
        del page.callbacks[name]
        raise KeyboardInterrupt("SECRET")

    page.remove_listener = fail
    with pytest.raises(type(body_error)) as raised:
        with NetworkMetadata(page, target_url=None) as capture:
            raise body_error
    assert raised.value is body_error
    assert not page.callbacks
    assert capture.summary()["losses"] == ["DETACH_UNAVAILABLE"]


def test_sole_detach_cancellation_propagates_and_late_event_ignored():
    page = Page()
    capture = NetworkMetadata(page, target_url=None)
    capture.__enter__()
    callback = page.callbacks["request"]

    def fail(name, listener):
        del page.callbacks[name]
        raise KeyboardInterrupt

    page.remove_listener = fail
    with pytest.raises(KeyboardInterrupt):
        capture.__exit__(None, None, None)
    callback(Request())
    assert capture.summary()["events"] == []


@pytest.mark.parametrize("cancel", [False, True])
def test_real_harnessed_agent_uses_same_page_once_and_keeps_original(
    monkeypatch, tmp_path, cancel
):
    from openadapt_flow.compile_agent import pipeline

    page = Page()
    computer = SimpleNamespace(page=page)
    ledger = object()
    failure = KeyboardInterrupt("ORIGINAL")
    calls = []

    class Agent:
        dialect = "test"

        def drive(self, request, harness):
            calls.append((request, harness.computer))
            page.callbacks["request"](Request())
            if cancel:
                raise failure
            return ledger

    # Only the browser-dependent harness constructor is modeled. The actual
    # compiler adapter and registered metadata callbacks execute unchanged.
    monkeypatch.setattr(
        pipeline,
        "Harness",
        lambda computer, *args, **kwargs: SimpleNamespace(computer=computer),
    )
    wrapper = pipeline._HarnessedAgent(
        Agent(),
        gate=None,
        keep=[],
        evidence_dir=tmp_path,
        principal_id=None,
        metadata_url="http://localhost:8080/tasks",
    )
    request = SimpleNamespace()
    if cancel:
        with pytest.raises(KeyboardInterrupt) as raised:
            wrapper.run(request, computer)
        assert raised.value is failure
    else:
        wrapper.run(request, computer)
        assert wrapper.ledger is ledger
    assert calls == [(request, computer)]
    assert wrapper.network_metadata["events"][0]["method"] == "POST"
    assert not page.callbacks
