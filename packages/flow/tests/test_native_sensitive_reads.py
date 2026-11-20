"""All native previews, probes and reads share one current sensitivity guard."""

from types import SimpleNamespace

import pytest

from openadapt_flow.backends.win_agent import server
from openadapt_flow.backends.win_agent.native_value import PROOF_SCHEMA
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.ir import StructuralLocator
from tests.test_approval_form_fields import _agent, _Control, _window


class CountedControl(_Control):
    def __init__(self, status=False, change=None):
        self.calls = []
        self._status = status
        self.change = change
        super().__init__("TextControl", value="fixture value", name="fixture label")
        self._status = status
        self.AutomationId = "field"

    @property
    def IsPassword(self):
        if self._status == "error":
            raise RuntimeError("status unavailable")
        return self._status

    @IsPassword.setter
    def IsPassword(self, value):
        self._status = value

    @property
    def Name(self):
        self.calls.append("Name")
        if self.change == "name":
            self._status = True
        return self._name

    @Name.setter
    def Name(self, value):
        self._name = value

    @property
    def GetValuePattern(self):
        self.calls.append("pattern_lookup")
        if self.change == "pattern_lookup":
            self._status = True

        def factory():
            self.calls.append("pattern_call")
            if self.change == "pattern_call":
                self._status = True
            outer = self

            class Pattern:
                IsReadOnly = False

                @property
                def Value(self):
                    outer.calls.append("Value")
                    if outer.change == "value":
                        outer._status = True
                    return outer._value

            if self.change in {"legacy", "name"}:
                raise RuntimeError("no value pattern")
            return Pattern()

        return factory

    def GetLegacyIAccessiblePattern(self):
        self.calls.append("legacy_call")
        if self.change == "legacy":
            self._status = True
        outer = self

        class Pattern:
            @property
            def Value(self):
                outer.calls.append("LegacyValue")
                return None

        return Pattern()


def invoke(monkeypatch, target, entry):
    if entry == "preview":
        return server._value_text(target)
    if entry == "probe":
        return server._form_exposes_value(target)
    window = _window(target)
    perform = _agent(monkeypatch, window)
    if entry == "form":
        return perform("window-form-fields", {"hwnd": 7, "pid": 10})
    import sys

    sys.modules["uiautomation"].GetRootControl = lambda: window
    return perform(
        "read", {"locator": {"automation_id": "field"}, "rows": entry == "rows"}
    )


@pytest.mark.parametrize("entry", ["preview", "probe", "form", "read", "rows"])
@pytest.mark.parametrize("status", [True, None, 0, 1, "false", "error"])
def test_sensitive_unknown_or_nonboolean_status_never_calls_a_text_getter(
    monkeypatch, entry, status
):
    target = CountedControl(status)
    result = invoke(monkeypatch, target, entry)
    assert target.calls == []
    if entry in {"preview", "probe"}:
        assert result is None
    elif entry == "form":
        assert result["window"] is None and result["fields"] == []
    else:
        assert result["text"] is None and result["preview_text"] is None
        assert result["complete"] is False
        if entry == "rows":
            assert result["rows"] is None and result["preview_rows"] == []


@pytest.mark.parametrize("entry", ["preview", "probe"])
@pytest.mark.parametrize(
    "change", ["pattern_lookup", "pattern_call", "value", "legacy", "name"]
)
def test_preview_and_capability_changes_stop_later_getters_and_discard_data(
    monkeypatch, entry, change
):
    target = CountedControl(change=change)
    result = invoke(monkeypatch, target, entry)
    if entry == "probe" and change in {"legacy", "name"}:
        # A non-value capability result need not inspect preview fallbacks.
        assert result is False
        assert "legacy_call" not in target.calls and "Name" not in target.calls
    else:
        assert result is None
    if change == "pattern_lookup":
        assert "pattern_call" not in target.calls
    if change in {"pattern_lookup", "pattern_call"}:
        assert "Value" not in target.calls
    if change == "legacy":
        assert "LegacyValue" not in target.calls and "Name" not in target.calls
    if change != "name":
        assert "Name" not in target.calls


