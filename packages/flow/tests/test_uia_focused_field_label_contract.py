"""Typed native label relations are read-only raw UIA elements."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from openadapt_flow.backends.win_agent import server
from openadapt_flow.backends.windows_backend import WindowsBackend, _TypedResponse
from openadapt_flow.compiler import compile_recording
from openadapt_flow.recorder import Recorder


class RawElement:
    """Synthetic IUIAutomationElement; no high-level Control.Name member."""

    def __init__(self, name: str | None, related: Any = None) -> None:
        self.CurrentName = name
        self.CurrentLabeledBy = related
        self.mutations: list[str] = []

    def SetFocus(self) -> None:
        self.mutations.append("focus")
        raise AssertionError("label reads must not move focus")

    def GetCurrentPropertyValue(self, property_id: int) -> Any:
        raise AssertionError(f"typed relation read queried property {property_id}")


class Control:
    """Faithful Control.Element and Control.Name surface, without COM imports."""

    def __init__(self, element: RawElement) -> None:
        self.Element = element

    @property
    def Name(self) -> str:
        return self.Element.CurrentName or ""

    def GetPropertyValue(self, property_id: int) -> Any:
        # The old 30092 fallback can return a nonempty unrelated legacy name.
        # The fixed typed path must not invoke this accessor at any ID.
        raise AssertionError(f"generic property query {property_id}")


@pytest.mark.parametrize("own_name", ["Editor", ""])
def test_raw_related_name_wins_without_generic_query(own_name: str) -> None:
    relation = RawElement("  Reference   code  ")
    element = RawElement(own_name, relation)
    assert not hasattr(relation, "Name")
    assert server._uia_labeled_by(Control(element)) is relation
    assert server._uia_field_label(Control(element)) == "Reference code"
    assert element.mutations == relation.mutations == []


def test_legacy_name_property_is_never_queried() -> None:
    class LegacyTrap(Control):
        def __init__(self, element: RawElement) -> None:
            super().__init__(element)
            self.queries: list[int] = []

        def GetPropertyValue(self, property_id: int) -> Any:
            self.queries.append(property_id)
            return "misleading legacy name"

    relation = RawElement("Reference code")
    element = RawElement("Editor", relation)
    control = LegacyTrap(element)
    assert server._uia_field_label(control) == "Reference code"
    assert control.queries == []
    assert element.mutations == relation.mutations == []


@pytest.mark.parametrize("related_name", [None, "", " \t "])
@pytest.mark.parametrize("own_name, expected", [("  Editor  ", "Editor"), ("", None)])
def test_empty_relation_falls_back_to_own_name(
    related_name: str | None, own_name: str, expected: str | None
) -> None:
    relation = RawElement(related_name)
    element = RawElement(own_name, relation)
    assert server._uia_field_label(Control(element)) == expected
    assert element.mutations == relation.mutations == []


@pytest.mark.parametrize("own_name, expected", [("Editor", "Editor"), ("", None)])
def test_absent_relation_falls_back(own_name: str, expected: str | None) -> None:
    element = RawElement(own_name)
    assert server._uia_field_label(Control(element)) == expected
    assert element.mutations == []


@pytest.mark.parametrize("stage", ["element", "relation", "related_name"])
def test_unavailable_native_getter_falls_back(stage: str) -> None:
    class Unavailable:
        @property
        def CurrentLabeledBy(self) -> Any:
            raise RuntimeError("unavailable")

        @property
        def CurrentName(self) -> str:
            raise RuntimeError("unavailable")

    class ElementUnavailable:
        Name = "Editor"

        @property
        def Element(self) -> Any:
            raise RuntimeError("unavailable")

    if stage == "element":
        control: Any = ElementUnavailable()
    elif stage == "relation":
        control = SimpleNamespace(Name="Editor", Element=Unavailable())
    else:
        control = SimpleNamespace(
            Name="Editor", Element=RawElement("Editor", Unavailable())
        )
    assert server._uia_field_label(control) == "Editor"


@pytest.mark.parametrize("focus_state", ["absent", "unavailable"])
def test_missing_focus_has_no_label(focus_state: str) -> None:
    def focused() -> Any:
        if focus_state == "unavailable":
            raise RuntimeError("unavailable")
        return None

    assert (
        server._focused_field_label(SimpleNamespace(GetFocusedControl=focused)) is None
    )


def test_real_recorder_and_compiler_keep_related_label_passively(tmp_path, monkeypatch):
    relation = RawElement("Reference code")
    element = RawElement("Editor", relation)
    focused = Control(element)
    auto = SimpleNamespace(GetFocusedControl=lambda: focused)
    windows = WindowsBackend("http://127.0.0.1:9", timeout_s=0.5)
    trace: list[Any] = []

    def post(path: str, payload: dict) -> _TypedResponse:
        assert path == "/uia/focused-field-label" and payload == {}
        trace.append("label-read")
        return _TypedResponse(
            True, server._perform_uia_initialized(auto, "focused-field-label", payload)
        )

    monkeypatch.setattr(windows, "_post_typed_read", post)
    png = io.BytesIO()
    Image.new("RGB", (1280, 800), (245, 245, 245)).save(png, format="PNG")

    class Backend:
        viewport = (1280, 800)

        def screenshot(self):
            return png.getvalue()

        def type_text(self, text):
            trace.append(("type", text))

        def focused_field_label(self):
            return windows.focused_field_label()

    recorder = Recorder(Backend(), tmp_path / "recording")
    recorder.type_text("ABC123")
    recording = recorder.finish()
    event = json.loads((recording / "events.jsonl").read_text())
    workflow = compile_recording(recording, tmp_path / "bundle", name="native-label")
    step = workflow.steps[0]
    assert trace == ["label-read", ("type", "ABC123")]
    assert element.mutations == relation.mutations == []
    assert event["text"] == step.text == "ABC123"
    assert event["field_label"] == step.field_label == "Reference code"
    assert step.param is None
