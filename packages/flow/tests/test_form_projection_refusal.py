"""A refused projection reports only closed request-local non-content facts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from openadapt_flow.backends.win_agent import server
from tests.test_approval_form_fields import (
    _Control,
    _Desktop,
    _Init,
    _window,
    _windows_backend,
)

MARKER = "fixture-content-must-not-be-returned"


def context(*children, hwnd=7, pid=10, cleanup_error=False):
    window = _window(*children)
    window.NativeWindowHandle, window.ProcessId = hwnd, pid
    window.Name = MARKER
    desktop = _Desktop()
    desktop.windows = {hwnd: (pid, (0, 0, 1024, 720), True, False)}

    class Initializer(_Init):
        def __exit__(self, *args):
            if cleanup_error:
                raise RuntimeError(MARKER)
            return None

    auto = SimpleNamespace(
        ControlFromHandle=lambda h: window if h == hwnd else None,
        UIAutomationInitializerInThread=Initializer,
    )
    return window, desktop, auto


def operation(desktop, auto, hwnd=7, pid=10):
    return server._window_form_fields_operation(
        {"hwnd": hwnd, "pid": pid, "value_contract": server.VALUE_READ_CONTRACT},
        desktop,
        lambda: auto,
    )


def check_refusal(answer, channel, reason, *, ordinal=1, window=(7, 10)):
    assert set(answer) == {"status", "window", "fields", "refusal"}
    assert (
        answer["status"] == "ok" and answer["window"] is None and answer["fields"] == []
    )
    receipt = answer["refusal"]
    assert server.valid_form_projection_refusal(receipt, window=window)
    assert receipt["channel"] == channel and receipt["reason"] == reason
    assert receipt["ordinal"] == ordinal
    assert receipt["window"] == (
        {"hwnd": window[0], "pid": window[1]} if window else None
    )
    assert MARKER not in json.dumps(answer)
    return receipt


@pytest.mark.parametrize(
    "fault,channel,reason",
    [
        ("visibility", "visibility", "visibility_unknown"),
        ("geometry", "geometry", "geometry_unavailable"),
        ("control_type", "control_type", "control_type_unavailable"),
        ("children", "children", "children_unavailable"),
        ("state", "state", "state_capability_unknown"),
        ("value_capability", "value_capability", "value_capability_unknown"),
        ("required_value", "value", "required_value_unavailable"),
    ],
)
def test_closed_refusal_discards_partial_fields_and_provider_content(
    fault, channel, reason
):
    target = _Control(
        "EditControl" if fault == "required_value" else "TextControl", name=MARKER
    )
    original = target.GetPropertyValueEx

    def query(property_id, ignore_default):
        if (fault, property_id) in {
            ("visibility", 30022),
            ("state", 30036),
            ("value_capability", 30043),
        }:
            return None
        return original(property_id, ignore_default)

    target.GetPropertyValueEx = query
    if fault == "geometry":
        target.BoundingRectangle = None
    elif fault == "control_type":
        target.ControlTypeName = None
    elif fault == "children":
        target.GetChildren = lambda: (_ for _ in ()).throw(RuntimeError(MARKER))
    _, desktop, auto = context(_Control("TitleBarControl"), target)
    check_refusal(operation(desktop, auto), channel, reason, ordinal=2)


def test_no_source_getter_or_final_census_runs_after_refusal(monkeypatch):
    forbidden = []
    stopped = False

    class Target(_Control):
        def __getattribute__(self, key):
            if stopped and not key.startswith("_"):
                forbidden.append(key)
                raise AssertionError("source getter after refusal")
            return super().__getattribute__(key)

    target = Target("EditControl", value=MARKER)
    original_property = target.GetPropertyValueEx
    target.GetPropertyValueEx = lambda prop, ignore: (
        None if prop == 30022 else original_property(prop, ignore)
    )
    _, desktop, auto = context(target)
    original_refuse = server._form_refused

    def refuse(*args, **kwargs):
        nonlocal stopped
        result = original_refuse(*args, **kwargs)
        stopped = True
        return result

    monkeypatch.setattr(server, "_form_refused", refuse)
    original_window = server._window_operation
    frames = []

    def frame(*args, **kwargs):
        frames.append(True)
        if stopped:
            forbidden.append("window recheck")
            raise AssertionError("final frame getter after refusal")
        return original_window(*args, **kwargs)

    monkeypatch.setattr(server, "_window_operation", frame)
    check_refusal(operation(desktop, auto), "visibility", "visibility_unknown")
    assert forbidden == [] and len(frames) == 1


def test_cleanup_exception_does_not_override_first_refusal_or_leak_text():
    target = _Control("TextControl")
    original = target.GetPropertyValueEx
    target.GetPropertyValueEx = lambda prop, ignore: (
        None if prop == 30022 else original(prop, ignore)
    )
    _, desktop, auto = context(target, cleanup_error=True)
    check_refusal(operation(desktop, auto), "visibility", "visibility_unknown")


def test_unknown_observer_reason_is_not_stringified_or_copied(monkeypatch):
    class Opaque:
        def __repr__(self):
            pytest.fail("provider repr must never be used")

        def __str__(self):
            pytest.fail("provider text must never be used")

    monkeypatch.setattr(
        server,
        "_form_field_state",
        lambda _: server._FormStateObservation(False, None, Opaque()),
    )
    _, desktop, auto = context(_Control("TextControl", name=MARKER))
    check_refusal(operation(desktop, auto), "state", "projection_unavailable")


def test_frame_unavailable_has_no_unverified_window_echo_or_uia_reads():
    _, desktop, _ = context()
    desktop.windows = {}
    answer = server._window_form_fields_operation(
        {"hwnd": 7, "pid": 10, "value_contract": server.VALUE_READ_CONTRACT},
        desktop,
        lambda: pytest.fail("UIA read after frame unavailable"),
    )
    receipt = check_refusal(
        answer, "window", "window_unavailable", ordinal=None, window=None
    )
    assert receipt["stage"] == "frame"


def test_owned_window_refusal_does_not_load_uia():
    _, desktop, _ = context()
    desktop.windows[8] = (10, (20, 20, 200, 100), True, False)
    answer = server._window_form_fields_operation(
        {"hwnd": 7, "pid": 10, "value_contract": server.VALUE_READ_CONTRACT},
        desktop,
        lambda: pytest.fail("UIA read after owned-window refusal"),
    )
    receipt = check_refusal(answer, "window", "owned_window_present", ordinal=None)
    assert receipt["stage"] == "owned_windows"


def test_uia_setup_failure_is_closed_and_non_content():
    _, desktop, _ = context()
    answer = server._window_form_fields_operation(
        {"hwnd": 7, "pid": 10, "value_contract": server.VALUE_READ_CONTRACT},
        desktop,
        lambda: (_ for _ in ()).throw(RuntimeError(MARKER)),
    )
    check_refusal(answer, "uia", "uia_unavailable", ordinal=None)


def test_final_scope_failure_reports_original_verified_window_without_extra_recheck():
    _, desktop, auto = context()
    calls = []
    original = desktop.rect

    def moved(hwnd):
        calls.append(hwnd)
        assert len(calls) <= 2
        return original(hwnd) if len(calls) == 1 else (1, 0, 1025, 720)

    desktop.rect = moved
    receipt = check_refusal(
        operation(desktop, auto), "window", "final_window_unavailable", ordinal=None
    )
    assert receipt["stage"] == "final_scope" and len(calls) == 2


@pytest.mark.parametrize("limit", ["nodes", "depth", "fields"])
def test_limit_receipt_discards_partial_fields_without_a_recovery_walk(
    monkeypatch, limit
):
    if limit == "nodes":
        monkeypatch.setattr(server, "_MAX_UIA_NODES", 0)
        children = [_Control("TextControl")]
        reason, ordinal = "node_limit", 1
    elif limit == "depth":
        monkeypatch.setattr(server, "_MAX_UIA_DEPTH", 0)
        children = [_Control("PaneControl", children=[_Control("TextControl")])]
        reason, ordinal = "depth_limit", 1
    else:
        monkeypatch.setattr(server, "_MAX_FORM_FIELDS", 0)
        children = [_Control("TitleBarControl"), _Control("TextControl")]
        reason, ordinal = "field_limit", 2
    _, desktop, auto = context(*children)
    check_refusal(operation(desktop, auto), "limits", reason, ordinal=ordinal)


def test_interleaved_requests_cannot_share_or_overwrite_refusal_identity():
    def request(number):
        target = _Control("TextControl")
        original = target.GetPropertyValueEx
        target.GetPropertyValueEx = lambda prop, ignore: (
            None if prop == 30022 else original(prop, ignore)
        )
        hwnd, pid = 10 + number, 100 + number
        count = number % 3
        _, desktop, auto = context(
            *[_Control("TitleBarControl") for _ in range(count)],
            target,
            hwnd=hwnd,
            pid=pid,
        )
        answer = operation(desktop, auto, hwnd=hwnd, pid=pid)
        check_refusal(
            answer,
            "visibility",
            "visibility_unknown",
            ordinal=count + 1,
            window=(hwnd, pid),
        )
        return answer

    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(request, range(12)))
    assert len({answer["refusal"]["window"]["hwnd"] for answer in answers}) == 12
    _, desktop, auto = context()
    successful = operation(desktop, auto)
    assert successful["refusal"] is None and successful["window"] is not None


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "free_reason",
        "wrong_channel",
        "bool_ordinal",
        "wrong_window",
        "extra_window_key",
        "nonstring_schema",
    ],
)
def test_client_rejects_malformed_refusal_without_retry_or_error_cache(fault):
    receipt = server._FormProjectionRefusal(
        "observation", "visibility", "visibility_unknown", 1
    ).payload((7, 10))
    if fault == "extra":
        receipt["value"] = MARKER
    elif fault == "free_reason":
        receipt["reason"] = MARKER
    elif fault == "wrong_channel":
        receipt["channel"] = "geometry"
    elif fault == "bool_ordinal":
        receipt["ordinal"] = True
    elif fault == "wrong_window":
        receipt["window"]["pid"] = 99
    elif fault == "extra_window_key":
        receipt["window"]["name"] = MARKER
    else:
        receipt["schema"] = 1
    assert not server.valid_form_projection_refusal(receipt, window=(7, 10))
    wire = {"status": "ok", "window": None, "fields": [], "refusal": receipt}
    backend, requests = _windows_backend({"type": (7, 10)}, wire)
    before = dict(vars(backend))
    assert backend.approval_form_fields("save") is None
    assert len(requests) == 1 and vars(backend) == before


def test_every_closed_reason_is_diagnostic_only_and_roundtrips():
    for (stage, channel), reasons in server._FORM_REFUSAL_MODEL.items():
        for reason in reasons:
            receipt = server._FormProjectionRefusal(stage, channel, reason, 1).payload(
                (7, 10)
            )
            assert server.valid_form_projection_refusal(receipt, window=(7, 10))
            backend, requests = _windows_backend(
                {"type": (7, 10)},
                {
                    "status": "ok",
                    "window": None,
                    "fields": [],
                    "refusal": deepcopy(receipt),
                },
            )
            assert backend.approval_form_fields("save") is None and len(requests) == 1
