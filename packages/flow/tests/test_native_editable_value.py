"""One exact ValuePattern contract for focused and point-bound text readers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.backends.win_agent import native_value
from openadapt_flow.backends.win_agent.native_value import (
    NativeOwner,
    NativeRead,
    utf16_bytes,
)
from openadapt_flow.backends.win_agent.server import (
    AgentConfig,
    _editable_value_observation,
    _focused_target,
    _perform_uia_initialized,
)
from openadapt_flow.backends.windows_backend import WindowsBackend
from tests.test_native_focus_keyboard import focused_auto, png
from tests.test_win_agent_server import RunningAgent

_native_controls = {}


@pytest.fixture(autouse=True)
def independent_native_protocol(monkeypatch):
    class Reader:
        def owner(self, hwnd):
            control, _ = _native_controls[hwnd]
            return NativeOwner(hwnd, control.ProcessId, 9876)

        def read(self, owner, *, guard):
            guard()
            control, pattern = _native_controls[owner.hwnd]
            value = getattr(control, "native_stored_value", pattern.Value)
            units = len(utf16_bytes(value)) // 2 if isinstance(value, str) else 0
            return NativeRead(owner, units, units, value)

    monkeypatch.setattr(native_value, "Win32TextReader", Reader)
    yield
    _native_controls.clear()


def editable(value=""):
    auto, control = focused_auto()
    control.IsPassword = False

    def property_value(property_id, ignore_default):
        assert property_id == native_value.IS_PASSWORD_PROPERTY and ignore_default == 1
        return control.IsPassword

    control.GetPropertyValueEx = property_value
    pattern = SimpleNamespace(Value=value, IsReadOnly=False)
    control.GetValuePattern = lambda: pattern
    control.NativeWindowHandle = 12345
    _native_controls[control.NativeWindowHandle] = (control, pattern)
    return auto, control, pattern


@given(st.text(max_size=9000))
def test_exact_value_including_empty_unicode_whitespace_is_not_normalized_or_truncated(
    value,
):
    auto, control, pattern = editable(value)
    observed = _editable_value_observation(auto, {})
    assert observed["status"] == "ok" and observed["complete"] is True
    assert observed["value"] == value
    assert observed["target_fingerprint"] == _focused_target(auto)["fingerprint"]
    assert control.invocations == 0


@pytest.mark.parametrize(
    "kind",
    [
        "password",
        "password_unknown",
        "readonly",
        "readonly_unknown",
        "pattern",
        "nonstring",
        "oversized",
        "ambiguous",
        "wrong_point",
        "wrong_binding",
        "changing_value",
        "replacement",
    ],
)
def test_incomplete_or_changed_observation_returns_no_value_and_no_name_fallback(kind):
    auto, control, pattern = editable("exact")
    data = {}
    if kind == "password":
        control.IsPassword = True
    elif kind == "password_unknown":
        control.IsPassword = None
    elif kind == "readonly":
        pattern.IsReadOnly = True
    elif kind == "readonly_unknown":
        pattern.IsReadOnly = None
    elif kind == "pattern":
        control.GetValuePattern = lambda: None
    elif kind == "nonstring":
        pattern.Value = None
    elif kind == "oversized":
        pattern.Value = "x" * 65537
    elif kind == "ambiguous":
        auto, control = focused_auto(duplicate=True)
        control.IsPassword = False
        control.GetValuePattern = lambda: pattern
    elif kind == "wrong_point":
        data = {"x": 0, "y": 0}
    elif kind == "wrong_binding":
        data = {"expected_fingerprint": "f" * 64}
    elif kind in {"changing_value", "replacement"}:
        calls = []

        def changing():
            calls.append(True)
            if len(calls) == 2:
                if kind == "changing_value":
                    pattern.Value = "changed"
                else:
                    control.runtime_id = (9, 9)
            return pattern

        control.GetValuePattern = changing
    result = _editable_value_observation(auto, data)
    assert result["status"] == "unavailable" and result["complete"] is False
    assert "value" not in result and "name" not in result


def test_long_fake_provider_native_agreement_is_complete_without_length_guessing():
    auto, _, _ = editable("prefix" + "x" * 8196 + "suffix")
    assert _editable_value_observation(auto, {})["value"].endswith("suffix")


def test_actual_http_getter_siblings_share_exact_value_and_lease_binding():
    auto, control, pattern = editable("")
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="unit-token"),
        grab_fn=png,
        context_fn=lambda: {
            "status": "ok",
            "application": "test-runtime",
            "application_version": "sha256:" + "a" * 64,
            "session": "b" * 64,
            "environment": "c" * 64,
            "workflow_state": None,
        },
        uia_fn=lambda operation, data: _perform_uia_initialized(auto, operation, data),
    )
    try:
        backend = WindowsBackend(agent.url, auth_token="unit-token")
        assert "native_editable_value_v2" in backend.agent_capabilities()
        point = tuple(_focused_target(auto)["point"])
        assert backend.text_value_at(*point) == ""
        assert backend.focused_text_value() == ""
        assert backend.editable_value_observation()["complete"] is True
        pattern.Value = "  exact \u00e9 "
        assert (
            backend.text_value_at(*point)
            == backend.focused_text_value()
            == pattern.Value
        )
        backend.guarded_keyboard_frame()
        backend.arm_guarded_keyboard(*point)
        control.runtime_id = (9, 9)
        assert (
            backend.text_value_at(*point) is None
            and backend.focused_text_value() is None
        )
        backend.cancel_guarded_keyboard()
        assert backend.focused_text_value() == pattern.Value
        control.IsPassword = True
        assert (
            backend.text_value_at(*point) is None
            and backend.focused_text_value() is None
        )
    finally:
        agent.close()


@pytest.mark.parametrize(
    "field", ["schema", "missing_proof", "hash", "extent", "read_count"]
)
def test_http_getter_siblings_reject_legacy_or_unproven_complete_flags(field):
    auto, _, _ = editable("exact")

    def altered(operation, data):
        result = _perform_uia_initialized(auto, operation, data)
        if operation == "editable-value" and result.get("status") == "ok":
            if field == "schema":
                result["schema"] = "openadapt.native-editable-value/v1"
            elif field == "missing_proof":
                result.pop("completeness_proof")
            else:
                key, value = {
                    "hash": ("sha256_utf16le", "0" * 64),
                    "extent": ("utf16_units", 0),
                    "read_count": ("native_reads", True),
                }[field]
                result["completeness_proof"][key] = value
        return result

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="unit-token"),
        grab_fn=png,
        uia_fn=altered,
    )
    try:
        backend = WindowsBackend(agent.url, auth_token="unit-token")
        point = tuple(_focused_target(auto)["point"])
        assert backend.editable_value_observation() is None
        assert backend.focused_text_value() is None
        assert backend.text_value_at(*point) is None
    finally:
        agent.close()


def test_confirmed_provider_prefix_is_unavailable_across_all_text_readers():
    auto, control, _ = editable("x" * 4096)
    control.native_stored_value = "x" * 8192 + "-END"
    result = _editable_value_observation(auto, {})
    assert result == {
        "status": "unavailable",
        "complete": False,
        "reason": "provider_native_value_disagreement",
    }
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="unit-token"),
        grab_fn=png,
        uia_fn=lambda op, data: _perform_uia_initialized(auto, op, data),
    )
    try:
        backend = WindowsBackend(agent.url, auth_token="unit-token")
        point = tuple(_focused_target(auto)["point"])
        assert backend.editable_value_observation() is None
        assert backend.focused_text_value() is None
        assert backend.text_value_at(*point) is None
    finally:
        agent.close()
