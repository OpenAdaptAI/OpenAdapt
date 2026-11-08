"""macOS AX identity text for table cells (structured_text) and F-keys.

A cell's own text names one field, not the record, so identity reads the
whole row, or abstains when the toolkit exposes no row (Qt's item views).
"""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.backends.macos_backend import MacElement, QuartzMacAXClient
from openadapt_flow.backends.remote_display import _MAC_KEYCODES


class _Node:
    def __init__(self, role: str, text: str | None = None, parent: Any = None):
        self.role, self.text, self.parent, self.children = role, text, parent, []
        if parent is not None:
            parent.children.append(self)


def _client() -> QuartzMacAXClient:
    client = object.__new__(QuartzMacAXClient)

    def attr(node: Any, name: str) -> Any:
        return {
            "AXParent": node.parent,
            "AXRole": node.role,
            "AXChildren": node.children,
        }[name]

    client._attr = attr  # type: ignore[method-assign]
    client._text = lambda node: node.text  # type: ignore[method-assign]
    return client


def _element(native: Any) -> MacElement:
    return MacElement(
        ax_path="@point",
        accessible_id=None,
        role="text",
        name=native.text,
        app_pid=1,
        window_title="w",
        bounds=(0, 0, 10, 10),
        text=native.text,
        native=native,
    )


def test_cell_in_a_row_reads_as_the_whole_row() -> None:
    window = _Node("AXWindow", "w")
    table = _Node("AXTable", None, window)
    row = _Node("AXRow", None, table)
    title = _Node("AXCell", None, row)
    title_text = _Node("AXStaticText", "Diabetes Care Guide", title)
    author = _Node("AXCell", None, row)
    _Node("AXStaticText", "Ana Ruiz-Ortega", author)

    text = _client().structured_text(_element(title_text))

    assert text == "Diabetes Care Guide Ana Ruiz-Ortega"


def test_cell_directly_under_a_table_abstains() -> None:
    window = _Node("AXWindow", "w")
    table = _Node("AXTable", None, window)
    _Node("AXRow", "1", table)
    cell = _Node("AXStaticText", "Diabetes Care Guide", table)

    assert _client().structured_text(_element(cell)) is None


def test_a_control_outside_any_table_reads_as_itself() -> None:
    window = _Node("AXWindow", "w")
    group = _Node("AXGroup", None, window)
    button = _Node("AXButton", "Save note", group)

    assert _client().structured_text(_element(button)) == "Save note"


def test_an_oversized_row_abstains() -> None:
    table = _Node("AXTable", None, _Node("AXWindow", "w"))
    row = _Node("AXRow", None, table)
    cells = [_Node("AXStaticText", f"c{i}", row) for i in range(300)]

    assert _client().structured_text(_element(cells[0])) is None


@pytest.mark.parametrize(
    ("key", "code"),
    [("f1", 0x7A), ("f2", 0x78), ("f5", 0x60), ("f10", 0x6D), ("f12", 0x6F)],
)
def test_function_keys_have_mac_virtual_key_codes(key: str, code: int) -> None:
    assert _MAC_KEYCODES[key] == code
