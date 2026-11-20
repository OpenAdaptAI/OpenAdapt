"""All native previews, probes and reads share one current sensitivity guard."""

from types import SimpleNamespace

import pytest

from openadapt_flow.backends.win_agent import server
from openadapt_flow.backends.win_agent.native_value import PROOF_SCHEMA
from openadapt_flow.backends.windows_backend import (
    CLIENT_VALUE_READ_CONTRACT,
    WindowsBackend,
)
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
    if entry == "probe":
        # The availability probe no longer touches any text/pattern getter.
        assert result is True and target.calls == []
        return
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
    "version",
    [
        None,
        "openadapt.native-value-completeness/v1",
        "openadapt.native-value-completeness/v2",
        "openadapt.native-value-completeness/v3",
        "openadapt.native-value-read/v1",
        True,
    ],
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
        assert payload["value_contract"] == CLIENT_VALUE_READ_CONTRACT
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


@pytest.mark.parametrize(
    "fault", ["missing", "lookup", "call", "none", "object", "integer"]
)
def test_unknown_value_capability_never_becomes_a_caption_or_mask(monkeypatch, fault):
    class Target(CountedControl):
        @property
        def GetPropertyValueEx(self):
            if fault == "missing":
                return None
            if fault == "lookup":
                raise RuntimeError("availability lookup failed")

            def query(property_id, ignore_default):
                assert ignore_default == 1
                if property_id == 30019:
                    return self.IsPassword
                if property_id != 30043:
                    return _Control.GetPropertyValueEx(
                        self, property_id, ignore_default
                    )
                if fault == "call":
                    raise RuntimeError("availability query failed")
                return {"none": None, "object": object(), "integer": 0}[fault]

            return query

    target = Target()
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is None and result["fields"] == []
    assert target.calls == []


@pytest.mark.parametrize("fault", ["lookup", "call", "readability", "value"])
def test_known_value_capability_failure_never_downgrades_to_name_mask(
    monkeypatch, fault
):
    class Target(CountedControl):
        @property
        def GetValuePattern(self):
            self.calls.append("pattern_lookup")
            if fault == "lookup":
                raise RuntimeError("pattern lookup failed")
            outer = self

            def factory():
                outer.calls.append("pattern_call")
                if fault == "call":
                    raise RuntimeError("pattern query failed")

                class Pattern:
                    @property
                    def IsReadOnly(self):
                        if fault == "readability":
                            raise RuntimeError("readability failed")
                        return False

                    @property
                    def Value(self):
                        outer.calls.append("Value")
                        raise RuntimeError("value failed")

                return Pattern()

            return factory

    target = Target()
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is None and result["fields"] == []
    assert "Name" not in target.calls and "legacy_call" not in target.calls


@pytest.mark.parametrize("status", [True, None])
@pytest.mark.parametrize("stage", ["lookup", "call"])
def test_availability_metadata_change_obeys_shared_sensitive_guard(
    monkeypatch, stage, status
):
    class Target(CountedControl):
        @property
        def GetPropertyValueEx(self):
            if stage == "lookup":
                self._status = status

            def query(property_id, ignore_default):
                assert ignore_default == 1
                if property_id == 30019:
                    return self._status
                assert property_id == 30043
                self.calls.append("availability_call")
                self._status = status
                return False

            return query

    target = Target()
    assert server._form_exposes_value(target) is None
    assert target.calls == ([] if stage == "lookup" else ["availability_call"])


def test_known_absent_value_pattern_keeps_caption_separate_and_rechecks_after_name(
    monkeypatch,
):
    target = CountedControl()
    target._value = None
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is not None
    field = result["fields"][0]
    assert field["kind"] == "text" and field["text"] == "fixture label"
    assert field["value_proof"] is None and field["value_identity"] is None
    assert "pattern_lookup" not in target.calls and "Value" not in target.calls

    class ChangingCaption(CountedControl):
        @property
        def Name(self):
            self._value = "new required value"
            return self._name

        @Name.setter
        def Name(self, value):
            self._name = value

    target = ChangingCaption()
    target._value = None
    changed = invoke(monkeypatch, target, "form")
    assert changed["window"] is None and changed["fields"] == []


@pytest.mark.parametrize("operation", ["read", "window-form-fields"])
@pytest.mark.parametrize(
    "helper_version",
    [
        "openadapt.native-value-completeness/v1",
        "openadapt.native-value-completeness/v2",
    ],
)
def test_new_server_old_helper_refuses_before_provider_access(
    monkeypatch, operation, helper_version
):
    calls = []
    monkeypatch.setattr(
        server,
        "_native_value_api",
        lambda: SimpleNamespace(PROOF_SCHEMA=helper_version),
    )

    class NoProvider:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("old helper must refuse before content reads")

    payload = (
        {"locator": {"automation_id": "field"}}
        if operation == "read"
        else {"hwnd": 7, "pid": 10}
    )
    payload["value_contract"] = server.SERVER_VALUE_READ_CONTRACT
    with pytest.raises(
        server.AgentRequestError, match="incompatible native value contract"
    ):
        if operation == "read":
            server._perform_uia_initialized(NoProvider(), operation, payload)
        else:
            server._window_form_fields_operation(payload, NoProvider(), NoProvider())
    assert calls == []


