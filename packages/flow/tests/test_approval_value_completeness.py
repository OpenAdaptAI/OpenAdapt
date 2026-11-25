"""Incomplete native sources cannot authorize an approval through any entry."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.approval_mode.form_state import (
    approval_form_state,
    window_fields_state,
)
from openadapt_flow.approval_mode.window_fields import valid_form_field
from tests.test_approval_form_fields import (
    _agent,
    _Control,
    _fields,
    _frame,
    _proof,
    _window,
    _windows_backend,
)

NO_OCR = SimpleNamespace(ocr=lambda _: [])
SAME_VISIBLE_OCR = SimpleNamespace(ocr=lambda _: [{"text": "same visible prefix"}])


def _through_wire(answer):
    return _windows_backend({"type": (7, 10)}, answer)[0]


@pytest.mark.parametrize(
    "kind,stored,provider",
    [
        (kind, stored, provider)
        for kind in (
            "EditControl",
            "DocumentControl",
            "DataItemControl",
            "TextControl",
            "ButtonControl",
        )
        for stored, provider in (
            ("x" * 4096 + "-different suffix", "x" * 4096),
            ("hidden stored text", ""),
            ("stored", None),
        )
        if provider is not None
        or kind in {"EditControl", "DocumentControl", "DataItemControl"}
    ],
)
def test_omitted_storage_refuses_the_actual_server_client_and_approval_path(
    monkeypatch,
    kind,
    stored,
    provider,
):
    control = _Control(kind, value=provider)
    control.native_override = stored
    perform = _agent(monkeypatch, _window(control))
    wire = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert wire["window"] is None and wire["fields"] == []
    backend = _through_wire(wire)
    assert backend.approval_form_fields("save") is None
    assert approval_form_state(backend, SAME_VISIBLE_OCR, _frame(), "save") is None


@pytest.mark.parametrize(
    "fault",
    [
        "missing_hwnd",
        "missing_runtime",
        "foreign_pid",
        "password",
        "password_unknown",
        "no_ancestry",
    ],
)
def test_unsupported_or_unbound_full_value_source_cannot_downgrade(monkeypatch, fault):
    control = _Control("EditControl", value="same visible prefix")
    perform = _agent(monkeypatch, _window(control))
    if fault == "missing_hwnd":
        control.NativeWindowHandle = 0
    elif fault == "missing_runtime":
        control.GetRuntimeId = lambda: []
    elif fault == "foreign_pid":
        control.ProcessId = 99
    elif fault == "password":
        control.IsPassword = True
    elif fault == "password_unknown":
        control.IsPassword = None
    else:
        control.parent = None
    wire = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert wire["window"] is None
    assert (
        approval_form_state(_through_wire(wire), SAME_VISIBLE_OCR, _frame(), "save")
        is None
    )


@pytest.mark.parametrize("kind", ["edit", "value", "text"])
@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "null",
        "legacy_flag",
        "wrong_hash",
        "wrong_extent",
        "wrong_pid",
        "missing_read",
        "bool_count",
        "extra",
    ],
)
def test_bad_value_proof_refuses_before_digest_or_mask_at_both_consumers(kind, fault):
    fields = _fields()
    field = fields["fields"][1]
    field["kind"] = kind
    proof = field["value_proof"]
    if fault == "absent":
        del field["value_proof"]
    elif fault == "null":
        field["value_proof"] = None
        field["value_identity"] = None
    elif fault == "legacy_flag":
        field["value_proof"] = {"complete": True}
    elif fault == "wrong_hash":
        proof["sha256_utf16le"] = "0" * 64
    elif fault == "wrong_extent":
        proof["utf16_units"] += 1
    elif fault == "wrong_pid":
        proof["pid"] = 99
    elif fault == "missing_read":
        proof["native_reads"] = 1
    elif fault == "bool_count":
        proof["uia_reads"] = True
    else:
        proof["extra"] = True
    # Unproved visible labels have a separate existing convention. They are
    # not value-backed claims; a null proof is allowed only for that channel.
    if kind == "text" and fault == "null":
        assert valid_form_field(field)
        return
    wire = {
        "status": "ok",
        "window": {"hwnd": 7, "pid": 10, "bounds": list(fields["bounds"])},
        "fields": fields["fields"],
        "refusal": None,
    }
    backend = _through_wire(wire)
    assert backend.approval_form_fields("save") is None
    assert approval_form_state(backend, SAME_VISIBLE_OCR, _frame(), "save") is None
    if fault != "wrong_pid":
        assert window_fields_state(NO_OCR, _frame(), fields) is None


@given(st.text(max_size=10000))
def test_the_shared_wire_model_preserves_exact_unicode_value_and_proof(value):
    field = {
        "kind": "edit",
        "rect": [1, 2, 30, 40],
        "text": value,
        "state": None,
        "value_proof": _proof(value),
        "value_identity": "a" * 64,
    }
    assert valid_form_field(field, pid=10)
    mutated = deepcopy(field)
    mutated["text"] += "hidden suffix"
    assert not valid_form_field(mutated, pid=10)


def test_empty_and_container_values_are_retained_without_hiding_pixels():
    fields = _fields(search="", note="")
    fields["fields"][1]["kind"] = "value"
    seen = []

    def ocr(png):
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            seen.append(image.crop((20, 340, 620, 460)).tobytes())
        return [{"text": "visible"}]

    assert window_fields_state(SimpleNamespace(ocr=ocr), _frame(), fields) is not None
    # Compare the actual region, not an assumed ink pixel in the font.
    import io

    from PIL import Image

    with Image.open(io.BytesIO(_frame())) as original:
        expected = original.crop((20, 340, 620, 460)).tobytes()
    assert seen == [expected]


def test_native_channel_failure_or_missing_frame_refuses_even_with_stable_ocr():
    def unavailable(*_):
        raise RuntimeError("no independent full value")

    backend = SimpleNamespace(approval_form_fields=unavailable)
    assert approval_form_state(backend, SAME_VISIBLE_OCR, _frame(), "save") is None
    assert approval_form_state(backend, SAME_VISIBLE_OCR, None, "save") is None


@pytest.mark.usefixtures("approver")
@pytest.mark.parametrize("phase", ["before_request", "after_person_approval"])
def test_actual_gate_refuses_incomplete_native_value_before_the_save(tmp_path, phase):
    from openadapt_flow.approval_mode.gate import ApprovalGate, SaveContext, StepContext
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
    from openadapt_flow.ir import ActionKind, Step, StepResult, Workflow
    from tests.test_approval_mode_each_item import APPROVE, Person

    fields = _fields()
    wire = {
        "status": "ok",
        "window": {"hwnd": 7, "pid": 10, "bounds": list(fields["bounds"])},
        "fields": fields["fields"],
        "refusal": None,
    }
    backend = _through_wire(wire)
    store = ItemApprovalStore(tmp_path / "run")
    person = Person([APPROVE])
    person.store = store
    gate = ApprovalGate(
        "each_item",
        approval_store=store,
        approval_wait_s=5,
        approval_poll_s=1,
        sleep=person.sleep,
        monotonic=person.monotonic,
    )
    gate.begin_item(run_id="run-1", bundle_content_digest="a" * 64, profile="standard")
    step = Step(
        id="save",
        intent="Save the approved item",
        action=ActionKind.KEY,
        key="Enter",
        risk="irreversible",
    )
    workflow = Workflow(name="native-completeness", steps=[step])
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    frame = _frame()

    def read(png):
        return approval_form_state(backend, NO_OCR, png, step.id)

    if phase == "before_request":
        fields["fields"][1]["value_proof"] = None
    first = gate.before_step(
        StepContext(
            step=step,
            workflow=workflow,
            result=result,
            step_index=0,
            run_id="run-1",
            frame=lambda: frame,
            form_state=read,
        )
    )
    if phase == "before_request":
        assert not first.proceed
        assert [check.detail for check in first.checks] == ["form_state_unreadable"]
        assert store.requests() == []
    else:
        assert first.proceed and len(store.requests()) == 1
        # A formerly complete channel becomes incomplete after actual signed
        # approval. Unchanged pixels and valid historical approval cannot help.
        fields["fields"][1]["value_proof"] = None
        edge = gate.before_save(
            SaveContext(
                step=step,
                workflow=workflow,
                result=result,
                prior_results=[],
                effect_verifier=None,
                governed_authorization=None,
                model_calls=0,
                frame=lambda: frame,
                form_state=read,
            )
        )
        assert not edge.proceed
        assert [check.detail for check in edge.checks] == ["form_state_unreadable"]


@pytest.mark.parametrize("replacement", ["field", "window"])
def test_fresh_handle_lookup_rejects_a_replaced_uia_runtime(monkeypatch, replacement):
    import sys

    control = _Control("EditControl", value="unchanged")
    window = _window(control)
    perform = _agent(monkeypatch, window)
    auto = sys.modules["uiautomation"]
    original = auto.ControlFromHandle
    lookup_count = 0
    changed = (
        _Control("EditControl", value="unchanged")
        if replacement == "field"
        else _window()
    )
    changed.NativeWindowHandle = (
        control.NativeWindowHandle if replacement == "field" else 7
    )
    changed.GetRuntimeId = lambda: [42, changed.NativeWindowHandle, 999]

    def fresh(hwnd):
        nonlocal lookup_count
        if hwnd == 7:
            lookup_count += 1
        if (replacement == "field" and hwnd == control.NativeWindowHandle) or (
            replacement == "window" and hwnd == 7 and lookup_count > 1
        ):
            return changed
        return original(hwnd)

    monkeypatch.setattr(auto, "ControlFromHandle", fresh)
    wire = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert wire["window"] is None and wire["fields"] == []


def test_field_identity_is_required_and_replacement_changes_the_approval_digest():
    before = _fields()
    expected = window_fields_state(NO_OCR, _frame(), before)
    assert expected is not None
    changed = deepcopy(before)
    changed["fields"][1]["value_identity"] = "b" * 64
    actual = window_fields_state(NO_OCR, _frame(), changed)
    assert actual is not None and actual != expected
    for missing in (None, True, "prefix", "A" * 64):
        changed["fields"][1]["value_identity"] = missing
        assert window_fields_state(NO_OCR, _frame(), changed) is None
