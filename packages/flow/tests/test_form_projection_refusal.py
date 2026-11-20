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
        {"hwnd": hwnd, "pid": pid, "value_contract": server.SERVER_VALUE_READ_CONTRACT},
        desktop,
        lambda: auto,
    )


def check_refusal(answer, channel, reason, *, ordinal=1, window=(7, 10)):
    assert set(answer) == {"status", "window", "fields", "refusal"}
    assert (
        answer["status"] == "ok" and answer["window"] is None and answer["fields"] == []
    )
    receipt = answer["refusal"]
    assert server.valid_form_projection_refusal_v2(receipt, window=window)
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
        {"hwnd": 7, "pid": 10, "value_contract": server.SERVER_VALUE_READ_CONTRACT},
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
        {"hwnd": 7, "pid": 10, "value_contract": server.SERVER_VALUE_READ_CONTRACT},
        desktop,
        lambda: pytest.fail("UIA read after owned-window refusal"),
    )
    receipt = check_refusal(answer, "window", "owned_window_present", ordinal=None)
    assert receipt["stage"] == "owned_windows"


def test_uia_setup_failure_is_closed_and_non_content():
    _, desktop, _ = context()
    answer = server._window_form_fields_operation(
        {"hwnd": 7, "pid": 10, "value_contract": server.SERVER_VALUE_READ_CONTRACT},
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
        property_id = (30022, 30041, 30036, 30043)[number % 4]
        category = {
            30022: ("visibility", "visibility_unknown"),
            30041: ("state", "state_capability_unknown"),
            30036: ("state", "state_capability_unknown"),
            30043: ("value_capability", "value_capability_unknown"),
        }[property_id]

        def query(prop, ignore):
            if prop != property_id:
                return original(prop, ignore)
            if number % 2:
                raise RuntimeError(MARKER)
            return None

        target.GetPropertyValueEx = query
        hwnd, pid = 10 + number, 100 + number
        count = number % 3
        _, desktop, auto = context(
            *[_Control("TitleBarControl") for _ in range(count)],
            target,
            hwnd=hwnd,
            pid=pid,
        )
        answer = operation(desktop, auto, hwnd=hwnd, pid=pid)
        receipt = check_refusal(
            answer,
            *category,
            ordinal=count + 1,
            window=(hwnd, pid),
        )
        assert receipt["detail"] == {
            "property": server._FORM_PROPERTY_NAMES[property_id],
            "phase": "query" if number % 2 else "result_kind",
            "outcome": "query_error" if number % 2 else "none",
        }
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
    assert not server.valid_form_projection_refusal_v2(receipt, window=(7, 10))
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
            assert server.valid_form_projection_refusal_v2(receipt, window=(7, 10))
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


class _OpaquePropertyError(Exception):
    def __repr__(self):
        pytest.fail("provider error repr is forbidden")

    def __str__(self):
        pytest.fail("provider error text is forbidden")


class _OpaquePropertyValue:
    def __repr__(self):
        pytest.fail("raw property repr is forbidden")

    def __str__(self):
        pytest.fail("raw property text is forbidden")


_PROPERTY_FAULTS = [
    ("guard_before_lookup", "guard_refused", 1, 0, 0),
    ("lookup", "lookup_error", 1, 1, 0),
    ("lookup", "lookup_unavailable", 1, 1, 0),
    ("guard_before_query", "guard_refused", 2, 1, 0),
    ("query", "query_error", 2, 1, 1),
    ("guard_after_query", "guard_refused", 3, 1, 1),
    ("result_kind", "none", 2, 1, 1),
    ("result_kind", "non_bool", 2, 1, 1),
]


@pytest.mark.parametrize("property_id,name", list(server._FORM_PROPERTY_NAMES.items()))
@pytest.mark.parametrize("phase,outcome,guards,lookups,queries", _PROPERTY_FAULTS)
def test_raw_boolean_failure_detail_uses_only_original_reads(
    monkeypatch, property_id, name, phase, outcome, guards, lookups, queries
):
    calls = {"guard": 0, "lookup": 0, "query": 0}

    def guard(_control):
        calls["guard"] += 1
        fail_at = {
            "guard_before_lookup": 1,
            "guard_before_query": 2,
            "guard_after_query": 3,
        }
        if phase in fail_at and calls["guard"] == fail_at[phase]:
            raise _OpaquePropertyError()

    class Target:
        @property
        def GetPropertyValueEx(self):
            calls["lookup"] += 1
            if outcome == "lookup_error":
                raise _OpaquePropertyError()
            if outcome == "lookup_unavailable":
                return None
            return self.query

        def query(self, prop, ignore):
            calls["query"] += 1
            assert (prop, ignore) == (property_id, 1)
            if outcome == "query_error":
                raise _OpaquePropertyError()
            if outcome == "none":
                return None
            if outcome == "non_bool":
                return _OpaquePropertyValue()
            return False

        def __getattr__(self, key):
            pytest.fail("no unrelated or content getter is permitted")

    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: SimpleNamespace(require_non_sensitive_value=guard),
    )
    observation = server._form_boolean_observation(Target(), property_id)
    assert observation.value is None
    assert observation.detail.payload() == {
        "property": name,
        "phase": phase,
        "outcome": outcome,
    }
    assert calls == {"guard": guards, "lookup": lookups, "query": queries}
    assert MARKER not in json.dumps(observation.detail.payload())