def test_changing_only_helper_cannot_make_old_server_advertise_new_consumer_policy():
    # Historical87d negotiated against helper.PROOF_SCHEMA. The new client
    # sends a server-consumer namespace, deliberately different from ANY
    # supported helper proof version, so helper-only replacement cannot turn
    # the old read/probe/preview semantics into the new protocol.
    assert server.SERVER_VALUE_READ_CONTRACT.startswith("openadapt.native-value-read/")
    assert server.SERVER_VALUE_READ_CONTRACT != PROOF_SCHEMA
    for version in ("v1", "v2", "v3"):
        old_server_helper_contract = "openadapt.native-value-completeness/" + version
        assert server.SERVER_VALUE_READ_CONTRACT != old_server_helper_contract


@pytest.mark.parametrize("state_kind", ["toggle", "selection"])
@pytest.mark.parametrize("status", [True, None, "error", 0])
def test_state_sources_share_the_raw_sensitive_no_getter_guard(state_kind, status):
    target = CountedControl(status=status)
    target.GetTogglePattern = lambda: pytest.fail("sensitive toggle capability called")
    target.GetSelectionItemPattern = lambda: pytest.fail(
        "sensitive selection capability called"
    )
    observed = server._form_field_state(target)
    assert not observed.complete and observed.state is None
    assert target.calls == []


@pytest.mark.parametrize("state_kind", ["toggle", "selection"])
@pytest.mark.parametrize("stage", ["factory", "state"])
@pytest.mark.parametrize("status", [True, None])
def test_state_source_metadata_changes_do_not_expose_or_retain_state(
    state_kind, stage, status
):
    target = CountedControl()
    accesses = []
    target._toggle = 0 if state_kind == "toggle" else None
    target._selected = False if state_kind == "selection" else None

    def factory():
        if stage == "factory":
            target._status = status

        class Pattern:
            def __getattribute__(self, attribute):
                if attribute in {"ToggleState", "IsSelected"}:
                    accesses.append(attribute)
                    target._status = status
                    return 0 if attribute == "ToggleState" else False
                return super().__getattribute__(attribute)

        return Pattern()

    if state_kind == "toggle":
        target.GetTogglePattern = factory
    else:
        target.GetSelectionItemPattern = factory
    observed = server._form_field_state(target)
    assert not observed.complete and observed.state is None
    if stage == "factory":
        assert accesses == []


@pytest.mark.parametrize(
    "raw_state,expected",
    [(0, "off"), (1, "on"), (2, "indeterminate"), (True, None), (None, None)],
)
def test_qualified_toggle_states_preserve_exact_typed_results(raw_state, expected):
    target = _Control("CheckBoxControl", toggle=raw_state)
    observed = server._form_field_state(target)
    assert observed.state == expected
    assert observed.complete is (raw_state is None or type(raw_state) is int)


@pytest.mark.parametrize(
    "raw_state,expected",
    [(False, "unselected"), (True, "selected"), (0, None), (None, None)],
)
def test_qualified_selection_states_do_not_coerce_unknown_to_false(raw_state, expected):
    target = _Control("TabItemControl", selected=raw_state)
    observed = server._form_field_state(target)
    assert observed.state == expected
    assert observed.complete is (raw_state is None or type(raw_state) is bool)


@pytest.mark.parametrize(
    "role",
    [
        "EditControl",
        "DocumentControl",
        "TextControl",
        "ButtonControl",
        "ListItemControl",
        "CustomControl",
        "PaneControl",
        "TitleBarControl",
    ],
)
@pytest.mark.parametrize("fault", ["unknown", "nonbool", "error"])
def test_unknown_state_scope_refuses_before_any_value_or_caption_mask(
    monkeypatch, role, fault
):
    target = CountedControl()
    target.ControlTypeName = role
    original = target.GetPropertyValueEx

    def query(property_id, ignore_default):
        if property_id == 30036:
            if fault == "error":
                raise RuntimeError("selection availability unreadable")
            return None if fault == "unknown" else 0
        return original(property_id, ignore_default)

    target.GetPropertyValueEx = query
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is None and result["fields"] == []
    assert target.calls == []


def test_supported_absent_state_and_unrepresentable_composite_stay_distinct():
    absent = server._form_field_state(_Control("CustomControl"))
    assert absent.complete and absent.state is None and absent.reason is None
    both = _Control("CustomControl", toggle=0, selected=False)
    refused = server._form_field_state(both)
    assert not refused.complete and refused.state is None
    assert refused.reason == "composite_state_unrepresented"


