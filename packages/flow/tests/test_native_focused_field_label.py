"""Native backends expose focused_field_label from the accessibility tree (#1868).

Unit coverage for the three substrates. Real-app proofs (Chart.swift /
chart_gtk.py) need the macOS lab VM and stay on the parent issue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.backend import FieldLabelBackend
from openadapt_flow.backends import linux_backend as linux_backend_module
from openadapt_flow.backends.linux_backend import LinuxElement
from openadapt_flow.backends.macos_backend import _ax_field_label
from openadapt_flow.backends.win_agent import server as win_agent_server
from openadapt_flow.backends.windows_backend import WindowsBackend, _TypedResponse
from tests.test_linux_backend import TEXT_ELEMENT, FakeLinuxClient
from tests.test_linux_backend import backend as linux_be


def test_ax_field_label_prefers_title_ui_element() -> None:
    attrs = {
        "field": {
            "AXTitleUIElement": "label-el",
            "AXTitle": "ignored-own-title",
            "AXDescription": "ignored-own-desc",
        },
        "label-el": {
            "AXTitle": "Insurance No.",
            "AXDescription": "unused",
        },
    }

    def copy_attr(element: Any, attribute: str) -> Any:
        return attrs.get(element, {}).get(attribute)

    assert _ax_field_label("field", copy_attr) == "Insurance No."


def test_ax_field_label_falls_back_to_own_title_then_description() -> None:
    def copy_title(element: Any, attribute: str) -> Any:
        if attribute == "AXTitleUIElement":
            return None
        if attribute == "AXTitle":
            return "  Policy #  "
        return None

    assert _ax_field_label("field", copy_title) == "Policy #"

    def copy_desc(element: Any, attribute: str) -> Any:
        if attribute == "AXDescription":
            return "member_id"
        return None

    assert _ax_field_label("field", copy_desc) == "member_id"
    assert _ax_field_label("field", lambda *_: None) is None


def test_linux_backend_focused_field_label_reads_client_then_name() -> None:
    class LabelClient(FakeLinuxClient):
        def field_label(self, element):
            self.calls.append(("field-label", element.native_id))
            return "Insurance No."

    target = linux_be(LabelClient())
    assert isinstance(target, FieldLabelBackend)
    assert target.focused_field_label() == "Insurance No."

    plain = linux_be(FakeLinuxClient())
    # TEXT_ELEMENT.name is "Document"
    assert plain.focused_field_label() == TEXT_ELEMENT.name

    missing = FakeLinuxClient()
    missing.focused = None
    assert linux_be(missing).focused_field_label() is None


def test_linux_backend_focused_field_label_out_of_scope_is_none() -> None:
    client = FakeLinuxClient()
    client.focused = LinuxElement(
        "0.1:9",
        "body",
        "textbox",
        "Document",
        "gedit",
        "other.txt",
        9001,
        (120, 260, 580, 380),
        ("focus",),
    )
    assert linux_be(client).focused_field_label() is None


def test_atspi_field_label_prefers_labelled_by() -> None:
    client = object.__new__(linux_backend_module.AtspiLinuxClient)
    labelled_by = object()
    client._atspi = SimpleNamespace(
        RelationType=SimpleNamespace(LABELLED_BY=labelled_by)
    )

    class Rel:
        def __init__(self, rel_type, target):
            self._type = rel_type
            self._target = target

        def get_relation_type(self):
            return self._type

        def get_target(self, index):
            assert index == 0
            return self._target

    class Node:
        def __init__(self, name, relations=None):
            self._name = name
            self._relations = relations or []

        def get_name(self):
            return self._name

        def get_relation_set(self):
            return self._relations

    label_node = Node("Chart note")
    field_node = Node(
        "ignored-own-name",
        relations=[Rel(labelled_by, label_node)],
    )
    element = LinuxElement(
        "0.1",
        None,
        "textbox",
        "ignored-own-name",
        "chart",
        "Chart",
        1,
        (1, 1, 10, 10),
        ("focus",),
        native=field_node,
    )
    assert client.field_label(element) == "Chart note"

    bare = LinuxElement(
        "0.2",
        None,
        "textbox",
        "Own Name",
        "chart",
        "Chart",
        1,
        (1, 1, 10, 10),
        ("focus",),
        native=Node("Own Name"),
    )
    assert client.field_label(bare) == "Own Name"


def test_uia_field_label_prefers_labeled_by_then_name() -> None:
    class Ctrl:
        def __init__(self, name, labeled_by=None, prop_id=30092):
            self.Name = name
            self._labeled_by = labeled_by
            self._prop_id = prop_id

        def GetPropertyValue(self, prop_id):
            if prop_id == self._prop_id:
                return self._labeled_by
            return None

    label = Ctrl("Insurance No.")
    field = Ctrl("ignored", labeled_by=label)
    assert win_agent_server._uia_field_label(field) == "Insurance No."
    assert win_agent_server._uia_field_label(Ctrl("Policy #")) == "Policy #"
    assert win_agent_server._uia_field_label(Ctrl("")) is None


def test_windows_backend_focused_field_label_reads_typed_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsBackend("http://127.0.0.1:9", timeout_s=0.5)

    def fake_post(path: str, payload: dict) -> _TypedResponse:
        assert path == "/uia/focused-field-label"
        assert payload == {}
        return _TypedResponse(True, {"status": "ok", "label": "  Policy #  "})

    monkeypatch.setattr(backend, "_post_typed_read", fake_post)
    assert isinstance(backend, FieldLabelBackend)
    assert backend.focused_field_label() == "Policy #"

    monkeypatch.setattr(
        backend, "_post_typed_read", lambda *a, **k: _TypedResponse(False, None)
    )
    assert backend.focused_field_label() is None