@pytest.mark.parametrize("property_id,name", list(server._FORM_PROPERTY_NAMES.items()))
@pytest.mark.parametrize("value", [False, True])
def test_supported_boolean_values_have_no_failure_detail(
    monkeypatch, property_id, name, value
):
    calls = []

    def guard(_):
        calls.append("guard")

    target = SimpleNamespace(
        GetPropertyValueEx=lambda prop, default: calls.append((prop, default)) or value
    )
    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: SimpleNamespace(require_non_sensitive_value=guard),
    )
    observed = server._form_boolean_observation(target, property_id)
    assert observed.value is value and observed.detail is None
    assert calls == ["guard", "guard", (property_id, 1), "guard"]


@pytest.mark.parametrize("failed_property", [30041, 30036])
def test_state_first_unknown_prevents_later_probe_or_pattern_reads(
    monkeypatch, failed_property
):
    calls = []

    def query(prop, ignore):
        calls.append(prop)
        return None if prop == failed_property else False

    target = SimpleNamespace(GetPropertyValueEx=query)
    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: SimpleNamespace(require_non_sensitive_value=lambda _: None),
    )
    observed = server._form_field_state(target)
    assert not observed.complete and observed.state is None
    assert observed.reason == "state_capability_unknown"
    assert observed.detail.payload() == {
        "property": server._FORM_PROPERTY_NAMES[failed_property],
        "phase": "result_kind",
        "outcome": "none",
    }
    assert calls == ([30041] if failed_property == 30041 else [30041, 30036])


def test_state_recheck_unknown_stops_before_the_next_property(monkeypatch):
    calls = []

    def query(prop, ignore):
        calls.append(prop)
        return None if calls == [30041, 30036, 30041] else False

    target = SimpleNamespace(GetPropertyValueEx=query)
    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: SimpleNamespace(require_non_sensitive_value=lambda _: None),
    )
    observed = server._form_field_state(target)
    assert not observed.complete and observed.reason == "state_capability_changed"
    assert observed.detail.payload() == {
        "property": "toggle",
        "phase": "result_kind",
        "outcome": "none",
    }
    assert calls == [30041, 30036, 30041]


@pytest.mark.parametrize(
    "property_id,channel,reason",
    [
        (30022, "visibility", "visibility_unknown"),
        (30041, "state", "state_capability_unknown"),
        (30036, "state", "state_capability_unknown"),
        (30043, "value_capability", "value_capability_unknown"),
    ],
)
def test_request_refusal_preserves_property_detail_with_ordinal_without_content(
    property_id, channel, reason
):
    target = _Control("TextControl", name=MARKER)
    original = target.GetPropertyValueEx
    target.GetPropertyValueEx = lambda prop, ignore: (
        None if prop == property_id else original(prop, ignore)
    )
    _, desktop, auto = context(target, cleanup_error=True)
    receipt = check_refusal(operation(desktop, auto), channel, reason)
    assert receipt["detail"] == {
        "property": server._FORM_PROPERTY_NAMES[property_id],
        "phase": "result_kind",
        "outcome": "none",
    }