@pytest.mark.parametrize("entry", ["preview", "probe", "form", "read", "rows"])
def test_known_nonsensitive_controls_keep_qualified_values_and_explicit_previews(
    monkeypatch, entry
):
    target = CountedControl()
    result = invoke(monkeypatch, target, entry)
    if entry == "preview":
        assert result == "fixture value"
    elif entry == "probe":
        assert result is True
    elif entry == "form":
        assert result["window"] is not None
        assert result["fields"][0]["text"] == "fixture value"
        assert result["fields"][0]["value_proof"]["schema"] == PROOF_SCHEMA
    else:
        assert result["complete"] is True and result["text"] == "fixture value"
        assert result["preview_text"] == "fixture value"
        if entry == "rows":
            assert result["rows"] is None and result["rows_complete"] is False


@pytest.mark.parametrize("operation", ["read", "window-form-fields"])
@pytest.mark.parametrize(
    "version", [None, "openadapt.native-value-completeness/v1", True]
)
def test_old_client_new_server_refuses_protocol_before_any_provider_access(
    operation, version
):
    calls = []

    class NoProvider:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("protocol mismatch must precede provider access")

    payload = (
        {"locator": {"automation_id": "field"}}
        if operation == "read"
        else {"hwnd": 7, "pid": 10}
    )
    if version is not None:
        payload["value_contract"] = version
    with pytest.raises(server.AgentRequestError) as error:
        if operation == "read":
            server._perform_uia_initialized(NoProvider(), operation, payload)
        else:
            server._window_form_fields_operation(payload, NoProvider(), NoProvider())
    assert error.value.code == "invalid_schema" and calls == []


@pytest.mark.parametrize("operation", ["read", "window-form-fields"])
def test_new_client_old_closed_server_refuses_before_any_getter(operation):
    backend = object.__new__(WindowsBackend)
    backend._approval_window = lambda *_a: (7, 10)
    calls = []

    def legacy_route(path, payload):
        assert payload["value_contract"] == PROOF_SCHEMA
        required = {"locator"} if operation == "read" else {"hwnd", "pid"}
        try:
            server._exact_object(
                payload, required=frozenset(required), label="legacy exact request"
            )
        except server.AgentRequestError as error:
            assert error.code == "invalid_schema"
            return SimpleNamespace(available=False, payload=None)
        calls.append("unsafe_legacy_getter")
        raise AssertionError("new request must be refused before legacy preview")

    backend._post_typed_read = legacy_route
    result = (
        backend.read_structural(StructuralLocator(automation_id="field"))
        if operation == "read"
        else backend.approval_form_fields()
    )
    assert result is None and calls == []


@pytest.mark.parametrize(
    "metadata",
    ["AutomationId", "ControlTypeName", "ClassName", "ProcessId", "BoundingRectangle"],
)
@pytest.mark.parametrize("status", [True, None])
def test_semantic_candidate_metadata_cannot_open_a_later_name_getter(
    monkeypatch, metadata, status
):
    class MetadataControl(CountedControl):
        armed = False
        changed = False

        def __getattribute__(self, key):
            value = super().__getattribute__(key)
            if key == metadata and super().__getattribute__("armed"):
                self._status = status
                self.changed = True
            if key == "Name" and super().__getattribute__("changed"):
                raise AssertionError("no Name getter after sensitive metadata change")
            return value

    target = MetadataControl()
    target.armed = True
    # Each metadata property has the same last-check model, independently of
    # which structural census field happens to be queried first.
    with pytest.raises(Exception) as error:
        server._candidate(target, get=server._read_control_value)
    assert type(error.value).__name__ == "ValueUnavailable"
    assert "Value" not in target.calls and "legacy_call" not in target.calls