def test_complete_leaf_value_retains_its_independent_state_without_hiding_it(
    monkeypatch,
):
    target = _Control("EditControl", value="value", selected=False)
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is not None
    assert [entry["kind"] for entry in result["fields"]] == ["edit", "state"]
    assert result["fields"][1]["state"] == "unselected"


@pytest.mark.parametrize("pattern", ["toggle", "selection"])
def test_state_capability_change_during_getter_is_not_known_absence_or_state(pattern):
    target = _Control(
        "CustomControl",
        toggle=0 if pattern == "toggle" else None,
        selected=False if pattern == "selection" else None,
    )

    class Pattern:
        @property
        def ToggleState(self):
            target._toggle = None
            return 0

        @property
        def IsSelected(self):
            target._selected = None
            return False

    if pattern == "toggle":
        target.GetTogglePattern = Pattern
    else:
        target.GetSelectionItemPattern = Pattern
    observed = server._form_field_state(target)
    assert not observed.complete and observed.state is None
    assert observed.reason == "state_capability_changed"


@pytest.mark.parametrize(
    "fault",
    [
        "visibility_unknown",
        "visibility_nonbool",
        "visibility_error",
        "bounds_unknown",
        "type_unknown",
        "type_nonstring",
    ],
)
def test_unreadable_visible_scope_never_omits_a_required_value(monkeypatch, fault):
    target = _Control("EditControl", value="required value")
    original = target.GetPropertyValueEx
    if fault.startswith("visibility"):

        def query(property_id, ignore_default):
            if property_id == 30022:
                if fault == "visibility_error":
                    raise RuntimeError("visibility unavailable")
                return None if fault == "visibility_unknown" else 0
            return original(property_id, ignore_default)

        target.GetPropertyValueEx = query
    elif fault == "bounds_unknown":
        target.BoundingRectangle = None
    else:
        target.ControlTypeName = None if fault == "type_unknown" else 42
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is None and result["fields"] == []


@pytest.mark.parametrize("role", ["PaneControl", "GroupControl", "UnknownValueControl"])
@pytest.mark.parametrize("children", [False, True])
def test_any_capability_true_generic_value_retains_unmasked_complete_obligation(
    monkeypatch, role, children
):
    target = _Control(
        role,
        value="full generic value",
        children=[_Control("EditControl", value="child")] if children else [],
    )
    result = invoke(monkeypatch, target, "form")
    assert result["window"] is not None
    assert result["fields"][0]["kind"] == "value"
    assert result["fields"][0]["text"] == "full generic value"
    assert result["fields"][0]["value_proof"] is not None
    target.native_override = "full generic value with omitted suffix"
    refused = invoke(monkeypatch, target, "form")
    assert refused["window"] is None and refused["fields"] == []


@pytest.mark.parametrize("historical_head", ["93347ab1", "b1b6cbdf"])
def test_historical_client_import_cannot_advertise_new_local_server_contract(
    historical_head,
):
    # This exact historical import precedes every backend read in both public
    # modules. The independent reviewer also loads each full immutable module.
    source = (
        "from openadapt_flow.backends.win_agent.server import VALUE_READ_CONTRACT\n"
        "raise AssertionError('legacy backend must never reach its body')\n"
    )
    assert "VALUE_READ_CONTRACT" not in vars(server)
    with pytest.raises(ImportError, match="VALUE_READ_CONTRACT"):
        exec(
            compile(
                source, f"historical-{historical_head}-windows_backend-import", "exec"
            ),
            {},
        )


def test_client_contract_does_not_follow_local_server_metadata(monkeypatch):
    monkeypatch.setattr(
        server, "SERVER_VALUE_READ_CONTRACT", "openadapt.native-value-read/future"
    )
    calls = []
    content_reads = []

    class NoProvider:
        def __getattr__(self, name):
            content_reads.append(name)
            raise AssertionError("incompatible client must refuse before source access")

    backend = object.__new__(WindowsBackend)
    backend._approval_window = lambda *_args: (7, 10)

    def post(path, payload):
        calls.append(payload["value_contract"])
        with pytest.raises(
            server.AgentRequestError, match="incompatible native value contract"
        ):
            if path == "/uia/read":
                server._perform_uia_initialized(NoProvider(), "read", payload)
            else:
                server._window_form_fields_operation(
                    payload, NoProvider(), NoProvider()
                )
        return SimpleNamespace(available=False, payload=None)

    backend._post_typed_read = post
    assert backend.read_structural(StructuralLocator(automation_id="field")) is None
    assert backend.approval_form_fields() is None
    assert calls == [CLIENT_VALUE_READ_CONTRACT, CLIENT_VALUE_READ_CONTRACT]
    assert content_reads == []