@pytest.mark.parametrize(
    "fault",
    ["extra", "property", "phase", "pair", "raw_type", "wrong_channel", "wrong_reason"],
)
def test_detail_closed_shape_and_phase_pairs_cannot_cross_client(fault):
    detail = server._FormPropertyDetail("toggle", "result_kind", "none")
    receipt = server._FormProjectionRefusal(
        "observation", "state", "state_capability_unknown", 1, detail
    ).payload((7, 10))
    if fault == "extra":
        receipt["detail"]["value"] = MARKER
    elif fault == "property":
        receipt["detail"]["property"] = MARKER
    elif fault == "phase":
        receipt["detail"]["phase"] = MARKER
    elif fault == "pair":
        receipt["detail"]["phase"] = "query"
    elif fault == "raw_type":
        receipt["detail"]["outcome"] = 30041
    elif fault == "wrong_reason":
        receipt["reason"] = "composite_state_unrepresented"
    else:
        receipt["channel"] = "value_capability"
        receipt["reason"] = "value_capability_unknown"
    assert not server.valid_form_projection_refusal_v2(receipt, window=(7, 10))
    backend, requests = _windows_backend(
        {"type": (7, 10)},
        {"status": "ok", "window": None, "fields": [], "refusal": receipt},
    )
    before = dict(vars(backend))
    assert backend.approval_form_fields("save") is None
    assert len(requests) == 1 and vars(backend) == before


@pytest.mark.parametrize("operation_name", ["read", "form"])
def test_wire_v2_refuses_before_any_provider_or_helper_getter(
    monkeypatch, operation_name
):
    calls = []
    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: (
            calls.append("helper")
            or pytest.fail("old client must refuse before helper lookup")
        ),
    )

    class NoProvider:
        def __getattr__(self, key):
            calls.append(key)
            pytest.fail("old client must refuse before provider lookup")

    payload = {"value_contract": "openadapt.native-value-read/v2"}
    if operation_name == "read":
        payload["locator"] = {"automation_id": "field"}
    else:
        payload.update(hwnd=7, pid=10)
    with pytest.raises(
        server.AgentRequestError, match="incompatible native value contract"
    ):
        if operation_name == "read":
            server._perform_uia_initialized(NoProvider(), "read", payload)
        else:
            server._window_form_fields_operation(payload, NoProvider(), NoProvider())
    assert calls == []


@pytest.mark.parametrize("historical_head", ["7527440e", "a222b03a"])
def test_old_v2_client_import_requires_its_original_refusal_parser(historical_head):
    assert "valid_form_projection_refusal" not in vars(server)
    source = "from openadapt_flow.backends.win_agent.server import valid_form_projection_refusal\nraise AssertionError('old client body must not run')\n"
    with pytest.raises(ImportError, match="valid_form_projection_refusal"):
        exec(
            compile(
                source, "historical-" + historical_head + "-validator-import", "exec"
            ),
            {},
        )


def test_new_client_refuses_old_local_validator_export_before_its_body(monkeypatch):
    import sys
    from pathlib import Path
    from types import ModuleType

    import openadapt_flow.backends.windows_backend as client

    legacy = ModuleType("openadapt_flow.backends.win_agent.server")
    legacy.valid_form_projection_refusal = lambda *_args, **_kwargs: pytest.fail(
        "legacy validator must never run"
    )
    monkeypatch.setitem(sys.modules, legacy.__name__, legacy)
    source = Path(client.__file__).read_bytes()
    with pytest.raises(ImportError, match="valid_form_projection_refusal_v2"):
        exec(
            compile(source, str(client.__file__), "exec"),
            {"__name__": "mixed_client_source"},
        )
